"""Subscriptions: YouTube channel subscriptions, and Spotify's followed artists and followed playlists, kept in
Stash.

The same model as playlists (app/playlists.py, whose connections, HTTP client and progress record this module
uses): a fetch copies the service's state into Stash, which is also a backup and an export. Subscribing and
unsubscribing are pending changes (state 'added' / 'removed') until a push sends them to the service. Tags and
notes are Stash's own and never leave it.

YouTube's API has no way to list or save other people's playlists that you have saved in YouTube's library.
Such playlists can be added here by address as 'local' entries: Stash keeps them and reads their title and
size, but they are not synchronised.

Quota (YouTube, 10 000 units a day by default): listing subscriptions costs 1 unit per 50, an activity check
1 unit per channel, subscribing or unsubscribing 50 units each.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Optional
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import db
from .main import Ctx, clean_text, ctx, err, norm_tags
from .playlists import (PROVIDERS, ServiceError, _api, _check, _iso_time, _progress, _progress_lock, _service,
                        _service_errors, _set_progress, account, problem_code, spotify_total)

KINDS = {"youtube": ("channel", "playlist"), "spotify": ("artist", "playlist", "album")}
MAX_SUBSCRIPTIONS = 5000
YT_WRITE_UNITS = 50

session_api = APIRouter(prefix="/api/subscriptions")


def _key(c: Ctx, service: str):
    return (c.uid, f"{service}:subscriptions")


def progress(c: Ctx, service: str) -> dict:
    with _progress_lock:
        p = _progress.get(_key(c, service))
        return dict(p) if p else {"running": False}


def _connected(c: Ctx, service: str):
    acc = account(c, service)
    if not acc or not acc["access_token"]:
        raise err(409, f"not_connected: connect {service} first on the Playlists page")
    return acc


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def _url(service: str, kind: str, external_id: str) -> str:
    if service == "youtube":
        return channel_url(external_id) if kind == "channel" else f"https://www.youtube.com/playlist?list={external_id}"
    return f"https://open.spotify.com/{kind}/{external_id}"


# --- reading the service ---------------------------------------------------------------------------------

def _remote_youtube(http, on_page) -> list[dict]:
    api, out, token = PROVIDERS["youtube"]["api"], [], None
    while len(out) < MAX_SUBSCRIPTIONS:
        params = {"part": "snippet,contentDetails", "mine": "true", "maxResults": 50, "order": "alphabetical"}
        if token:
            params["pageToken"] = token
        data = _check("youtube", http.get(f"{api}/subscriptions", params=params))
        for s in data.get("items", []):
            sn = s["snippet"]
            cid = sn.get("resourceId", {}).get("channelId", "")
            if cid:
                out.append({"kind": "channel", "external_id": cid, "remote_id": s["id"], "title": sn.get("title", ""),
                            "description": sn.get("description", "")[:1000], "owner": "",
                            "item_count": s.get("contentDetails", {}).get("totalItemCount"),
                            "subscribed_at": _iso_time(sn.get("publishedAt"))})
        on_page(len(out), data.get("pageInfo", {}).get("totalResults"))
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def _remote_spotify(http, acc, on_page) -> list[dict]:
    api, out = PROVIDERS["spotify"]["api"], []
    url, params = f"{api}/me/following", {"type": "artist", "limit": 50}
    while url and len(out) < MAX_SUBSCRIPTIONS:
        data = _check("spotify", http.get(url, params=params)).get("artists", {})
        params = None
        for a in data.get("items", []):
            out.append({"kind": "artist", "external_id": a["id"], "remote_id": "", "title": a.get("name", ""),
                        "description": ", ".join(a.get("genres", [])[:5]), "owner": "",
                        "item_count": a.get("followers", {}).get("total"), "subscribed_at": None})
        on_page(len(out), data.get("total"))
        url = data.get("next")
    # followed playlists are the ones in /me/playlists that someone else owns
    url, params = f"{api}/me/playlists", {"limit": 50}
    while url and len(out) < MAX_SUBSCRIPTIONS:
        data = _check("spotify", http.get(url, params=params))
        params = None
        for p in data.get("items", []):
            if p and p.get("owner", {}).get("id") != acc["external_id"]:
                out.append({"kind": "playlist", "external_id": p["id"], "remote_id": "", "title": p.get("name", ""),
                            "description": (p.get("description") or "")[:1000],
                            "owner": p.get("owner", {}).get("display_name") or p.get("owner", {}).get("id", ""),
                            "item_count": spotify_total(p), "subscribed_at": None})
        on_page(len(out), None)
        url = data.get("next")
    # saved albums (the library's Albums); user-library-read
    url, params = f"{api}/me/albums", {"limit": 50}
    while url and len(out) < MAX_SUBSCRIPTIONS:
        data = _check("spotify", http.get(url, params=params))
        params = None
        for x in data.get("items", []):
            a = x.get("album") or {}
            if not a.get("id"):
                continue
            out.append({"kind": "album", "external_id": a["id"], "remote_id": "", "title": a.get("name", ""),
                        "description": a.get("release_date", "")[:4],
                        "owner": ", ".join(ar.get("name", "") for ar in a.get("artists", [])[:3]),
                        "item_count": a.get("total_tracks"), "subscribed_at": _iso_time(x.get("added_at"))})
        on_page(len(out), None)
        url = data.get("next")
    return out


def pull(c: Ctx, service: str, *, track: bool = True) -> dict:
    """Copy the service's subscriptions into Stash. Entries with pending changes keep them; ones no longer on
    the service are marked gone (kept, with their tags and notes). Stash-only entries are left alone."""
    service = _service(service)
    acc = _connected(c, service)
    key = _key(c, service) if track else None
    if track:
        _set_progress(key, running=True, phase="fetch", done=0, total=0, name="", items=0)
    try:
        return _pull(c, service, acc, key)
    finally:
        if track:
            _set_progress(key, running=False)


def _pull(c: Ctx, service: str, acc, key) -> dict:
    con, t = c.con, db.now()

    def on_page(read, total):
        _set_progress(key, done=read, total=max(int(total or 0), read))

    with _api(c, service, key) as http:
        remote = _remote_youtube(http, on_page) if service == "youtube" else _remote_spotify(http, acc, on_page)
    seen, added, updated = set(), 0, 0
    with db.tx(con):
        for s in remote:
            seen.add((s["kind"], s["external_id"]))
            row = con.execute("SELECT * FROM subscriptions WHERE user_id=? AND service=? AND kind=? AND external_id=?",
                              (c.uid, service, s["kind"], s["external_id"])).fetchone()
            fields = (s["remote_id"], s["title"], s["description"], s["owner"], _url(service, s["kind"], s["external_id"]),
                      s["item_count"], s["subscribed_at"], t)
            if row:
                # a pending unsubscribe stays pending; a pending subscribe that the service now has is done
                state = row["state"] if row["state"] == "removed" else "synced"
                con.execute("UPDATE subscriptions SET remote_id=?, title=?, description=?, owner=?, url=?, item_count=?,"
                            " subscribed_at=COALESCE(?, subscribed_at), synced_at=?, state=?, gone=0 WHERE id=?",
                            (*fields, state, row["id"]))
                updated += 1
            else:
                con.execute("INSERT INTO subscriptions(user_id, service, kind, external_id, remote_id, title, description,"
                            " owner, url, item_count, subscribed_at, synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (c.uid, service, s["kind"], s["external_id"], *fields))
                added += 1
        gone = 0
        for r in con.execute("SELECT id, kind, external_id, state FROM subscriptions WHERE user_id=? AND service=?"
                             " AND state IN ('synced', 'removed')", (c.uid, service)).fetchall():
            if (r["kind"], r["external_id"]) not in seen:
                if r["state"] == "removed":   # unsubscribed elsewhere too: nothing left to push
                    con.execute("UPDATE subscriptions SET state='synced', gone=1 WHERE id=?", (r["id"],))
                else:
                    con.execute("UPDATE subscriptions SET gone=1 WHERE id=?", (r["id"],))
                gone += 1
    counts: dict[str, int] = {}
    for s in remote:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    return {"service": service, "total": len(remote), "new": added, "updated": updated, "gone": gone, "by_kind": counts}


def check_activity(c: Ctx, service: str, ids: list[int] | None = None) -> dict:
    """YouTube: the date of each channel's newest upload (1 quota unit per channel). Read from the channel's
    uploads playlist, whose id is the channel id with UU in place of UC."""
    service = _service(service)
    if service != "youtube":
        raise err(422, "activity_youtube_only: Spotify does not say when an artist last released something per call")
    _connected(c, service)
    where, args = "", []
    if ids:
        where = f" AND id IN ({db.marks(len(ids))})"
        args = ids
    rows = c.con.execute(f"SELECT id, external_id, title FROM subscriptions WHERE user_id=? AND service='youtube'"
                         f" AND kind='channel' AND state<>'local'{where} ORDER BY title COLLATE NOCASE", [c.uid, *args]).fetchall()
    key = _key(c, service)
    _set_progress(key, running=True, phase="activity", done=0, total=len(rows), name="", items=0)
    checked, failed, t = 0, [], db.now()
    api = PROVIDERS["youtube"]["api"]
    try:
        with _api(c, service, key) as http:
            for n, r in enumerate(rows):
                _set_progress(key, done=n, name=r["title"])
                uploads = "UU" + r["external_id"][2:] if r["external_id"].startswith("UC") else ""
                last = None
                if uploads:
                    try:
                        data = _check("youtube", http.get(f"{api}/playlistItems", params={
                            "part": "contentDetails", "playlistId": uploads, "maxResults": 1}))
                        items = data.get("items", [])
                        if items:
                            last = _iso_time(items[0].get("contentDetails", {}).get("videoPublishedAt"))
                    except ServiceError as e:
                        if e.status in (401, 429) or problem_code(c, e).startswith("youtube_api_disabled"):
                            raise
                        if e.status != 404:   # 404: a channel without uploads
                            failed.append({"name": r["title"], "error": f"{e.status} {e.message}"[:200]})
                            continue
                with db.tx(c.con):
                    c.con.execute("UPDATE subscriptions SET last_activity=?, activity_checked=? WHERE id=?",
                                  (last, t, r["id"]))
                checked += 1
    finally:
        _set_progress(key, running=False)
    return {"service": service, "total": len(rows), "checked": checked, "failed": failed}


# --- local changes ---------------------------------------------------------------------------------------------

_YT_CHANNEL = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YT_PLAYLIST = re.compile(r"^(PL|OL|FL|LL|UU|RD)[A-Za-z0-9_-]{10,}$")
_SP_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_target(service: str, text: str) -> tuple[str, str]:
    """(kind, id or handle) from an address, a URI, an id or a @handle; ('', '') when it is none of them.
    For a YouTube @handle or /user/ name the id is resolved later ('handle:@x', 'user:x')."""
    text = text.strip()
    if service == "youtube":
        if _YT_CHANNEL.match(text):
            return "channel", text
        if text.startswith("@") and len(text) > 1:
            return "channel", f"handle:{text}"
        if _YT_PLAYLIST.match(text):
            return "playlist", text
        try:
            u = urlsplit(text if "://" in text else "https://" + text)
        except ValueError:
            return "", ""
        host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        if host not in ("youtube.com", "music.youtube.com"):
            return "", ""
        parts = [p for p in u.path.split("/") if p]
        lst = parse_qs(u.query).get("list", [""])[0]
        if parts[:1] == ["playlist"] and lst:
            return "playlist", lst
        if parts[:1] == ["channel"] and len(parts) > 1 and _YT_CHANNEL.match(parts[1]):
            return "channel", parts[1]
        if parts and parts[0].startswith("@"):
            return "channel", f"handle:{parts[0]}"
        if parts[:1] in (["user"], ["c"]) and len(parts) > 1:
            return "channel", f"user:{parts[1]}"
        if lst:
            return "playlist", lst
        return "", ""
    m = re.match(r"^spotify:(artist|playlist|album):([A-Za-z0-9]{22})$", text)
    if m:
        return m.group(1), m.group(2)
    try:
        u = urlsplit(text if "://" in text else "https://" + text)
    except ValueError:
        return "", ""
    if (u.hostname or "").lower() != "open.spotify.com":
        return "", ""
    parts = [p for p in u.path.split("/") if p]
    for i, p in enumerate(parts[:-1]):
        if p in ("artist", "playlist", "album") and _SP_ID.match(parts[i + 1]):
            return p, parts[i + 1]
    return "", ""


def _resolve(http, service: str, kind: str, ref: str) -> Optional[dict]:
    """The id and title for an address the user gave; None when the service does not know it."""
    api = PROVIDERS[service]["api"]
    if service == "youtube" and kind == "channel":
        part = {"part": "snippet,contentDetails,statistics"}
        if ref.startswith("handle:"):
            tries = [{"forHandle": ref.split(":", 1)[1]}]
        elif ref.startswith("user:"):
            # /c/<name> and /user/<name> addresses: the custom name has usually become the channel's handle, and
            # the older ones are legacy usernames; each look-up is one quota unit
            name = ref.split(":", 1)[1]
            tries = [{"forHandle": f"@{name}"}, {"forUsername": name}]
        else:
            tries = [{"id": ref}]
        items = []
        for params in tries:
            items = _check(service, http.get(f"{api}/channels", params=part | params)).get("items", [])
            if items:
                break
        if not items:
            return None
        ch = items[0]
        return {"external_id": ch["id"], "title": ch["snippet"].get("title", ""),
                "description": ch["snippet"].get("description", "")[:1000], "owner": "",
                "item_count": int(ch.get("statistics", {}).get("videoCount") or 0) or None}
    if service == "youtube":
        items = _check(service, http.get(f"{api}/playlists", params={"part": "snippet,contentDetails", "id": ref})).get("items", [])
        if not items:
            return None
        p = items[0]
        return {"external_id": p["id"], "title": p["snippet"].get("title", ""),
                "description": p["snippet"].get("description", "")[:1000], "owner": p["snippet"].get("channelTitle", ""),
                "item_count": p.get("contentDetails", {}).get("itemCount")}
    if kind == "artist":
        a = _check(service, http.get(f"{api}/artists/{ref}"))
        return {"external_id": a["id"], "title": a.get("name", ""), "description": ", ".join(a.get("genres", [])[:5]),
                "owner": "", "item_count": a.get("followers", {}).get("total")}
    if kind == "album":
        a = _check(service, http.get(f"{api}/albums/{ref}"))
        return {"external_id": a["id"], "title": a.get("name", ""), "description": a.get("release_date", "")[:4],
                "owner": ", ".join(ar.get("name", "") for ar in a.get("artists", [])[:3]), "item_count": a.get("total_tracks")}
    p = _check(service, http.get(f"{api}/playlists/{ref}"))
    return {"external_id": p["id"], "title": p.get("name", ""), "description": (p.get("description") or "")[:1000],
            "owner": p.get("owner", {}).get("display_name") or p.get("owner", {}).get("id", ""),
            "item_count": spotify_total(p)}


def _own(c: Ctx, ids) -> list:
    ids = [int(i) for i in ids][:MAX_SUBSCRIPTIONS]
    if not ids:
        return []
    return c.con.execute(f"SELECT * FROM subscriptions WHERE user_id=? AND id IN ({db.marks(len(ids))})", [c.uid, *ids]).fetchall()


def edit(c: Ctx, service: str, ops: list[dict]) -> dict:
    """Pending and local changes. subscribe (addresses: on YouTube a channel, or a playlist kept only in Stash;
    on Spotify an artist or a playlist), unsubscribe (ids), keep (ids: undo a pending unsubscribe), tag (ids, add,
    remove), set_tags (ids, tags), note (id, note), forget (ids: drop Stash's copy, nothing on the service)."""
    service = _service(service)
    con = c.con
    counts = {"subscribed": 0, "kept_local": 0, "unsubscribed": 0, "kept": 0, "tagged": 0, "noted": 0, "forgotten": 0,
              "already": 0, "unknown": [], "not_found": []}
    for op in ops[:200]:
        kind = op.get("op")
        if kind == "subscribe":
            refs = [(str(x), *parse_target(service, str(x))) for x in (op.get("items") or [])[:200]]
            counts["unknown"] += [x for x, k, _ in refs if not k]
            refs = [(x, k, r) for x, k, r in refs if k]
            if not refs:
                continue
            _connected(c, service)
            with _api(c, service) as http:
                found = [(x, k, _resolve(http, service, k, r)) for x, k, r in refs]
            with db.tx(con):
                for x, k, info in found:
                    if not info:
                        counts["not_found"].append(x)
                        continue
                    row = con.execute("SELECT * FROM subscriptions WHERE user_id=? AND service=? AND kind=? AND external_id=?",
                                      (c.uid, service, k, info["external_id"])).fetchone()
                    stash_only = service == "youtube" and k == "playlist"
                    if row and row["state"] in ("synced", "local", "added") and not row["gone"]:
                        counts["already"] += 1
                        continue
                    state = "local" if stash_only else "added"
                    if row:
                        con.execute("UPDATE subscriptions SET state=?, gone=0 WHERE id=?",
                                    ("synced" if row["state"] == "removed" and not row["gone"] else state, row["id"]))
                    else:
                        con.execute("INSERT INTO subscriptions(user_id, service, kind, external_id, title, description, owner,"
                                    " url, item_count, state, subscribed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (c.uid, service, k, info["external_id"], info["title"], info["description"], info["owner"],
                                     _url(service, k, info["external_id"]), info["item_count"], state, db.now()))
                    counts["kept_local" if stash_only else "subscribed"] += 1
            continue
        with db.tx(con):
            if kind in ("unsubscribe", "keep", "forget"):
                for r in _own(c, op.get("ids", [])):
                    if r["service"] != service:
                        continue
                    if kind == "forget" or (kind == "unsubscribe" and r["state"] in ("added", "local")):
                        # a subscribe that never reached the service, or a Stash-only entry: just drop it
                        con.execute("DELETE FROM subscriptions WHERE id=?", (r["id"],))
                        counts["forgotten" if kind == "forget" else "unsubscribed"] += 1
                    elif kind == "unsubscribe" and r["state"] == "synced" and not r["gone"]:
                        con.execute("UPDATE subscriptions SET state='removed' WHERE id=?", (r["id"],))
                        counts["unsubscribed"] += 1
                    elif kind == "keep" and r["state"] == "removed":
                        con.execute("UPDATE subscriptions SET state='synced' WHERE id=?", (r["id"],))
                        counts["kept"] += 1
            elif kind in ("tag", "set_tags"):
                add, remove = norm_tags(op.get("add", op.get("tags", []))), set(norm_tags(op.get("remove", [])))
                for r in _own(c, op.get("ids", [])):
                    have = [] if kind == "set_tags" else json.loads(r["tags"] or "[]")
                    new = [x for x in have if x not in remove] + [x for x in add if x not in have]
                    con.execute("UPDATE subscriptions SET tags=? WHERE id=?", (json.dumps(new[:30], ensure_ascii=False), r["id"]))
                    counts["tagged"] += 1
            elif kind == "note":
                rows = _own(c, [op.get("id", 0)])
                if not rows:
                    raise err(404, "no_such_subscription")
                con.execute("UPDATE subscriptions SET note=? WHERE id=?",
                            (re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(op.get("note", "")))[:2000], rows[0]["id"]))
                counts["noted"] += 1
            else:
                raise err(422, f"bad_op: {kind}")
    return counts


# --- pushing to the service ------------------------------------------------------------------------------------

def pending(c: Ctx, service: str) -> dict:
    rows = c.con.execute("SELECT id, kind, title, state FROM subscriptions WHERE user_id=? AND service=?"
                         " AND state IN ('added', 'removed') ORDER BY title COLLATE NOCASE", (c.uid, _service(service))).fetchall()
    out = {"subscribe": [dict(r) for r in rows if r["state"] == "added"],
           "unsubscribe": [dict(r) for r in rows if r["state"] == "removed"]}
    out["quota_units"] = YT_WRITE_UNITS * (len(out["subscribe"]) + len(out["unsubscribe"])) if service == "youtube" else 0
    return out


def sync(c: Ctx, service: str, push: bool = False) -> dict:
    """Fetch, or with push=True first send the pending subscribes and unsubscribes and then fetch. A write that
    fails is listed and stays pending."""
    service = _service(service)
    _connected(c, service)
    key = _key(c, service)
    done, failed = [], []
    _set_progress(key, running=True, phase="push" if push else "fetch", done=0, total=0, name="", items=0)
    try:
        if push:
            rows = c.con.execute("SELECT * FROM subscriptions WHERE user_id=? AND service=? AND state IN ('added', 'removed')"
                                 " ORDER BY title COLLATE NOCASE", (c.uid, service)).fetchall()
            _set_progress(key, total=len(rows))
            api = PROVIDERS[service]["api"]
            with _api(c, service, key) as http:
                for n, r in enumerate(rows):
                    _set_progress(key, done=n, name=r["title"])
                    try:
                        _write(http, service, api, r)
                    except ServiceError as e:
                        if e.status in (401, 429) or problem_code(c, e).startswith(("youtube_api_disabled", "reconnect_needed")):
                            raise
                        failed.append({"name": r["title"], "action": "subscribe" if r["state"] == "added" else "unsubscribe",
                                       "error": f"{e.status} {e.message}"[:200]})
                        continue
                    with db.tx(c.con):
                        if r["state"] == "removed":
                            c.con.execute("UPDATE subscriptions SET state='synced', gone=1 WHERE id=?", (r["id"],))
                        else:
                            c.con.execute("UPDATE subscriptions SET state='synced' WHERE id=?", (r["id"],))
                    done.append({"name": r["title"], "action": "subscribe" if r["state"] == "added" else "unsubscribe"})
            _set_progress(key, phase="fetch", done=0, total=0, name="")
        fetched = _pull(c, service, account(c, service), key)
    finally:
        _set_progress(key, running=False)
    return fetched | {"pushed": done, "push_failed": failed}


def _write(http, service: str, api: str, r) -> None:
    if service == "youtube":
        if r["state"] == "added":
            _check(service, http.post(f"{api}/subscriptions", params={"part": "snippet"}, json={
                "snippet": {"resourceId": {"kind": "youtube#channel", "channelId": r["external_id"]}}}))
        elif r["remote_id"]:
            _check(service, http.delete(f"{api}/subscriptions", params={"id": r["remote_id"]}))
        return
    # Spotify, since February 2026: follow and unfollow through the generic library call with a URI. Its
    # documentation lists playlists but not artists; if Spotify refuses an artist, the summary says so and the
    # change stays pending.
    method = http.put if r["state"] == "added" else http.delete
    _check(service, method(f"{api}/me/library", params={"uris": f"spotify:{r['kind']}:{r['external_id']}"}))


# --- reading for the page and MCP -------------------------------------------------------------------------------

def item_json(r) -> dict:
    return {"id": r["id"], "service": r["service"], "kind": r["kind"], "external_id": r["external_id"], "title": r["title"],
            "description": r["description"], "owner": r["owner"], "url": r["url"], "item_count": r["item_count"],
            "subscribed_at": r["subscribed_at"], "last_activity": r["last_activity"],
            "activity_checked": r["activity_checked"], "state": r["state"], "gone": bool(r["gone"]),
            "tags": json.loads(r["tags"] or "[]"), "note": r["note"]}


def listing(c: Ctx, service: str, kind: str = "", text: str = "", tag: str = "", state: str = "",
            inactive_days: int = 0, sort: str = "title", offset: int = 0, limit: int = 1000) -> dict:
    service = _service(service)
    where, args = ["user_id=?", "service=?"], [c.uid, service]
    if kind:
        where.append("kind=?")
        args.append(kind)
    if state:
        where.append("gone=1" if state == "gone" else "state=? AND gone=0")
        if state != "gone":
            args.append(state)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE value=?)")
        args.append(tag.strip().lower())
    for word in text.casefold().split()[:10]:
        where.append("(lower(title) LIKE ? OR lower(description) LIKE ? OR lower(note) LIKE ? OR lower(owner) LIKE ? OR lower(tags) LIKE ?)")
        args += [f"%{word}%"] * 5
    if inactive_days > 0:
        where.append("kind='channel' AND activity_checked IS NOT NULL AND (last_activity IS NULL OR last_activity < ?)")
        args.append(db.now() - inactive_days * 86400)
    # quietest first: checked channels with no uploads at all, then the oldest last upload; unchecked ones last
    order = {"title": "title COLLATE NOCASE", "subscribed": "subscribed_at DESC",
             "activity": "activity_checked IS NULL, last_activity IS NOT NULL, last_activity",
             "size": "item_count DESC"}.get(sort, "title COLLATE NOCASE")
    sql = f"FROM subscriptions WHERE {' AND '.join(where)}"
    total = c.con.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
    rows = c.con.execute(f"SELECT * {sql} ORDER BY {order}, id LIMIT ? OFFSET ?",
                         [*args, max(1, min(MAX_SUBSCRIPTIONS, limit)), max(0, offset)]).fetchall()
    return {"total": total, "offset": max(0, offset), "items": [item_json(r) for r in rows]}


def overview(c: Ctx) -> dict:
    from .playlists import accounts

    rows = c.con.execute("SELECT service, kind, state, gone, COUNT(*) AS n FROM subscriptions WHERE user_id=?"
                         " GROUP BY service, kind, state, gone", (c.uid,)).fetchall()
    stats: dict = {}
    for r in rows:
        s = stats.setdefault(r["service"], {"by_kind": {}, "pending": 0, "local": 0, "gone": 0})
        if r["gone"]:
            s["gone"] += r["n"]
            continue
        s["by_kind"][r["kind"]] = s["by_kind"].get(r["kind"], 0) + r["n"]
        if r["state"] in ("added", "removed"):
            s["pending"] += r["n"]
        if r["state"] == "local":
            s["local"] += r["n"]
    tags: dict[str, int] = {}
    for r in c.con.execute("SELECT j.value AS tag, COUNT(*) AS n FROM subscriptions s, json_each(s.tags) j"
                           " WHERE s.user_id=? GROUP BY j.value ORDER BY n DESC, j.value", (c.uid,)):
        tags[r["tag"]] = r["n"]
    return {"accounts": accounts(c), "stats": stats, "tags": tags}


def export(c: Ctx) -> dict:
    rows = c.con.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY service, kind, title", (c.uid,)).fetchall()
    return {"exported_at": db.now(), "subscriptions": [item_json(r) for r in rows]}


# --- the web UI, with the session cookie ------------------------------------------------------------------------

class Edit(BaseModel):
    ops: list[dict] = Field(min_length=1, max_length=200)


class Activity(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=MAX_SUBSCRIPTIONS)


@session_api.get("")
def web_overview(c: Ctx = Depends(ctx)):
    return overview(c)


@session_api.get("/export")
def web_export(c: Ctx = Depends(ctx)):
    import time

    from fastapi.responses import Response

    body = json.dumps(export(c), ensure_ascii=False, indent=1)
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="stash-subscriptions-{time.strftime("%Y-%m-%d")}.json"'})


@session_api.get("/{service}")
def web_list(service: str, kind: str = "", q: str = "", tag: str = "", state: str = "", inactive_days: int = 0,
             sort: Literal["title", "subscribed", "activity", "size"] = "title", offset: int = 0, limit: int = 1000,
             c: Ctx = Depends(ctx)):
    return listing(c, service, kind, q, tag, state, inactive_days, sort, offset, limit)


@session_api.get("/{service}/pending")
def web_pending(service: str, c: Ctx = Depends(ctx)):
    return pending(c, service)


@session_api.get("/{service}/progress")
def web_progress(service: str, c: Ctx = Depends(ctx)):
    return progress(c, _service(service))


@session_api.post("/{service}/sync")
def web_sync(service: str, push: bool = False, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: sync(c, service, push))


@session_api.post("/{service}/activity")
def web_activity(service: str, body: Activity, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: check_activity(c, service, body.ids or None))


@session_api.post("/{service}/edit")
def web_edit(service: str, body: Edit, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: edit(c, service, body.ops))
