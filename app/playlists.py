"""Playlists from Spotify and YouTube, kept in Stash.

Each account connects its own Spotify or Google developer app (client id and secret, entered in Stash; the
Playlists page explains how to make one). Stash fetches the playlists and their items into its own database,
which is a copy you own: it can be exported, and it stays when a playlist is deleted on the service. Edits are
made here first (add, remove, move, rename: by hand or by an assistant over MCP) as pending changes, and pushed
to the service when you say so; a pull brings the service's state back. Nothing on the service changes until
a push.

Tokens and app secrets are stored as they are in the database (file mode 0600), like everything else in it.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from typing import Literal, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from . import db
from .main import ORIGIN, Ctx, clean_text, ctx, err

SERVICES = ("spotify", "youtube")
Service = Literal["spotify", "youtube"]
MAX_ITEMS = 5000
MAX_PLAYLISTS = 500

PROVIDERS = {
    "spotify": {
        "auth": "https://accounts.spotify.com/authorize",
        "token": "https://accounts.spotify.com/api/token",
        "api": "https://api.spotify.com/v1",
        "scope": "playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private",
    },
    "youtube": {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "api": "https://www.googleapis.com/youtube/v3",
        "scope": "https://www.googleapis.com/auth/youtube",
    },
}

TRANSPORT: httpx.BaseTransport | None = None     # tests put a MockTransport here

session_api = APIRouter(prefix="/api/playlists")


class ServiceError(RuntimeError):
    def __init__(self, service: str, status: int, message: str):
        super().__init__(f"{service}: {status} {message}")
        self.service, self.status, self.message = service, status, message


# --- HTTP to the services -----------------------------------------------------------------------------

def _http(headers: dict | None = None) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20, connect=10), headers=headers or {}, transport=TRANSPORT)


def _check(service: str, r: httpx.Response) -> dict:
    if r.status_code >= 400:
        try:
            data = r.json()
            message = (data.get("error", {}) if isinstance(data.get("error"), dict) else data).get("message") \
                or data.get("error_description") or str(data.get("error") or "")[:200]
        except ValueError:
            message = r.text[:200]
        raise ServiceError(service, r.status_code, str(message))
    if r.status_code == 204 or not r.content:
        return {}
    return r.json()


def redirect_uri(service: str) -> str:
    return f"{ORIGIN}/api/playlists/accounts/{service}/callback"


def _service(value: str) -> str:
    if value not in SERVICES:
        raise err(422, "bad_service")
    return value


# --- accounts and tokens -------------------------------------------------------------------------------

def account(c: Ctx, service: str):
    return c.con.execute("SELECT * FROM playlist_accounts WHERE user_id=? AND service=?", (c.uid, service)).fetchone()


def begin_connect(c: Ctx, service: str, client_id: str, client_secret: str) -> str:
    """Remember the app's credentials and return the address of the service's consent page."""
    service = _service(service)
    client_id, client_secret = clean_text(client_id, 200), clean_text(client_secret, 200)
    if not client_id or not client_secret:
        raise err(422, "missing_credentials")
    state = secrets.token_urlsafe(24)
    with db.tx(c.con):
        c.con.execute(
            "INSERT INTO playlist_accounts(user_id, service, client_id, client_secret, oauth_state, created_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, service) DO UPDATE SET client_id=excluded.client_id,"
            " client_secret=excluded.client_secret, oauth_state=excluded.oauth_state",
            (c.uid, service, client_id, client_secret, state, db.now()))
    p = PROVIDERS[service]
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri(service),
              "scope": p["scope"], "state": state}
    if service == "youtube":
        params |= {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
    return f"{p['auth']}?{urlencode(params)}"


def _token_request(service: str, acc, data: dict) -> dict:
    p = PROVIDERS[service]
    headers, form = {}, dict(data)
    if service == "spotify":
        basic = base64.b64encode(f"{acc['client_id']}:{acc['client_secret']}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    else:
        form |= {"client_id": acc["client_id"], "client_secret": acc["client_secret"]}
    with _http(headers) as http:
        return _check(service, http.post(p["token"], data=form))


def finish_connect(c: Ctx, service: str, code: str, state: str) -> dict:
    service = _service(service)
    acc = account(c, service)
    if not acc or not acc["oauth_state"] or not secrets.compare_digest(acc["oauth_state"], state or ""):
        raise err(400, "bad_state")
    tokens = _token_request(service, acc, {"grant_type": "authorization_code", "code": code,
                                           "redirect_uri": redirect_uri(service)})
    access, refresh = tokens.get("access_token", ""), tokens.get("refresh_token", "")
    if not access:
        raise err(502, f"{service}_error: no access token")
    expires = db.now() + int(tokens.get("expires_in", 3600))
    with db.tx(c.con):
        c.con.execute("UPDATE playlist_accounts SET access_token=?, refresh_token=?, expires_at=?, oauth_state=''"
                      " WHERE id=?", (access, refresh or acc["refresh_token"], expires, acc["id"]))
    profile = _profile(c, service)
    with db.tx(c.con):
        c.con.execute("UPDATE playlist_accounts SET external_id=?, display_name=? WHERE id=?",
                      (profile["id"], profile["name"], acc["id"]))
    return profile


def _access_token(c: Ctx, service: str) -> str:
    acc = account(c, service)
    if not acc or not acc["access_token"]:
        raise err(409, f"not_connected: connect {service} first on the Playlists page")
    if acc["expires_at"] - 60 > db.now():
        return acc["access_token"]
    if not acc["refresh_token"]:
        raise err(409, f"reconnect_needed: the {service} connection has expired; connect it again")
    tokens = _token_request(service, acc, {"grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
    access = tokens.get("access_token", "")
    if not access:
        raise err(502, f"{service}_error: token refresh failed")
    with db.tx(c.con):
        c.con.execute("UPDATE playlist_accounts SET access_token=?, refresh_token=?, expires_at=? WHERE id=?",
                      (access, tokens.get("refresh_token") or acc["refresh_token"],
                       db.now() + int(tokens.get("expires_in", 3600)), acc["id"]))
    return access


def _api(c: Ctx, service: str) -> httpx.Client:
    return _http({"Authorization": f"Bearer {_access_token(c, service)}", "Accept": "application/json"})


def _profile(c: Ctx, service: str) -> dict:
    with _api(c, service) as http:
        if service == "spotify":
            me = _check(service, http.get(f"{PROVIDERS[service]['api']}/me"))
            return {"id": me["id"], "name": me.get("display_name") or me["id"]}
        data = _check(service, http.get(f"{PROVIDERS[service]['api']}/channels", params={"part": "snippet", "mine": "true"}))
        items = data.get("items") or [{}]
        return {"id": items[0].get("id", ""), "name": items[0].get("snippet", {}).get("title", "")}


def disconnect(c: Ctx, service: str) -> dict:
    """Forget the tokens and app credentials. The playlists already fetched stay as a copy."""
    service = _service(service)
    with db.tx(c.con):
        n = c.con.execute("DELETE FROM playlist_accounts WHERE user_id=? AND service=?", (c.uid, service)).rowcount
    return {"disconnected": bool(n)}


def accounts(c: Ctx) -> list[dict]:
    rows = {r["service"]: r for r in c.con.execute("SELECT * FROM playlist_accounts WHERE user_id=?", (c.uid,))}
    out = []
    for s in SERVICES:
        r = rows.get(s)
        out.append({"service": s, "connected": bool(r and r["access_token"]), "display_name": r["display_name"] if r else "",
                    "client_id": r["client_id"] if r else "", "synced_at": r["synced_at"] if r else None,
                    "redirect_uri": redirect_uri(s)})
    return out


# --- item addresses and metadata ------------------------------------------------------------------------

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SP_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_item(service: str, text: str) -> str:
    """The video or track id in an address, a URI or a bare id; '' when it is not one for this service."""
    text = text.strip()
    if service == "youtube":
        if _YT_ID.match(text):
            return text
        try:
            u = urlsplit(text if "://" in text else "https://" + text)
        except ValueError:
            return ""
        host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        if host == "youtu.be":
            vid = u.path.strip("/").split("/")[0]
        elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
            vid = parse_qs(u.query).get("v", [""])[0]
            if not vid and u.path.startswith(("/shorts/", "/embed/", "/live/")):
                vid = u.path.split("/")[2] if len(u.path.split("/")) > 2 else ""
        else:
            return ""
        return vid if _YT_ID.match(vid) else ""
    if _SP_ID.match(text):
        return text
    m = re.match(r"^spotify:track:([A-Za-z0-9]{22})$", text)
    if m:
        return m.group(1)
    try:
        u = urlsplit(text if "://" in text else "https://" + text)
    except ValueError:
        return ""
    if (u.hostname or "").lower() == "open.spotify.com":
        parts = u.path.strip("/").split("/")
        if parts and parts[-2:-1] == ["track"] or (len(parts) >= 2 and parts[-2] == "track"):
            return parts[-1] if _SP_ID.match(parts[-1]) else ""
    return ""


def item_url(service: str, item_id: str) -> str:
    return f"https://www.youtube.com/watch?v={item_id}" if service == "youtube" else f"https://open.spotify.com/track/{item_id}"


def _youtube_videos(http: httpx.Client, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in db.chunks(ids, 50):
        data = _check("youtube", http.get(f"{PROVIDERS['youtube']['api']}/videos",
                                          params={"part": "snippet,contentDetails", "id": ",".join(part), "maxResults": 50}))
        for v in data.get("items", []):
            out[v["id"]] = {"title": v["snippet"]["title"], "artist": v["snippet"].get("channelTitle", ""),
                            "duration_ms": _iso_duration_ms(v.get("contentDetails", {}).get("duration", ""))}
    return out


def _iso_duration_ms(value: str) -> int:
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", value or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return (h * 3600 + mi * 60 + s) * 1000


def _spotify_tracks(http: httpx.Client, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in db.chunks(ids, 50):
        data = _check("spotify", http.get(f"{PROVIDERS['spotify']['api']}/tracks", params={"ids": ",".join(part)}))
        for t in data.get("tracks", []):
            if t:
                out[t["id"]] = {"title": t["name"], "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                                "duration_ms": t.get("duration_ms", 0)}
    return out


def _metadata(http: httpx.Client, service: str, ids: list[str]) -> dict[str, dict]:
    return _youtube_videos(http, ids) if service == "youtube" else _spotify_tracks(http, ids)


# --- pulling from the service ----------------------------------------------------------------------------

def _remote_playlists(http: httpx.Client, service: str, acc) -> list[dict]:
    api = PROVIDERS[service]["api"]
    out: list[dict] = []
    if service == "spotify":
        url, params = f"{api}/me/playlists", {"limit": 50}
        while url and len(out) < MAX_PLAYLISTS:
            data = _check(service, http.get(url, params=params))
            params = None
            for p in data.get("items", []):
                if not p:
                    continue
                owner = p.get("owner", {}).get("id", "")
                out.append({"external_id": p["id"], "name": p.get("name", ""), "description": p.get("description") or "",
                            "public": int(bool(p.get("public"))), "owner": owner,
                            "editable": int(owner == acc["external_id"] or bool(p.get("collaborative"))),
                            "remote_count": p.get("tracks", {}).get("total", 0), "snapshot": p.get("snapshot_id", "")})
            url = data.get("next")
        return out
    token = None
    while len(out) < MAX_PLAYLISTS:
        params = {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _check(service, http.get(f"{api}/playlists", params=params))
        for p in data.get("items", []):
            out.append({"external_id": p["id"], "name": p["snippet"].get("title", ""),
                        "description": p["snippet"].get("description", ""),
                        "public": int(p.get("status", {}).get("privacyStatus") == "public"),
                        "owner": p["snippet"].get("channelId", ""), "editable": 1,
                        "remote_count": p.get("contentDetails", {}).get("itemCount", 0), "snapshot": p.get("etag", "")})
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def _remote_items(http: httpx.Client, service: str, external_id: str) -> list[dict]:
    api = PROVIDERS[service]["api"]
    out: list[dict] = []
    if service == "spotify":
        url, params = f"{api}/playlists/{external_id}/tracks", {"limit": 100, "fields": "next,items(added_at,track(id,name,duration_ms,artists(name),is_local))"}
        while url and len(out) < MAX_ITEMS:
            data = _check(service, http.get(url, params=params))
            params = None
            for it in data.get("items", []):
                t = it.get("track") or {}
                if not t.get("id"):
                    continue   # local files and unavailable tracks cannot be written back
                out.append({"item_id": t["id"], "remote_id": "", "title": t.get("name", ""),
                            "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                            "duration_ms": t.get("duration_ms", 0), "added_at": _iso_time(it.get("added_at"))})
            url = data.get("next")
        return out
    token = None
    while len(out) < MAX_ITEMS:
        params = {"part": "snippet", "playlistId": external_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _check(service, http.get(f"{api}/playlistItems", params=params))
        for it in data.get("items", []):
            sn = it["snippet"]
            out.append({"item_id": sn.get("resourceId", {}).get("videoId", ""), "remote_id": it["id"],
                        "title": sn.get("title", ""), "artist": sn.get("videoOwnerChannelTitle", ""),
                        "duration_ms": 0, "added_at": _iso_time(sn.get("publishedAt"))})
        token = data.get("nextPageToken")
        if not token:
            break
    durations = _youtube_videos(http, [x["item_id"] for x in out if x["item_id"]])
    for x in out:
        x["duration_ms"] = durations.get(x["item_id"], {}).get("duration_ms", 0)
    return [x for x in out if x["item_id"]]


def _iso_time(value) -> Optional[int]:
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone
    except ValueError:
        return None


def _store_items(con, playlist_id: int, items: list[dict]) -> None:
    con.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    con.executemany(
        "INSERT INTO playlist_items(playlist_id, position, remote_position, item_id, remote_id, title, artist,"
        " duration_ms, url, added_at, state) VALUES(?,?,?,?,?,?,?,?,?,?,'synced')",
        [(playlist_id, i, i, x["item_id"], x["remote_id"], x["title"][:500], x["artist"][:500], x["duration_ms"],
          x.get("url") or "", x["added_at"]) for i, x in enumerate(items)])


def pull(c: Ctx, service: str, playlist_id: Optional[int] = None, force: bool = False) -> dict:
    """Fetch the playlists (or one) and their items. A playlist with pending local changes is left alone
    unless force=True, which discards them."""
    service = _service(service)
    acc = account(c, service)
    if not acc or not acc["access_token"]:
        raise err(409, f"not_connected: connect {service} first on the Playlists page")
    con, t = c.con, db.now()
    fetched, skipped = 0, []
    with _api(c, service) as http:
        if playlist_id is not None:
            row = playlist(c, playlist_id)
            wanted = [{"external_id": row["external_id"], "name": row["name"], "description": row["description"],
                       "public": row["public"], "owner": row["owner"], "editable": row["editable"],
                       "remote_count": row["remote_count"], "snapshot": row["snapshot"]}]
            if service == "spotify":   # the one playlist's own metadata
                p = _check(service, http.get(f"{PROVIDERS[service]['api']}/playlists/{row['external_id']}",
                                             params={"fields": "name,description,public,snapshot_id,tracks.total,owner.id,collaborative"}))
                wanted[0] |= {"name": p.get("name", ""), "description": p.get("description") or "",
                              "public": int(bool(p.get("public"))), "snapshot": p.get("snapshot_id", ""),
                              "remote_count": p.get("tracks", {}).get("total", 0)}
            else:
                p = _check(service, http.get(f"{PROVIDERS[service]['api']}/playlists",
                                             params={"part": "snippet,status,contentDetails", "id": row["external_id"]}))
                for it in p.get("items", []):
                    wanted[0] |= {"name": it["snippet"].get("title", ""), "description": it["snippet"].get("description", ""),
                                  "public": int(it.get("status", {}).get("privacyStatus") == "public"),
                                  "remote_count": it.get("contentDetails", {}).get("itemCount", 0), "snapshot": it.get("etag", "")}
        else:
            wanted = _remote_playlists(http, service, acc)
        seen = set()
        for p in wanted:
            seen.add(p["external_id"])
            row = con.execute("SELECT * FROM playlists WHERE user_id=? AND service=? AND external_id=?",
                              (c.uid, service, p["external_id"])).fetchone()
            if row and row["dirty"] and not force:
                skipped.append(row["name"])
                continue
            items = _remote_items(http, service, p["external_id"])
            for x in items:
                x["url"] = item_url(service, x["item_id"])
            with db.tx(con):
                if row:
                    con.execute("UPDATE playlists SET name=?, description=?, public=?, editable=?, owner=?, remote_count=?,"
                                " snapshot=?, synced_at=?, dirty=0, gone=0 WHERE id=?",
                                (p["name"], p["description"], p["public"], p["editable"], p["owner"], len(items),
                                 p["snapshot"], t, row["id"]))
                    pid = row["id"]
                else:
                    pid = con.execute(
                        "INSERT INTO playlists(user_id, service, external_id, name, description, public, editable, owner,"
                        " remote_count, snapshot, synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (c.uid, service, p["external_id"], p["name"], p["description"], p["public"], p["editable"],
                         p["owner"], len(items), p["snapshot"], t)).lastrowid
                _store_items(con, pid, items)
            fetched += 1
        if playlist_id is None:
            with db.tx(con):   # playlists no longer on the service stay as a copy, marked gone
                for r in con.execute("SELECT id, external_id FROM playlists WHERE user_id=? AND service=?", (c.uid, service)).fetchall():
                    con.execute("UPDATE playlists SET gone=? WHERE id=?", (int(r["external_id"] not in seen), r["id"]))
                con.execute("UPDATE playlist_accounts SET synced_at=? WHERE id=?", (t, acc["id"]))
    return {"service": service, "fetched": fetched, "skipped_with_local_changes": skipped}


# --- reading ---------------------------------------------------------------------------------------------

def playlist(c: Ctx, playlist_id: int):
    row = c.con.execute("SELECT * FROM playlists WHERE id=? AND user_id=?", (playlist_id, c.uid)).fetchone()
    if not row:
        raise err(404, "no_such_playlist")
    return row


def playlist_json(r) -> dict:
    return {"id": r["id"], "service": r["service"], "external_id": r["external_id"], "name": r["name"],
            "description": r["description"], "public": bool(r["public"]), "editable": bool(r["editable"]),
            "owner": r["owner"], "remote_count": r["remote_count"], "synced_at": r["synced_at"], "dirty": bool(r["dirty"]),
            "gone": bool(r["gone"]), "url": (f"https://open.spotify.com/playlist/{r['external_id']}" if r["service"] == "spotify"
                                              else f"https://www.youtube.com/playlist?list={r['external_id']}")}


def overview(c: Ctx) -> dict:
    rows = c.con.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id AND i.state<>'removed') AS count,"
        " (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id AND i.state<>'synced') AS pending"
        " FROM playlists p WHERE p.user_id=? ORDER BY p.service, p.gone, p.name COLLATE NOCASE", (c.uid,)).fetchall()
    return {"accounts": accounts(c),
            "playlists": [playlist_json(r) | {"count": r["count"], "pending": r["pending"]} for r in rows]}


def item_json(r) -> dict:
    return {"id": r["id"], "position": r["position"], "item_id": r["item_id"], "title": r["title"], "artist": r["artist"],
            "duration_ms": r["duration_ms"], "url": r["url"], "added_at": r["added_at"], "state": r["state"],
            "moved": r["state"] == "synced" and r["remote_position"] is not None and r["remote_position"] != r["position"]}


def show(c: Ctx, playlist_id: int, offset: int = 0, limit: int = 500, removed: bool = True) -> dict:
    row = playlist(c, playlist_id)
    where = "" if removed else " AND state<>'removed'"
    rows = c.con.execute(f"SELECT * FROM playlist_items WHERE playlist_id=?{where} ORDER BY position, id LIMIT ? OFFSET ?",
                         (playlist_id, max(1, min(MAX_ITEMS, limit)), max(0, offset))).fetchall()
    total = c.con.execute(f"SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?{where}", (playlist_id,)).fetchone()[0]
    items = [item_json(r) for r in rows]
    pending = {"added": sum(x["state"] == "added" for x in items), "removed": sum(x["state"] == "removed" for x in items),
               "moved": sum(x["moved"] for x in items)}
    return {"playlist": playlist_json(row), "total": total, "offset": max(0, offset), "items": items, "pending": pending}


# --- local edits -----------------------------------------------------------------------------------------

def _renumber(con, playlist_id: int) -> None:
    rows = con.execute("SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (playlist_id,)).fetchall()
    con.executemany("UPDATE playlist_items SET position=? WHERE id=?", [(i, r["id"]) for i, r in enumerate(rows)])


def _mark_dirty(con, playlist_id: int) -> None:
    """dirty: 0 nothing pending, 1 items pending, 2 a rename pending (with or without items)."""
    pending = con.execute(
        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND (state<>'synced' OR remote_position<>position)",
        (playlist_id,)).fetchone()[0]
    current = con.execute("SELECT dirty FROM playlists WHERE id=?", (playlist_id,)).fetchone()[0]
    con.execute("UPDATE playlists SET dirty=? WHERE id=?", (2 if current == 2 else int(pending > 0), playlist_id))


def edit(c: Ctx, playlist_id: int, ops: list[dict]) -> dict:
    """Pending changes: add (addresses or ids, optionally at a position), remove (item ids), move (one item to a
    position), order (the items in the given order; the rest keep their relative order after them), rename
    (name, description, public). Nothing reaches the service until push."""
    row = playlist(c, playlist_id)
    if not row["editable"]:
        raise err(409, "not_editable: this playlist belongs to someone else")
    con, service = c.con, row["service"]
    counts = {"added": 0, "removed": 0, "moved": 0, "renamed": 0, "unknown": []}
    with db.tx(con):
        for op in ops[:200]:
            kind = op.get("op")
            if kind == "add":
                wanted = [i for i in (parse_item(service, str(x)) for x in op.get("items") or op.get("urls") or []) if i]
                counts["unknown"] += [str(x) for x in (op.get("items") or op.get("urls") or []) if not parse_item(service, str(x))]
                if not wanted:
                    continue
                total = con.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
                if total + len(wanted) > MAX_ITEMS:
                    raise err(422, "too_many_items")
                meta = {}
                try:
                    with _api(c, service) as http:
                        meta = _metadata(http, service, wanted)
                except (ServiceError, Exception):  # noqa: BLE001  metadata is a nicety; the push works without it
                    meta = {}
                position = op.get("position")
                at = total if position is None else max(0, min(int(position), total))
                con.execute("UPDATE playlist_items SET position=position+? WHERE playlist_id=? AND position>=?",
                            (len(wanted), playlist_id, at))
                for i, item_id in enumerate(wanted):
                    m = meta.get(item_id, {})
                    con.execute(
                        "INSERT INTO playlist_items(playlist_id, position, remote_position, item_id, remote_id, title, artist,"
                        " duration_ms, url, added_at, state) VALUES(?,?,NULL,?,'',?,?,?,?,?,'added')",
                        (playlist_id, at + i, item_id, m.get("title", item_id), m.get("artist", ""), m.get("duration_ms", 0),
                         item_url(service, item_id), db.now()))
                counts["added"] += len(wanted)
            elif kind == "remove":
                ids = [int(i) for i in op.get("ids", [])]
                for part in db.chunks(ids):
                    con.execute(f"DELETE FROM playlist_items WHERE playlist_id=? AND state='added' AND id IN ({db.marks(len(part))})",
                                [playlist_id, *part])
                    counts["removed"] += con.execute(
                        f"UPDATE playlist_items SET state='removed' WHERE playlist_id=? AND state='synced' AND id IN ({db.marks(len(part))})",
                        [playlist_id, *part]).rowcount
            elif kind == "move":
                item = con.execute("SELECT * FROM playlist_items WHERE id=? AND playlist_id=?", (int(op.get("id", 0)), playlist_id)).fetchone()
                if not item:
                    raise err(404, "no_such_item")
                ordered = [r["id"] for r in con.execute("SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (playlist_id,))]
                ordered.remove(item["id"])
                ordered.insert(max(0, min(int(op.get("position", 0)), len(ordered))), item["id"])
                con.executemany("UPDATE playlist_items SET position=? WHERE id=?", [(i, x) for i, x in enumerate(ordered)])
                counts["moved"] += 1
            elif kind == "order":
                wanted = [int(i) for i in op.get("ids", [])]
                have = [r["id"] for r in con.execute("SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (playlist_id,))]
                first = [i for i in wanted if i in set(have)]
                rest = [i for i in have if i not in set(first)]
                con.executemany("UPDATE playlist_items SET position=? WHERE id=?", [(i, x) for i, x in enumerate(first + rest)])
                counts["moved"] += len(first)
            elif kind == "rename":
                name = clean_text(op.get("name", row["name"]), 150) or row["name"]
                description = str(op.get("description", row["description"]))[:1000]
                public = int(bool(op.get("public", row["public"])))
                con.execute("UPDATE playlists SET name=?, description=?, public=?, dirty=2 WHERE id=?",
                            (name, description, public, playlist_id))
                counts["renamed"] += 1
            else:
                raise err(422, f"bad_op: {kind}")
        _renumber(con, playlist_id)
        _mark_dirty(con, playlist_id)
    return counts


def discard(c: Ctx, playlist_id: int) -> dict:
    """Drop the pending changes: back to what was last fetched from the service."""
    playlist(c, playlist_id)
    con = c.con
    with db.tx(con):
        con.execute("DELETE FROM playlist_items WHERE playlist_id=? AND state='added'", (playlist_id,))
        con.execute("UPDATE playlist_items SET state='synced', position=remote_position WHERE playlist_id=?", (playlist_id,))
        con.execute("UPDATE playlists SET dirty=0 WHERE id=?", (playlist_id,))
        _renumber(con, playlist_id)
    return pull(c, playlist(c, playlist_id)["service"], playlist_id, force=True)


# --- pushing to the service ----------------------------------------------------------------------------------

def push(c: Ctx, playlist_id: int) -> dict:
    """Make the service's playlist match the local one (items, order, name), then fetch it back."""
    row = playlist(c, playlist_id)
    if not row["editable"]:
        raise err(409, "not_editable: this playlist belongs to someone else")
    service, api = row["service"], PROVIDERS[row["service"]]["api"]
    con = c.con
    items = con.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (playlist_id,)).fetchall()
    desired = [r for r in items if r["state"] != "removed"]
    changes = {"added": sum(r["state"] == "added" for r in items), "removed": sum(r["state"] == "removed" for r in items),
               "calls": 0}
    with _api(c, service) as http:
        if service == "spotify":
            uris = [f"spotify:track:{r['item_id']}" for r in desired]
            _check(service, http.put(f"{api}/playlists/{row['external_id']}/tracks", json={"uris": uris[:100]}))
            changes["calls"] += 1
            for part in db.chunks(uris[100:], 100):
                _check(service, http.post(f"{api}/playlists/{row['external_id']}/tracks", json={"uris": part}))
                changes["calls"] += 1
            if row["dirty"] == 2:
                _check(service, http.put(f"{api}/playlists/{row['external_id']}",
                                         json={"name": row["name"], "description": row["description"], "public": bool(row["public"])}))
                changes["calls"] += 1
        else:
            current = [r["remote_id"] for r in sorted((r for r in items if r["remote_id"]), key=lambda r: r["remote_position"] or 0)]
            remote_of = {r["remote_id"]: r for r in items if r["remote_id"]}
            for r in items:
                if r["state"] == "removed" and r["remote_id"]:
                    _check(service, http.delete(f"{api}/playlistItems", params={"id": r["remote_id"]}))
                    changes["calls"] += 1
                    current.remove(r["remote_id"])
            new_ids: dict[int, str] = {}
            for i, r in enumerate(desired):
                if r["state"] == "added":
                    made = _check(service, http.post(f"{api}/playlistItems", params={"part": "snippet"}, json={
                        "snippet": {"playlistId": row["external_id"], "position": i,
                                    "resourceId": {"kind": "youtube#video", "videoId": r["item_id"]}}}))
                    changes["calls"] += 1
                    new_ids[r["id"]] = made.get("id", f"new-{r['id']}")
                    current.insert(min(i, len(current)), new_ids[r["id"]])
            wanted = [new_ids.get(r["id"]) or r["remote_id"] for r in desired]
            for i, rid in enumerate(wanted):
                if i < len(current) and current[i] == rid:
                    continue
                r = remote_of.get(rid) or next((x for x in desired if new_ids.get(x["id"]) == rid), None)
                if r is None:
                    continue
                _check(service, http.put(f"{api}/playlistItems", params={"part": "snippet"}, json={
                    "id": rid, "snippet": {"playlistId": row["external_id"], "position": i,
                                           "resourceId": {"kind": "youtube#video", "videoId": r["item_id"]}}}))
                changes["calls"] += 1
                changes["moved"] = changes.get("moved", 0) + 1
                current.remove(rid)
                current.insert(i, rid)
            if row["dirty"] == 2:
                _check(service, http.put(f"{api}/playlists", params={"part": "snippet,status"}, json={
                    "id": row["external_id"], "snippet": {"title": row["name"], "description": row["description"]},
                    "status": {"privacyStatus": "public" if row["public"] else "private"}}))
                changes["calls"] += 1
    with db.tx(con):
        con.execute("UPDATE playlists SET dirty=0 WHERE id=?", (playlist_id,))
    pulled = pull(c, service, playlist_id, force=True)
    return {"pushed": changes, "fetched": pulled["fetched"]}


def create(c: Ctx, service: str, name: str, description: str = "", public: bool = False) -> dict:
    """A new, empty playlist on the service, fetched into Stash at once."""
    service = _service(service)
    name = clean_text(name, 150)
    if not name:
        raise err(422, "missing_name")
    acc = account(c, service)
    api = PROVIDERS[service]["api"]
    with _api(c, service) as http:
        if service == "spotify":
            made = _check(service, http.post(f"{api}/users/{acc['external_id']}/playlists",
                                             json={"name": name, "description": description[:300], "public": bool(public)}))
            external_id = made["id"]
        else:
            made = _check(service, http.post(f"{api}/playlists", params={"part": "snippet,status"}, json={
                "snippet": {"title": name, "description": description[:1000]},
                "status": {"privacyStatus": "public" if public else "private"}}))
            external_id = made["id"]
    with db.tx(c.con):
        pid = c.con.execute(
            "INSERT INTO playlists(user_id, service, external_id, name, description, public, editable, owner, synced_at)"
            " VALUES(?,?,?,?,?,?,1,?,?)", (c.uid, service, external_id, name, description[:1000], int(bool(public)),
                                           acc["external_id"], db.now())).lastrowid
    return {"id": pid, "external_id": external_id, "name": name}


def forget(c: Ctx, playlist_id: int) -> dict:
    """Remove the local copy only. The playlist on the service is not touched."""
    playlist(c, playlist_id)
    with db.tx(c.con):
        c.con.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return {"forgotten": playlist_id}


def export(c: Ctx) -> dict:
    out = []
    for r in c.con.execute("SELECT * FROM playlists WHERE user_id=? ORDER BY service, name", (c.uid,)).fetchall():
        items = c.con.execute("SELECT * FROM playlist_items WHERE playlist_id=? AND state<>'removed' ORDER BY position, id",
                              (r["id"],)).fetchall()
        out.append(playlist_json(r) | {"items": [item_json(i) for i in items]})
    return {"exported_at": db.now(), "playlists": out}


# --- the web UI, with the session cookie ----------------------------------------------------------------------

class Connect(BaseModel):
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=200)


class Create(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str = Field("", max_length=1000)
    public: bool = False


class Edit(BaseModel):
    ops: list[dict] = Field(min_length=1, max_length=200)


def _service_errors(fn):
    try:
        return fn()
    except ServiceError as e:
        raise err(502, f"{e.service}_error: {e.status} {e.message}") from e


@session_api.get("")
def web_overview(c: Ctx = Depends(ctx)):
    return overview(c)


@session_api.get("/export")
def web_export(c: Ctx = Depends(ctx)):
    body = json.dumps(export(c), ensure_ascii=False, indent=1)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="stash-playlists-{time.strftime("%Y-%m-%d")}.json"'})


@session_api.post("/accounts/{service}/connect")
def web_connect(service: str, body: Connect, c: Ctx = Depends(ctx)):
    return {"url": begin_connect(c, service, body.client_id, body.client_secret)}


@session_api.get("/accounts/{service}/callback", include_in_schema=False)
def web_callback(service: str, request: Request, code: str = "", state: str = "", error: str = "", c: Ctx = Depends(ctx)):
    if error or not code:
        return RedirectResponse(f"/#/playlists?error={clean_text(error or 'denied', 100)}", status_code=303)
    try:
        finish_connect(c, service, code, state)
    except ServiceError as e:
        return RedirectResponse(f"/#/playlists?error={clean_text(f'{e.status} {e.message}', 100)}", status_code=303)
    return RedirectResponse(f"/#/playlists?connected={service}", status_code=303)


@session_api.delete("/accounts/{service}")
def web_disconnect(service: str, c: Ctx = Depends(ctx)):
    return disconnect(c, service)


@session_api.post("/accounts/{service}/sync")
def web_sync(service: str, force: bool = False, c: Ctx = Depends(ctx)):
    return _service_errors(lambda: pull(c, service, None, force))


@session_api.post("/accounts/{service}/create")
def web_create(service: str, body: Create, c: Ctx = Depends(ctx)):
    return _service_errors(lambda: create(c, service, body.name, body.description, body.public))


@session_api.get("/lists/{playlist_id}")
def web_show(playlist_id: int, offset: int = 0, limit: int = 500, c: Ctx = Depends(ctx)):
    return show(c, playlist_id, offset, limit)


@session_api.post("/lists/{playlist_id}/edit")
def web_edit(playlist_id: int, body: Edit, c: Ctx = Depends(ctx)):
    return _service_errors(lambda: edit(c, playlist_id, body.ops))


@session_api.post("/lists/{playlist_id}/discard")
def web_discard(playlist_id: int, c: Ctx = Depends(ctx)):
    return _service_errors(lambda: discard(c, playlist_id))


@session_api.post("/lists/{playlist_id}/push")
def web_push(playlist_id: int, c: Ctx = Depends(ctx)):
    return _service_errors(lambda: push(c, playlist_id))


@session_api.post("/lists/{playlist_id}/sync")
def web_sync_one(playlist_id: int, force: bool = False, c: Ctx = Depends(ctx)):
    return _service_errors(lambda: pull(c, playlist(c, playlist_id)["service"], playlist_id, force))


@session_api.delete("/lists/{playlist_id}")
def web_forget(playlist_id: int, c: Ctx = Depends(ctx)):
    return forget(c, playlist_id)
