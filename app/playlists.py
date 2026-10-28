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
import threading
import time
from typing import Literal, Optional
from urllib.parse import parse_qs, quote, urlencode, urlsplit

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
        # user-follow-*, user-library-*: followed artists and playlists on the Subscriptions page
        # (app/subscriptions.py); following goes through /me/library since February 2026
        "scope": "playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private"
                 " user-follow-read user-follow-modify user-library-read user-library-modify",
    },
    "youtube": {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "api": "https://www.googleapis.com/youtube/v3",
        "scope": "https://www.googleapis.com/auth/youtube",
    },
}

TRANSPORT: httpx.BaseTransport | None = None     # tests put a MockTransport here

# fetches in progress, (user id, service) -> {running, done, total, name, items}; one server process
_progress: dict[tuple[int, str], dict] = {}
_progress_lock = threading.Lock()

session_api = APIRouter(prefix="/api/playlists")


class ServiceError(RuntimeError):
    def __init__(self, service: str, status: int, message: str):
        super().__init__(f"{service}: {status} {message}")
        self.service, self.status, self.message = service, status, message


# --- HTTP to the services -----------------------------------------------------------------------------

GET_TRIES = 3          # a read that times out or loses its connection is tried this many times in all
RETRY_PAUSE = 2.0      # seconds before the second try, doubled before the third
RATE_TRIES = 8         # answers of 429 Too Many Requests waited out in one call
YOUTUBE_WRITE_COST = 50   # quota units per write to YouTube (a video added or removed, a playlist made, renamed or deleted)
YOUTUBE_PUSH_MAX = 150    # writes one push may send at once: more would spend most of the day's 10 000 units
RATE_PAUSE = 5.0       # seconds to wait on a 429 without Retry-After, doubled each time
MAX_RATE_WAIT = 180    # a longer Retry-After ends the call with a message instead of a long silent wait
MIN_INTERVAL = {"spotify": 0.12}   # seconds between calls: a long fetch stays under Spotify's rolling limit


class _Client(httpx.Client):
    """httpx.Client for one service: GETs are retried after a timeout or a dropped connection, and a network
    failure that remains becomes a ServiceError, so a fetch notes it for that playlist and goes on instead of
    ending in a server error. Writes are not retried: they may have reached the service."""

    def __init__(self, service: str, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self._last = 0.0

    progress_key = None    # set by a fetch, so that a wait for the rate limit shows on the progress bar
    block_key = ""         # config key holding when a long rate-limit pause of this app ends (see _api)

    def _blocked_until(self) -> int:
        if not self.block_key:
            return 0
        con = db.connect()
        try:
            return int(db.get_config(con, self.block_key, "0") or 0)
        finally:
            con.close()

    def _block(self, until: int) -> None:
        if self.block_key:
            con = db.connect()
            try:
                db.set_config(con, self.block_key, str(until))
            finally:
                con.close()

    def _pace(self) -> None:
        gap = MIN_INTERVAL.get(self.service, 0)
        if gap:
            wait = self._last + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        # while the service has paused this app for a long time, send nothing: calls during the pause can make
        # it longer. The error carries the end of the pause for the page to show.
        until = self._blocked_until()
        if until > time.time():
            raise ServiceError(self.service, 429, f"rate_limited:{until}")
        tries = GET_TRIES if request.method == "GET" else 1
        attempt, limited = 0, 0
        while True:
            self._pace()
            try:
                response = super().send(request, **kwargs)
            except httpx.TransportError as e:
                attempt += 1
                if attempt >= tries:
                    kind = "timed out" if isinstance(e, httpx.TimeoutException) else "could not be reached"
                    raise ServiceError(self.service, 504, f"the service {kind} ({type(e).__name__})") from e
                time.sleep(RETRY_PAUSE * (2 ** (attempt - 1)))
                continue
            if response.status_code == 403 and self.service == "youtube" and b"quotaExceeded" in response.content:
                # the day's 10 000 quota units are spent: nothing more goes through before midnight Pacific
                # time, so the pause is recorded like a long rate limit and the scheduled tasks skip YouTube
                response.close()
                until = youtube_quota_reset()
                self._block(until)
                raise ServiceError(self.service, 429, f"rate_limited:{until}")
            if response.status_code != 429:
                return response
            # rate limited: the request was not carried out, so any method may be sent again after the wait
            # the service asks for (Retry-After, seconds); a wait longer than MAX_RATE_WAIT ends the call
            try:
                wait = max(1, int(float(response.headers.get("retry-after", "") or 0)) or RATE_PAUSE * (2 ** limited))
            except ValueError:
                wait = RATE_PAUSE * (2 ** limited)
            limited += 1
            if wait > MAX_RATE_WAIT or limited > RATE_TRIES:
                until = int(time.time()) + int(wait)
                self._block(until)
                raise ServiceError(self.service, 429, f"rate_limited:{until}")
            response.close()
            _set_progress(self.progress_key, waiting=wait)
            time.sleep(wait)
            _set_progress(self.progress_key, waiting=0)


def youtube_quota_reset() -> int:
    """When YouTube's daily quota is next reset: midnight Pacific time, as an epoch, with a few minutes' margin."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return int((now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0).timestamp())


def _http(service: str, headers: dict | None = None) -> httpx.Client:
    return _Client(service, timeout=httpx.Timeout(30, connect=10), headers=headers or {}, transport=TRANSPORT)


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
    with _http(service, headers) as http:
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


def _api(c: Ctx, service: str, key=None) -> httpx.Client:
    """An HTTP client for the user's connection. key: the progress record to show rate-limit waits on."""
    http = _http(service, {"Authorization": f"Bearer {_access_token(c, service)}", "Accept": "application/json"})
    http.progress_key = key
    # a service limits the developer app (client id), so the pause is recorded per app, across restarts
    http.block_key = f"rate_block:{service}:{account(c, service)['client_id']}"
    return http


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
    # one call per track: Spotify removed the several-tracks call (/tracks?ids=) for Development Mode apps in 2026
    out: dict[str, dict] = {}
    for tid in ids[:200]:
        t = _check("spotify", http.get(f"{PROVIDERS['spotify']['api']}/tracks/{tid}"))
        if t:
            out[t["id"]] = {"title": t["name"], "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                            "duration_ms": t.get("duration_ms", 0)}
    return out


def spotify_total(p: dict) -> int:
    """A playlist's size. Spotify renamed the playlist's `tracks` to `items` in February 2026."""
    box = p.get("items") if isinstance(p.get("items"), dict) else p.get("tracks")
    return int((box or {}).get("total") or 0)


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
                            "remote_count": spotify_total(p), "snapshot": p.get("snapshot_id", "")})
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


def _remote_items(http: httpx.Client, service: str, external_id: str, on_page=None) -> list[dict]:
    """The items of one playlist, page by page. on_page(read, total) is called after every page, for the
    progress bar: how many items of this playlist have been read, of how many the service says it has."""
    api = PROVIDERS[service]["api"]
    out: list[dict] = []
    read = 0
    if service == "spotify":
        # /items and `item` since February 2026 (formerly /tracks and `track`); readable only for playlists the
        # user owns or collaborates on
        url, params = f"{api}/playlists/{external_id}/items", {"limit": 100}
        while url and len(out) < MAX_ITEMS:
            data = _check(service, http.get(url, params=params))
            params = None
            for it in data.get("items", []):
                read += 1
                t = it.get("item") if isinstance(it.get("item"), dict) else it.get("track")
                t = t if isinstance(t, dict) else {}
                if not t.get("id") or t.get("type", "track") != "track":
                    continue   # local files, unavailable tracks and podcast episodes cannot be written back
                out.append({"item_id": t["id"], "remote_id": "", "title": t.get("name", ""),
                            "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                            "duration_ms": t.get("duration_ms", 0), "added_at": _iso_time(it.get("added_at"))})
            if on_page:
                on_page(read, data.get("total"))
            url = data.get("next")
        return out
    token = None
    while len(out) < MAX_ITEMS:
        params = {"part": "snippet", "playlistId": external_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _check(service, http.get(f"{api}/playlistItems", params=params))
        for it in data.get("items", []):
            read += 1
            sn = it["snippet"]
            out.append({"item_id": sn.get("resourceId", {}).get("videoId", ""), "remote_id": it["id"],
                        "title": sn.get("title", ""), "artist": sn.get("videoOwnerChannelTitle", ""),
                        "duration_ms": 0, "added_at": _iso_time(sn.get("publishedAt"))})
        if on_page:
            on_page(read, data.get("pageInfo", {}).get("totalResults"))
        token = data.get("nextPageToken")
        if not token:
            break
    # the video's own record is the authority for its title and channel: a playlist item's snippet can lag
    # behind a renamed video. A video that videos.list no longer returns is deleted or private: it is marked
    # unavailable, and _store_items keeps the last real title Stash saw for it.
    videos = _youtube_videos(http, [x["item_id"] for x in out if x["item_id"]])
    for x in out:
        v = videos.get(x["item_id"])
        if v:
            x["title"], x["duration_ms"] = v["title"] or x["title"], v["duration_ms"]
            x["artist"] = v["artist"] or x["artist"]
            x["unavailable"] = 0
        else:
            x["duration_ms"], x["unavailable"] = 0, 1
    return [x for x in out if x["item_id"]]


def _iso_time(value) -> Optional[int]:
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone
    except ValueError:
        return None


def _store_items(con, playlist_id: int, items: list[dict]) -> None:
    """Replace the playlist's items with the service's. For an item that has become unavailable (a deleted or
    private video), the last title and channel Stash had for it are kept instead of YouTube's placeholder."""
    # an unavailable item's stored title is already the last real one, so it counts too
    known = {r["item_id"]: (r["title"], r["artist"]) for r in con.execute(
        "SELECT item_id, title, artist FROM playlist_items WHERE playlist_id=?", (playlist_id,))}
    for x in items:
        if x.get("unavailable") and x["item_id"] in known:
            x["title"], x["artist"] = known[x["item_id"]]
    con.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    con.executemany(
        "INSERT INTO playlist_items(playlist_id, position, remote_position, item_id, remote_id, title, artist,"
        " duration_ms, url, added_at, state, unavailable) VALUES(?,?,?,?,?,?,?,?,?,?,'synced',?)",
        [(playlist_id, i, i, x["item_id"], x["remote_id"], x["title"][:500], x["artist"][:500], x["duration_ms"],
          x.get("url") or "", x["added_at"], int(x.get("unavailable", 0))) for i, x in enumerate(items)])


def progress(c: Ctx, service: str) -> dict:
    """How far a running fetch of this account's playlists is, for the page's progress bar."""
    with _progress_lock:
        p = _progress.get((c.uid, service))
        return dict(p) if p else {"running": False}


def _set_progress(key, **fields) -> None:
    """key None: the caller keeps its own record (sync_all pushing one playlist), so nothing is written."""
    if key is None:
        return
    with _progress_lock:
        if fields.get("running") is False:
            _progress.pop(key, None)
        else:
            # done/total: playlists; list_read/list_total: items of the playlist being read; items: all so far
            _progress.setdefault(key, {"running": True, "phase": "fetch", "done": 0, "total": 0, "name": "",
                                       "items": 0, "list_read": 0, "list_total": 0}).update(fields)


def pull(c: Ctx, service: str, playlist_id: Optional[int] = None, force: bool = False, *, track: bool = True,
         budget: Optional[int] = None) -> dict:
    """Fetch the playlists (or one) and their items. A playlist with pending local changes is left alone
    unless force=True, which discards them. track=False leaves the progress record to the caller (sync_all)."""
    service = _service(service)
    acc = account(c, service)
    if not acc or not acc["access_token"]:
        raise err(409, f"not_connected: connect {service} first on the Playlists page")
    key = (c.uid, service)
    if playlist_id is not None and playlist(c, playlist_id)["local"]:
        return refresh_local(c, playlist_id)
    if not track:
        return _pull(c, service, acc, None, playlist_id, force, budget)
    _set_progress(key, running=True, phase="fetch", done=0, total=0, name="", items=0)
    try:
        return _pull(c, service, acc, key, playlist_id, force, budget)
    finally:
        _set_progress(key, running=False)


def _pull(c: Ctx, service: str, acc, key, playlist_id: Optional[int], force: bool, budget: Optional[int] = None) -> dict:
    """budget: read the contents of at most this many playlists (the scheduled fetch); the rest wait for the next run."""
    con, t = c.con, db.now()
    fetched, skipped, failed, item_count, metadata_only, unchanged, deferred, reads = 0, [], [], 0, [], [], [], 0
    with _api(c, service, key) as http:
        if playlist_id is not None:
            row = playlist(c, playlist_id)
            wanted = [{"external_id": row["external_id"], "name": row["name"], "description": row["description"],
                       "public": row["public"], "owner": row["owner"], "editable": row["editable"],
                       "remote_count": row["remote_count"], "snapshot": row["snapshot"]}]
            if service == "spotify":   # the one playlist's own metadata
                p = _check(service, http.get(f"{PROVIDERS[service]['api']}/playlists/{row['external_id']}"))
                wanted[0] |= {"name": p.get("name", ""), "description": p.get("description") or "",
                              "public": int(bool(p.get("public"))), "snapshot": p.get("snapshot_id", ""),
                              "remote_count": spotify_total(p) or row["remote_count"]}
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
        _set_progress(key, total=len(wanted))
        for n, p in enumerate(wanted):
            seen.add(p["external_id"])
            _set_progress(key, done=n, name=p["name"], list_read=0, list_total=int(p.get("remote_count") or 0))
            row = con.execute("SELECT * FROM playlists WHERE user_id=? AND service=? AND external_id=?",
                              (c.uid, service, p["external_id"])).fetchone()
            if row and row["dirty"] and not force:
                skipped.append(row["name"])
                continue
            if (service == "spotify" and p["editable"] and row and not force and row["snapshot"]
                    and row["snapshot"] == p["snapshot"] and row["synced_at"]):
                # Spotify's snapshot_id changes whenever a playlist changes: an unchanged one is not read again,
                # which keeps an ordinary fetch to a few calls instead of hundreds (Spotify pauses apps that
                # make too many)
                with db.tx(con):
                    con.execute("UPDATE playlists SET name=?, description=?, public=?, synced_at=?, gone=0 WHERE id=?",
                                (p["name"], p["description"], p["public"], t, row["id"]))
                unchanged.append(p["name"])
                n_items = con.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (row["id"],)).fetchone()[0]
                item_count += n_items
                fetched += 1
                continue
            if service == "spotify" and not p["editable"]:
                # Spotify gives Development Mode apps no contents of playlists that someone else owns (2026):
                # keep the name, owner and size, and whatever items an earlier fetch stored
                with db.tx(con):
                    if row:
                        con.execute("UPDATE playlists SET name=?, description=?, public=?, editable=0, owner=?, remote_count=?,"
                                    " snapshot=?, synced_at=?, gone=0 WHERE id=?",
                                    (p["name"], p["description"], p["public"], p["owner"], p["remote_count"], p["snapshot"],
                                     t, row["id"]))
                    else:
                        con.execute("INSERT INTO playlists(user_id, service, external_id, name, description, public, editable,"
                                    " owner, remote_count, snapshot, synced_at) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
                                    (c.uid, service, p["external_id"], p["name"], p["description"], p["public"], p["owner"],
                                     p["remote_count"], p["snapshot"], t))
                metadata_only.append(p["name"])
                continue

            if budget is not None and reads >= budget:
                deferred.append(p["name"])   # left as it was, read on a later run
                continue
            reads += 1

            def on_page(read, total, before=item_count, fallback=int(p.get("remote_count") or 0)):
                _set_progress(key, list_read=read, list_total=max(int(total or fallback), read), items=before + read)

            try:
                items = _remote_items(http, service, p["external_id"], on_page)
            except ServiceError as e:
                # one playlist the service will not give (deleted meanwhile, a block): note it and go on;
                # a dead token or a disabled API fails every playlist, so that ends the fetch
                if e.status in (401, 429) or problem_code(c, e).startswith("youtube_api_disabled") or playlist_id is not None:
                    raise
                failed.append({"name": p["name"], "error": f"{e.status} {e.message}"[:200]})
                continue
            item_count += len(items)
            _set_progress(key, items=item_count)
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
            with db.tx(con):   # playlists no longer on the service stay as a copy, marked gone (not Stash-only ones)
                for r in con.execute("SELECT id, external_id FROM playlists WHERE user_id=? AND service=? AND local=0",
                                     (c.uid, service)).fetchall():
                    con.execute("UPDATE playlists SET gone=? WHERE id=?", (int(r["external_id"] not in seen), r["id"]))
                con.execute("UPDATE playlist_accounts SET synced_at=? WHERE id=?", (t, acc["id"]))
    return {"service": service, "total": len(wanted), "fetched": fetched, "items": item_count,
            "skipped_with_local_changes": skipped, "failed": failed, "metadata_only": metadata_only,
            "unchanged": len(unchanged), "deferred": deferred}


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
            "gone": bool(r["gone"]), "local": bool(r["local"]), "deleting": bool(r["deleting"]),
            "url": "" if r["local"] else (f"https://open.spotify.com/playlist/{r['external_id']}" if r["service"] == "spotify"
                                          else f"https://www.youtube.com/playlist?list={r['external_id']}")}


def overview(c: Ctx) -> dict:
    rows = c.con.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id AND i.state<>'removed') AS count,"
        " (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id AND i.state<>'synced') AS pending"
        " FROM playlists p WHERE p.user_id=? ORDER BY p.service, p.gone, p.deleting, p.name COLLATE NOCASE", (c.uid,)).fetchall()
    return {"accounts": accounts(c),
            "playlists": [playlist_json(r) | {"count": r["count"], "pending": r["pending"]} for r in rows]}


def item_json(r) -> dict:
    return {"id": r["id"], "position": r["position"], "item_id": r["item_id"], "title": r["title"], "artist": r["artist"],
            "duration_ms": r["duration_ms"], "url": r["url"], "added_at": r["added_at"], "state": r["state"],
            "unavailable": bool(r["unavailable"]),
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
        if row["local"]:
            # a Stash-only playlist has no service to wait for: the change is the new state at once
            con.execute("DELETE FROM playlist_items WHERE playlist_id=? AND state='removed'", (playlist_id,))
            _renumber(con, playlist_id)
            con.execute("UPDATE playlist_items SET state='synced', remote_position=position WHERE playlist_id=?", (playlist_id,))
            con.execute("UPDATE playlists SET dirty=0 WHERE id=?", (playlist_id,))
        else:
            _mark_dirty(con, playlist_id)
    return counts


def discard(c: Ctx, playlist_id: int) -> dict:
    """Drop the pending changes: back to what was last fetched from the service."""
    if playlist(c, playlist_id)["local"]:
        raise err(409, "local_playlist: changes to a playlist kept only in Stash take effect at once")
    con = c.con
    with db.tx(con):
        con.execute("DELETE FROM playlist_items WHERE playlist_id=? AND state='added'", (playlist_id,))
        con.execute("UPDATE playlist_items SET state='synced', position=remote_position WHERE playlist_id=?", (playlist_id,))
        con.execute("UPDATE playlists SET dirty=0 WHERE id=?", (playlist_id,))
        _renumber(con, playlist_id)
    return pull(c, playlist(c, playlist_id)["service"], playlist_id, force=True)


# --- pushing to the service ----------------------------------------------------------------------------------

def sync_all(c: Ctx, service: str) -> dict:
    """Both ways: push every playlist with pending changes to the service, then fetch everything back. A push
    that fails is reported and its changes stay pending; the fetch then leaves that playlist alone."""
    service = _service(service)
    acc = account(c, service)
    if not acc or not acc["access_token"]:
        raise err(409, f"not_connected: connect {service} first on the Playlists page")
    key = (c.uid, service)
    dirty = c.con.execute("SELECT id, name FROM playlists WHERE user_id=? AND service=? AND (dirty>0 OR deleting=1)"
                          " AND editable=1 AND gone=0 ORDER BY name COLLATE NOCASE", (c.uid, service)).fetchall()
    pushed, push_failed, deleted = [], [], []
    _set_progress(key, running=True, phase="push", done=0, total=len(dirty), name="", items=0)
    try:
        for n, row in enumerate(dirty):
            _set_progress(key, done=n, name=row["name"])
            try:
                r = push(c, row["id"], track=False)
                (deleted if r.get("deleted") else pushed).append(row["name"])
            except ServiceError as e:
                if e.status in (401, 429) or problem_code(c, e).startswith("youtube_api_disabled"):
                    raise
                push_failed.append({"name": row["name"], "error": f"{e.status} {e.message}"[:200]})
        _set_progress(key, phase="fetch", done=0, total=0, name="", items=0)
        fetched = _pull(c, service, acc, key, None, False)
    finally:
        _set_progress(key, running=False)
    return fetched | {"pushed": pushed, "push_failed": push_failed, "deleted": deleted}


def mark_delete(c: Ctx, playlist_id: int, keep: bool = False) -> dict:
    """Mark a playlist to be deleted from the service at the next push or synchronise, or take the mark back
    (keep=True). Nothing is sent until then. A Stash-only playlist has nothing on the service: it is forgotten at
    once."""
    row = playlist(c, playlist_id)
    if row["local"]:
        return {"id": playlist_id, "deleting": False} if keep else forget(c, playlist_id) | {"deleting": False}
    if not row["editable"]:
        raise err(409, "not_editable: this playlist belongs to someone else")
    with db.tx(c.con):
        c.con.execute("UPDATE playlists SET deleting=? WHERE id=?", (0 if keep else 1, playlist_id))
    return {"id": playlist_id, "name": row["name"], "deleting": not keep}


def _delete_remote(c: Ctx, row) -> dict:
    """A playlist marked for deletion: gone from the service (YouTube deletes it; on Spotify a playlist is
    "deleted" by unfollowing it, through /me/library since February 2026), then from Stash."""
    service, api = row["service"], PROVIDERS[row["service"]]["api"]
    with _api(c, service) as http:
        if service == "spotify":
            _check(service, http.delete(f"{api}/me/library", params={"uris": f"spotify:playlist:{row['external_id']}"}))
        else:
            _check(service, http.delete(f"{api}/playlists", params={"id": row["external_id"]}))
    with db.tx(c.con):
        c.con.execute("DELETE FROM playlists WHERE id=?", (row["id"],))
    return {"deleted": row["name"], "pushed": {"added": 0, "removed": 0, "calls": 1}, "fetched": 0}


def push(c: Ctx, playlist_id: int, *, track: bool = True, budget: Optional[int] = None) -> dict:
    """Make the service's playlist match the local one (items, order, name), then fetch it back. A playlist
    marked for deletion is deleted from the service instead. YouTube charges 50 quota units per write, so a
    push of more than YOUTUBE_PUSH_MAX writes is refused unless a budget is given: then at most that many
    writes are sent now and the rest stay pending for the next run (the scheduled youtube-push task)."""
    row = playlist(c, playlist_id)
    if row["local"]:
        raise err(409, "local_playlist: this playlist is kept only in Stash and has nothing to push to")
    if not row["editable"]:
        raise err(409, "not_editable: this playlist belongs to someone else")
    if row["deleting"]:
        return _delete_remote(c, row)
    service, api = row["service"], PROVIDERS[row["service"]]["api"]
    con = c.con
    items = con.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (playlist_id,)).fetchall()
    if service == "youtube":
        writes = (sum(1 for r in items if r["state"] == "removed" and r["remote_id"])
                  + sum(1 for r in items if r["state"] == "added") + int(row["dirty"] == 2))
        if budget is not None:
            return _push_youtube_batch(c, row, items, budget, track)
        if writes > YOUTUBE_PUSH_MAX:
            raise err(409, f"too_many_writes: {writes} writes would cost {writes * YOUTUBE_WRITE_COST} of YouTube's 10 000"
                           f" daily quota units; the scheduled task youtube-push sends them in daily batches")
    elif budget is not None:
        return _push_spotify_batch(c, row, items, budget, track)
    desired = [r for r in items if r["state"] != "removed"]
    changes = {"added": sum(r["state"] == "added" for r in items), "removed": sum(r["state"] == "removed" for r in items),
               "calls": 0}
    with _api(c, service) as http:
        if service == "spotify":
            uris = [f"spotify:track:{r['item_id']}" for r in desired]
            _check(service, http.put(f"{api}/playlists/{row['external_id']}/items", json={"uris": uris[:100]}))
            changes["calls"] += 1
            for part in db.chunks(uris[100:], 100):
                _check(service, http.post(f"{api}/playlists/{row['external_id']}/items", json={"uris": part}))
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
    pulled = pull(c, service, playlist_id, force=True, track=track)
    return {"pushed": changes, "fetched": pulled["fetched"]}


def _push_youtube_batch(c: Ctx, row, items, budget: int, track: bool) -> dict:
    """Part of a YouTube push: up to `budget` writes, each recorded in Stash as soon as it is done, so that a
    run cut short (the quota, a failure) loses nothing. First the rename, then the removals, then the
    additions in order, each at the end of the playlist: the order on YouTube is the order of addition, and the
    reordering a full push does is left out. A video YouTube refuses (deleted, private: a 4xx for that video)
    is dropped from the list and reported; a passing failure (5xx) ends the batch with the video still pending.
    When nothing is left pending, the playlist is fetched back."""
    api, con, pid = PROVIDERS["youtube"]["api"], c.con, row["id"]
    done, failed = {"added": 0, "removed": 0, "calls": 0}, []
    with _api(c, "youtube") as http:
        if row["dirty"] == 2 and done["calls"] < budget:
            _check("youtube", http.put(f"{api}/playlists", params={"part": "snippet,status"}, json={
                "id": row["external_id"], "snippet": {"title": row["name"], "description": row["description"]},
                "status": {"privacyStatus": "public" if row["public"] else "private"}}))
            done["calls"] += 1
            with db.tx(con):
                con.execute("UPDATE playlists SET dirty=1 WHERE id=?", (pid,))
        for r in items:
            if r["state"] != "removed":
                continue
            if r["remote_id"]:
                if done["calls"] >= budget:
                    break
                _check("youtube", http.delete(f"{api}/playlistItems", params={"id": r["remote_id"]}))
                done["calls"] += 1
            with db.tx(con):
                con.execute("DELETE FROM playlist_items WHERE id=?", (r["id"],))
            done["removed"] += 1
        for r in items:
            if r["state"] != "added":
                continue
            if done["calls"] >= budget:
                break
            try:
                made = _check("youtube", http.post(f"{api}/playlistItems", params={"part": "snippet"}, json={
                    "snippet": {"playlistId": row["external_id"],
                                "resourceId": {"kind": "youtube#video", "videoId": r["item_id"]}}}))
            except ServiceError as e:
                # only YouTube's own refusal of the video (not found, private, forbidden) drops it; a dead token,
                # the quota, a disabled API or a passing 5xx ends the batch and the video stays pending
                if e.status >= 500 or e.status in (401, 429) or problem_code(c, e).startswith("youtube_api_disabled"):
                    raise
                done["calls"] += 1
                failed.append({"title": r["title"], "error": f"{e.status} {e.message}"[:200]})
                with db.tx(con):
                    con.execute("DELETE FROM playlist_items WHERE id=?", (r["id"],))
                continue
            done["calls"] += 1
            with db.tx(con):
                con.execute("UPDATE playlist_items SET state='synced', remote_id=?, remote_position=position WHERE id=?",
                            (made.get("id", f"new-{r['id']}"), r["id"]))
            done["added"] += 1
    remaining = con.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND state<>'synced'", (pid,)).fetchone()[0]
    remaining += int(con.execute("SELECT dirty FROM playlists WHERE id=?", (pid,)).fetchone()[0] == 2)
    if remaining:
        return {"pushed": done, "failed": failed, "remaining": remaining, "complete": False, "fetched": 0}
    with db.tx(con):
        con.execute("UPDATE playlists SET dirty=0 WHERE id=?", (pid,))
        _renumber(con, pid)
    pulled = pull(c, "youtube", pid, force=True, track=track)
    return {"pushed": done, "failed": failed, "remaining": 0, "complete": True, "fetched": pulled["fetched"]}


def _push_spotify_batch(c: Ctx, row, items, budget: int, track: bool) -> dict:
    """Part of a Spotify push, at most `budget` calls: the rename, then removals and additions a hundred items
    a call, each call recorded in Stash as soon as it is done. Additions go to the end in order; the reordering
    of a full push is left out. When nothing is left pending, the playlist is fetched back. Spotify has no
    daily quota but pauses apps that call too fast, so the scheduled spotify-push keeps to a few calls a day."""
    api, con, pid, ext = PROVIDERS["spotify"]["api"], c.con, row["id"], row["external_id"]
    done = {"added": 0, "removed": 0, "calls": 0}
    with _api(c, "spotify") as http:
        if row["dirty"] == 2 and done["calls"] < budget:
            _check("spotify", http.put(f"{api}/playlists/{ext}",
                                       json={"name": row["name"], "description": row["description"][:300], "public": bool(row["public"])}))
            done["calls"] += 1
            with db.tx(con):
                con.execute("UPDATE playlists SET dirty=1 WHERE id=?", (pid,))
        removed = [r for r in items if r["state"] == "removed"]
        for part in db.chunks(removed, 100):
            if done["calls"] >= budget:
                break
            uris = [f"spotify:track:{r['item_id']}" for r in part]
            _check("spotify", http.request("DELETE", f"{api}/playlists/{ext}/items", json={"uris": uris}))
            done["calls"] += 1
            with db.tx(con):
                con.executemany("DELETE FROM playlist_items WHERE id=?", [(r["id"],) for r in part])
            done["removed"] += len(part)
        added = [r for r in items if r["state"] == "added"]
        for part in db.chunks(added, 100):
            if done["calls"] >= budget:
                break
            uris = [f"spotify:track:{r['item_id']}" for r in part]
            _check("spotify", http.post(f"{api}/playlists/{ext}/items", json={"uris": uris}))
            done["calls"] += 1
            with db.tx(con):
                con.executemany("UPDATE playlist_items SET state='synced', remote_position=position WHERE id=?", [(r["id"],) for r in part])
            done["added"] += len(part)
    remaining = con.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND state<>'synced'", (pid,)).fetchone()[0]
    remaining += int(con.execute("SELECT dirty FROM playlists WHERE id=?", (pid,)).fetchone()[0] == 2)
    if remaining:
        return {"pushed": done, "failed": [], "remaining": remaining, "complete": False, "fetched": 0}
    with db.tx(con):
        con.execute("UPDATE playlists SET dirty=0 WHERE id=?", (pid,))
        _renumber(con, pid)
    pulled = pull(c, "spotify", pid, force=True, track=track)
    return {"pushed": done, "failed": [], "remaining": 0, "complete": True, "fetched": pulled["fetched"]}


def publish(c: Ctx, playlist_id: int) -> dict:
    """A Stash-only playlist becomes one on its service: made there now (empty, private unless marked public),
    its items pending additions that a push sends: the scheduled youtube-push and spotify-push tasks do so in
    daily batches within the services' limits."""
    row = playlist(c, playlist_id)
    if not row["local"]:
        raise err(409, "not_local: this playlist is already on the service")
    service = row["service"]
    acc = account(c, service)
    if not acc or not acc["access_token"]:
        raise err(409, f"not_connected: connect {service} first on the Playlists page")
    api = PROVIDERS[service]["api"]
    with _api(c, service) as http:
        if service == "spotify":
            made = _check(service, http.post(f"{api}/me/playlists", json={
                "name": row["name"], "description": row["description"][:300], "public": bool(row["public"])}))
        else:
            made = _check(service, http.post(f"{api}/playlists", params={"part": "snippet,status"}, json={
                "snippet": {"title": row["name"], "description": row["description"][:1000]},
                "status": {"privacyStatus": "public" if row["public"] else "private"}}))
    con = c.con
    with db.tx(con):
        con.execute("UPDATE playlists SET external_id=?, local=0, dirty=1, owner=?, remote_count=0, snapshot='', synced_at=?"
                    " WHERE id=?", (made["id"], acc["external_id"], db.now(), playlist_id))
        con.execute("UPDATE playlist_items SET state='added', remote_id='', remote_position=NULL WHERE playlist_id=?", (playlist_id,))
    pending = con.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
    return {"id": playlist_id, "external_id": made["id"], "name": row["name"], "pending": pending}


def adopt(c: Ctx, local_id: int, into_id: int) -> dict:
    """Publish a Stash-only YouTube playlist by turning an existing YouTube playlist into it: that one is
    renamed, its videos the Stash-only list does not have are marked removed, the missing ones are added at
    the end, and the Stash-only row goes. Where the two overlap a lot this costs far fewer of YouTube's quota
    units than a fresh publish (each write is 50); nothing is sent until the push."""
    new, old = playlist(c, local_id), playlist(c, into_id)
    if not new["local"] or old["local"]:
        raise err(409, "adopt_needs_local_and_remote: the first playlist must be Stash-only, the second one on YouTube")
    if new["service"] != "youtube" or old["service"] != "youtube":
        raise err(409, "youtube_only")
    if not old["editable"] or old["gone"]:
        raise err(409, "not_editable: the YouTube playlist is someone else's or gone")
    con = c.con
    with db.tx(con):
        wanted = con.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (local_id,)).fetchall()
        want_ids = {r["item_id"] for r in wanted}
        have = con.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (into_id,)).fetchall()
        kept, removed = set(), 0
        for r in have:
            if r["state"] == "added":          # a pending addition of the old list: not part of the new one
                con.execute("DELETE FROM playlist_items WHERE id=?", (r["id"],))
            elif r["item_id"] in want_ids and r["item_id"] not in kept:
                kept.add(r["item_id"])
                con.execute("UPDATE playlist_items SET state='synced' WHERE id=?", (r["id"],))
            else:                              # not wanted, or a repeat of one already kept
                con.execute("UPDATE playlist_items SET state='removed' WHERE id=?", (r["id"],))
                removed += 1
        added = 0
        for r in wanted:
            if r["item_id"] in kept:
                continue
            kept.add(r["item_id"])
            con.execute(
                "INSERT INTO playlist_items(playlist_id, position, remote_position, item_id, remote_id, title, artist,"
                " duration_ms, url, added_at, state, unavailable) VALUES(?,?,NULL,?,'',?,?,?,?,?,'added',?)",
                (into_id, len(have) + added, r["item_id"], r["title"], r["artist"], r["duration_ms"], r["url"],
                 r["added_at"], r["unavailable"]))
            added += 1
        con.execute("UPDATE playlists SET name=?, description=?, public=?, dirty=2, deleting=0 WHERE id=?",
                    (new["name"], new["description"], new["public"], into_id))
        con.execute("DELETE FROM playlists WHERE id=?", (local_id,))
        _renumber(con, into_id)
    return {"id": into_id, "name": new["name"], "kept": len(kept) - added, "removed": removed, "added": added}


def push_batch(c: Ctx, budget: int, service: str = "youtube") -> dict:
    """The scheduled youtube-push and spotify-push: the pending changes of every playlist of the service,
    then, once nothing is pending any more, the deletions of the playlists marked for it; at most `budget`
    calls in all (on YouTube every call is a write of 50 quota units)."""
    con = c.con
    service = _service(service)
    out = {"writes": 0, "pushed": [], "completed": [], "deleted": [], "failed": [], "remaining": 0}
    rows = con.execute("SELECT id, name FROM playlists WHERE user_id=? AND service=? AND dirty>0 AND editable=1"
                       " AND gone=0 AND local=0 AND deleting=0 ORDER BY name COLLATE NOCASE", (c.uid, service)).fetchall()
    for row in rows:
        if out["writes"] >= budget:
            break
        r = push(c, row["id"], track=False, budget=budget - out["writes"])
        out["writes"] += r["pushed"]["calls"]
        out["pushed"].append(row["name"])
        out["failed"] += [{"playlist": row["name"], **f} for f in r["failed"]]
        if r["complete"]:
            out["completed"].append(row["name"])
    out["remaining"] = con.execute(
        "SELECT COUNT(*) FROM playlist_items i JOIN playlists p ON p.id=i.playlist_id WHERE p.user_id=? AND p.service=?"
        " AND p.local=0 AND p.deleting=0 AND p.gone=0 AND p.editable=1 AND i.state<>'synced'", (c.uid, service)).fetchone()[0]
    if out["remaining"] == 0:
        for row in con.execute("SELECT id, name FROM playlists WHERE user_id=? AND service=? AND deleting=1 AND local=0"
                               " AND editable=1 ORDER BY name COLLATE NOCASE", (c.uid, service)).fetchall():
            if out["writes"] >= budget:
                break
            push(c, row["id"], track=False)
            out["writes"] += 1
            out["deleted"].append(row["name"])
    return out


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
            # POST /me/playlists: /users/{id}/playlists was removed for Development Mode apps in February 2026
            made = _check(service, http.post(f"{api}/me/playlists",
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


# --- Google Takeout: YouTube lists the API does not give (Watch later) ---------------------------------------
#
# YouTube's API has returned nothing for Watch later (and history) since 2016, but Google Takeout exports it,
# with every other playlist, as one CSV file per playlist under "YouTube and YouTube Music/playlists/". Older
# exports start with a few lines about the playlist before the header "Video Id,Time Added"; newer ones are a
# plain table with "Video ID" and "Playlist Video Creation Timestamp". Either is read here.

MAX_TAKEOUT_BYTES = 5_000_000
_TAKEOUT_SUFFIX = re.compile(r"(-videos)?\.csv$", re.I)


def parse_takeout_csv(text: str) -> list[tuple[str, Optional[int]]]:
    """(video id, added at) in the file's order, without repeats; [] when the file has no video id column."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    for i, row in enumerate(rows):
        names = [c.strip().lower() for c in row]
        col = next((j for j, n in enumerate(names) if n in ("video id", "video-id", "videoid")), None)
        if col is None:
            continue
        when = next((j for j, n in enumerate(names) if "time" in n or "timestamp" in n or "added" in n), None)
        out, seen = [], set()
        for r in rows[i + 1:]:
            vid = r[col].strip() if col < len(r) else ""
            if not re.match(r"^[A-Za-z0-9_-]{11}$", vid) or vid in seen:
                continue
            seen.add(vid)
            stamp = _iso_time(r[when].strip().replace(" UTC", "").replace(" ", "T")) if when is not None and when < len(r) else None
            out.append((vid, stamp))
        return out
    return []


def _local_items(c: Ctx, ids: list[tuple[str, Optional[int]]]) -> list[dict]:
    items = [{"item_id": vid, "remote_id": "", "title": vid, "artist": "", "duration_ms": 0, "added_at": when,
              "url": item_url("youtube", vid), "unavailable": 0} for vid, when in ids]
    acc = account(c, "youtube")
    if acc and acc["access_token"] and items:
        with _api(c, "youtube") as http:
            videos = _youtube_videos(http, [x["item_id"] for x in items])
        for x in items:
            v = videos.get(x["item_id"])
            if v:
                x.update(title=v["title"] or x["title"], artist=v["artist"], duration_ms=v["duration_ms"])
            else:
                x["unavailable"] = 1
    return items


def import_takeout(c: Ctx, files: list[dict]) -> dict:
    """Each CSV becomes a YouTube playlist kept only in Stash, named after the file; importing the same file name
    again updates that playlist. Titles, channels and lengths come from YouTube when it is connected (1 quota
    unit per 50 videos); a video it no longer has keeps the title Stash saw before, marked unavailable."""
    out, con, t = [], c.con, db.now()
    for f in files[:50]:
        name = _TAKEOUT_SUFFIX.sub("", clean_text(str(f.get("name", "")).rsplit("/", 1)[-1], 150)) or "Takeout"
        text = str(f.get("text", ""))
        if len(text) > MAX_TAKEOUT_BYTES:
            out.append({"file": name, "error": "too_large"})
            continue
        ids = parse_takeout_csv(text)
        if not ids:
            out.append({"file": name, "error": "no_video_ids"})
            continue
        items = _local_items(c, ids)
        external = "stash:takeout:" + re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:80]
        with db.tx(con):
            row = con.execute("SELECT id FROM playlists WHERE user_id=? AND service='youtube' AND external_id=?",
                              (c.uid, external)).fetchone()
            if row:
                pid = row[0]
                con.execute("UPDATE playlists SET name=?, remote_count=?, synced_at=?, dirty=0, gone=0 WHERE id=?",
                            (name, len(items), t, pid))
            else:
                pid = con.execute(
                    "INSERT INTO playlists(user_id, service, external_id, name, description, public, editable, owner,"
                    " remote_count, synced_at, local) VALUES(?,?,?,?,?,0,1,'',?,?,1)",
                    (c.uid, "youtube", external, name, "Google Takeout", len(items), t)).lastrowid
            _store_items(con, pid, items)
        out.append({"file": name, "playlist_id": pid, "videos": len(items),
                    "unavailable": sum(x["unavailable"] for x in items), "updated": bool(row)})
    return {"imported": [x for x in out if "error" not in x], "failed": [x for x in out if "error" in x]}


def refresh_local(c: Ctx, playlist_id: int) -> dict:
    """Read the titles of a Stash-only playlist's videos from YouTube again."""
    con = c.con
    if playlist(c, playlist_id)["service"] != "youtube":
        raise err(409, "local_playlist: a Stash-only Spotify playlist already has its titles; publish it to put it on Spotify")
    rows = con.execute("SELECT item_id, added_at FROM playlist_items WHERE playlist_id=? ORDER BY position, id",
                       (playlist_id,)).fetchall()
    items = _local_items(c, [(r["item_id"], r["added_at"]) for r in rows])
    with db.tx(con):
        _store_items(con, playlist_id, items)
        con.execute("UPDATE playlists SET synced_at=?, remote_count=? WHERE id=?", (db.now(), len(items), playlist_id))
    return {"service": "youtube", "total": 1, "fetched": 1, "items": len(items), "skipped_with_local_changes": [],
            "failed": [], "metadata_only": [], "unchanged": 0, "deferred": []}


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


_API_DISABLED = re.compile(r"(?:has not been used in project|is disabled|accessNotConfigured|SERVICE_DISABLED)", re.I)
_PROJECT = re.compile(r"project (\d{6,})")


def problem_code(c: Ctx, e: ServiceError) -> str:
    """A code the page can explain. youtube_api_disabled:<project number> when Google answers that the YouTube Data
    API is not enabled in the project of the user's client: the connection works but nothing can be fetched."""
    if e.service == "youtube" and e.status == 403 and _API_DISABLED.search(e.message):
        m = _PROJECT.search(e.message)
        acc = account(c, "youtube")
        project = m.group(1) if m else (acc["client_id"].split("-", 1)[0] if acc and "-" in acc["client_id"] else "")
        return f"youtube_api_disabled:{project}"
    if e.status == 429 and e.message.startswith("rate_limited:"):
        return f"rate_limited:{e.service}:{e.message.split(':', 1)[1]}"   # rate_limited:<service>:<pause ends, epoch>
    if e.status in (401, 403) and re.search(r"scope|insufficient", e.message, re.I):
        # connected before Stash asked for this permission (e.g. Spotify's followed artists): connect again
        return f"reconnect_needed:{e.service}"
    return f"{e.service}_error: {e.status} {e.message}"


def _service_errors(c: Ctx, fn):
    try:
        return fn()
    except ServiceError as e:
        code = problem_code(c, e)
        status = 429 if code.startswith("rate_limited") else 409 if code.startswith(("youtube_api_disabled", "reconnect_needed")) else 502
        raise err(status, code) from e


@session_api.get("")
def web_overview(c: Ctx = Depends(ctx)):
    return overview(c)


class TakeoutFile(BaseModel):
    name: str = Field(max_length=300)
    text: str = Field(max_length=MAX_TAKEOUT_BYTES)


class Takeout(BaseModel):
    files: list[TakeoutFile] = Field(min_length=1, max_length=50)


@session_api.post("/takeout")
def web_takeout(body: Takeout, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: import_takeout(c, [f.model_dump() for f in body.files]))


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
        # the tokens are saved before the profile is read, so an API that is still disabled is a problem to
        # explain on the page, not a failed connection
        problem = problem_code(c, e)
        if problem.startswith("youtube_api_disabled"):
            return RedirectResponse(f"/#/playlists?connected={service}&problem={quote(problem)}", status_code=303)
        return RedirectResponse(f"/#/playlists?error={quote(clean_text(f'{e.status} {e.message}', 100))}", status_code=303)
    return RedirectResponse(f"/#/playlists?connected={service}&fetch=1", status_code=303)


@session_api.delete("/accounts/{service}")
def web_disconnect(service: str, c: Ctx = Depends(ctx)):
    return disconnect(c, service)


@session_api.get("/accounts/{service}/progress")
def web_progress(service: str, c: Ctx = Depends(ctx)):
    return progress(c, _service(service))


@session_api.post("/accounts/{service}/sync-all")
def web_sync_all(service: str, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: sync_all(c, service))


@session_api.post("/accounts/{service}/sync")
def web_sync(service: str, force: bool = False, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: pull(c, service, None, force))


@session_api.post("/accounts/{service}/create")
def web_create(service: str, body: Create, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: create(c, service, body.name, body.description, body.public))


@session_api.get("/lists/{playlist_id}")
def web_show(playlist_id: int, offset: int = 0, limit: int = 500, c: Ctx = Depends(ctx)):
    return show(c, playlist_id, offset, limit)


@session_api.post("/lists/{playlist_id}/edit")
def web_edit(playlist_id: int, body: Edit, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: edit(c, playlist_id, body.ops))


@session_api.post("/lists/{playlist_id}/discard")
def web_discard(playlist_id: int, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: discard(c, playlist_id))


@session_api.post("/lists/{playlist_id}/push")
def web_push(playlist_id: int, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: push(c, playlist_id))


@session_api.post("/lists/{playlist_id}/sync")
def web_sync_one(playlist_id: int, force: bool = False, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: pull(c, playlist(c, playlist_id)["service"], playlist_id, force))


@session_api.post("/lists/{playlist_id}/delete")
def web_mark_delete(playlist_id: int, c: Ctx = Depends(ctx)):
    return mark_delete(c, playlist_id)


@session_api.post("/lists/{playlist_id}/publish")
def web_publish(playlist_id: int, c: Ctx = Depends(ctx)):
    return _service_errors(c, lambda: publish(c, playlist_id))


@session_api.post("/lists/{playlist_id}/keep")
def web_keep(playlist_id: int, c: Ctx = Depends(ctx)):
    return mark_delete(c, playlist_id, keep=True)


@session_api.delete("/lists/{playlist_id}")
def web_forget(playlist_id: int, c: Ctx = Depends(ctx)):
    return forget(c, playlist_id)
