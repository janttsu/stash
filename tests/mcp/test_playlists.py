"""Playlists against fake Spotify and YouTube services: connecting, fetching, pending edits, pushing (replace on
Spotify, a diff of deletes, inserts and moves on YouTube), the backup copy, and the MCP tools."""
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_mcp import Client, new_key

from app import playlists


class Fake:
    """Both services in one handler, with state the tests can inspect: every call is logged."""

    def __init__(self):
        self.calls: list[str] = []
        self.spotify = {"pl1": {"name": "Chill", "description": "slow", "public": True, "owner": "me",
                               "tracks": ["t" * 20 + "01", "t" * 20 + "02", "t" * 20 + "03"]},
                        "pl2": {"name": "Not mine", "description": "", "public": False, "owner": "other", "tracks": ["t" * 20 + "09"]}}
        self.youtube = {"PL1": {"title": "Live", "description": "", "privacy": "private",
                                "items": [("item-a", "vidvidvid01"), ("item-b", "vidvidvid02"), ("item-c", "vidvidvid03")]}}
        self.next_item = 0
        self.api_disabled = False      # Google's answer while the YouTube Data API is off in the client's project
        self.broken = set()            # playlist ids whose items the service refuses

    def __call__(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else {}
        self.calls.append(f"{request.method} {u.netloc}{u.path}")
        if u.netloc in ("accounts.spotify.com", "oauth2.googleapis.com"):
            form = parse_qs(request.content.decode())
            assert form["grant_type"][0] in ("authorization_code", "refresh_token")
            return httpx.Response(200, json={"access_token": f"access-{form['grant_type'][0]}", "refresh_token": "refresh-1", "expires_in": 3600})
        assert request.headers.get("authorization", "").startswith("Bearer access-"), "every call carries the token"
        if u.netloc == "api.spotify.com":
            return self.spotify_api(request, u.path.removeprefix("/v1"), q, body)
        return self.youtube_api(request, u.path.removeprefix("/youtube/v3"), q, body)

    # --- Spotify --------------------------------------------------------------------------------
    def spotify_api(self, request, path, q, body):
        tr = lambda i: {"id": i, "name": f"Track {i[-2:]}", "duration_ms": 180000, "artists": [{"name": "Artist"}]}  # noqa: E731
        if path == "/me":
            return httpx.Response(200, json={"id": "me", "display_name": "Me"})
        if path == "/me/playlists":
            return httpx.Response(200, json={"next": None, "items": [
                {"id": k, "name": p["name"], "description": p["description"], "public": p["public"], "collaborative": False,
                 "owner": {"id": p["owner"]}, "tracks": {"total": len(p["tracks"])}, "snapshot_id": f"snap-{len(p['tracks'])}"}
                for k, p in self.spotify.items()]})
        if path == "/tracks":
            return httpx.Response(200, json={"tracks": [tr(i) for i in q["ids"].split(",")]})
        if path == "/users/me/playlists" and request.method == "POST":
            self.spotify["pl3"] = {"name": body["name"], "description": body["description"], "public": body["public"], "owner": "me", "tracks": []}
            return httpx.Response(201, json={"id": "pl3"})
        pid = path.split("/")[2]
        p = self.spotify[pid]
        if path.endswith("/tracks"):
            if request.method == "GET":
                return httpx.Response(200, json={"next": None, "items": [{"added_at": "2026-01-02T03:04:05Z", "track": tr(i)} for i in p["tracks"]]})
            ids = [x.split(":")[-1] for x in body["uris"]]
            p["tracks"] = ids if request.method == "PUT" else p["tracks"] + ids
            return httpx.Response(200, json={"snapshot_id": "snap-new"})
        if request.method == "PUT":
            p.update({"name": body["name"], "description": body["description"], "public": body["public"]})
            return httpx.Response(200)
        return httpx.Response(200, json={"name": p["name"], "description": p["description"], "public": p["public"],
                                         "snapshot_id": "snap", "tracks": {"total": len(p["tracks"])}, "owner": {"id": p["owner"]}})

    # --- YouTube --------------------------------------------------------------------------------
    def youtube_api(self, request, path, q, body):
        if self.api_disabled:
            return httpx.Response(403, json={"error": {"code": 403, "message": "YouTube Data API v3 has not been used in "
                                                       "project 123456789012 before or it is disabled. Enable it by visiting …"}})
        if path == "/playlistItems" and request.method == "GET" and q.get("playlistId") in self.broken:
            return httpx.Response(404, json={"error": {"message": "playlistNotFound"}})
        if path == "/channels":
            return httpx.Response(200, json={"items": [{"id": "UC1", "snippet": {"title": "My channel"}}]})
        if path == "/videos":
            return httpx.Response(200, json={"items": [{"id": v, "snippet": {"title": f"Video {v[-2:]}", "channelTitle": "Chan"},
                                                         "contentDetails": {"duration": "PT3M"}} for v in q["id"].split(",")]})
        if path == "/playlists":
            if request.method == "POST":
                self.youtube["PL2"] = {"title": body["snippet"]["title"], "description": body["snippet"]["description"],
                                       "privacy": body["status"]["privacyStatus"], "items": []}
                return httpx.Response(200, json={"id": "PL2"})
            if request.method == "PUT":
                self.youtube[body["id"]].update({"title": body["snippet"]["title"], "description": body["snippet"]["description"],
                                                 "privacy": body["status"]["privacyStatus"]})
                return httpx.Response(200, json={})
            keys = [q["id"]] if "id" in q else list(self.youtube)
            return httpx.Response(200, json={"items": [
                {"id": k, "etag": "e", "snippet": {"title": self.youtube[k]["title"], "description": self.youtube[k]["description"], "channelId": "UC1"},
                 "status": {"privacyStatus": self.youtube[k]["privacy"]}, "contentDetails": {"itemCount": len(self.youtube[k]["items"])}}
                for k in keys]})
        if path == "/playlistItems":
            if request.method == "GET":
                p = self.youtube[q["playlistId"]]
                return httpx.Response(200, json={"items": [
                    {"id": rid, "snippet": {"title": f"Video {vid[-2:]}", "videoOwnerChannelTitle": "Chan", "publishedAt": "2026-01-01T00:00:00Z",
                                            "resourceId": {"videoId": vid}, "position": i}} for i, (rid, vid) in enumerate(p["items"])]})
            if request.method == "DELETE":
                for p in self.youtube.values():
                    p["items"] = [x for x in p["items"] if x[0] != q["id"]]
                return httpx.Response(204)
            sn = body["snippet"]
            p = self.youtube[sn["playlistId"]]
            if request.method == "POST":
                self.next_item += 1
                rid = f"item-new{self.next_item}"
                p["items"].insert(sn.get("position", len(p["items"])), (rid, sn["resourceId"]["videoId"]))
                return httpx.Response(200, json={"id": rid})
            rid = body["id"]
            entry = next(x for x in p["items"] if x[0] == rid)
            p["items"].remove(entry)
            p["items"].insert(sn["position"], entry)
            return httpx.Response(200, json={"id": rid})
        return httpx.Response(404, json={"error": {"message": f"no fake for {path}"}})


@pytest.fixture
def fake(monkeypatch):
    from app import db

    con = db.connect()   # each test starts with no accounts and no playlists
    con.execute("DELETE FROM playlist_accounts")
    con.execute("DELETE FROM playlists")
    con.close()
    f = Fake()
    monkeypatch.setattr(playlists, "TRANSPORT", httpx.MockTransport(f))
    yield f


def connect(stash, service):
    web = stash["web"]
    r = web.post(f"/api/playlists/accounts/{service}/connect", json={"client_id": "cid", "client_secret": "sec"})
    assert r.status_code == 200, r.text
    url = urlsplit(r.json()["url"])
    state = parse_qs(url.query)["state"][0]
    assert parse_qs(url.query)["redirect_uri"][0].endswith(f"/api/playlists/accounts/{service}/callback")
    r = web.get(f"/api/playlists/accounts/{service}/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/#/playlists?connected={service}&fetch=1", r.text
    return r


def test_connect_and_fetch(api, stash, fake):
    web = stash["web"]
    assert web.get(f"/api/playlists/accounts/spotify/callback?code=x&state=wrong", follow_redirects=False).status_code == 400
    connect(stash, "spotify")
    connect(stash, "youtube")
    data = web.get("/api/playlists").json()
    assert {a["service"]: a["display_name"] for a in data["accounts"]} == {"spotify": "Me", "youtube": "My channel"}
    assert all(a["connected"] for a in data["accounts"])
    r = web.post("/api/playlists/accounts/spotify/sync").json()
    assert r["fetched"] == 2
    r = web.post("/api/playlists/accounts/youtube/sync").json()
    assert r["fetched"] == 1
    data = web.get("/api/playlists").json()
    names = {(p["service"], p["name"]): p for p in data["playlists"]}
    assert names[("spotify", "Chill")]["count"] == 3 and names[("spotify", "Chill")]["editable"]
    assert not names[("spotify", "Not mine")]["editable"]
    yt = names[("youtube", "Live")]
    assert yt["count"] == 3 and yt["url"] == "https://www.youtube.com/playlist?list=PL1"
    d = web.get(f"/api/playlists/lists/{yt['id']}").json()
    assert [x["title"] for x in d["items"]] == ["Video 01", "Video 02", "Video 03"] and d["items"][0]["duration_ms"] == 180000
    # the service's token is refreshed when it has expired
    from app import db
    con = db.connect()
    con.execute("UPDATE playlist_accounts SET expires_at=0 WHERE service='spotify'")
    con.close()
    assert web.post("/api/playlists/accounts/spotify/sync").json()["fetched"] == 2
    assert "POST accounts.spotify.com/api/token" in fake.calls[-10:]
    # a playlist that disappears from the service stays in Stash, marked gone; the export holds everything
    del fake.spotify["pl2"]
    web.post("/api/playlists/accounts/spotify/sync")
    data = web.get("/api/playlists").json()
    assert next(p for p in data["playlists"] if p["name"] == "Not mine")["gone"]
    export = web.get("/api/playlists/export")
    assert export.headers["content-disposition"].startswith("attachment") and len(export.json()["playlists"]) == 3
    assert stash["raw"].get("/api/playlists").status_code == 401


def test_edit_and_push_spotify(api, stash, fake):
    web = stash["web"]
    connect(stash, "spotify")
    web.post("/api/playlists/accounts/spotify/sync")
    pid = next(p["id"] for p in web.get("/api/playlists").json()["playlists"] if p["name"] == "Chill")
    items = web.get(f"/api/playlists/lists/{pid}").json()["items"]
    t01, t02, t03 = (x["id"] for x in items)
    r = web.post(f"/api/playlists/lists/{pid}/edit", json={"ops": [
        {"op": "add", "items": ["https://open.spotify.com/track/" + "n" * 20 + "07?si=x", "spotify:track:" + "n" * 20 + "08", "not a track"]},
        {"op": "remove", "ids": [t02]},
        {"op": "move", "id": t03, "position": 0},
        {"op": "rename", "name": "Chill 2", "description": "slower", "public": False}]}).json()
    assert r["added"] == 2 and r["removed"] == 1 and r["moved"] == 1 and r["renamed"] == 1 and r["unknown"] == ["not a track"]
    d = web.get(f"/api/playlists/lists/{pid}").json()
    assert d["playlist"]["dirty"] and d["playlist"]["name"] == "Chill 2"
    assert [(x["title"], x["state"]) for x in d["items"]] == [("Track 03", "synced"), ("Track 01", "synced"), ("Track 02", "removed"),
                                                             ("Track 07", "added"), ("Track 08", "added")]
    assert d["items"][0]["moved"] and d["pending"] == {"added": 2, "removed": 1, "moved": 2}
    assert fake.spotify["pl1"]["tracks"] == ["t" * 20 + "01", "t" * 20 + "02", "t" * 20 + "03"], "nothing changed on the service yet"
    # a fetch leaves a playlist with pending changes alone
    assert web.post("/api/playlists/accounts/spotify/sync").json()["skipped_with_local_changes"] == ["Chill 2"]
    r = web.post(f"/api/playlists/lists/{pid}/push").json()
    assert r["pushed"]["added"] == 2 and r["pushed"]["removed"] == 1
    assert fake.spotify["pl1"]["tracks"] == ["t" * 20 + "03", "t" * 20 + "01", "n" * 20 + "07", "n" * 20 + "08"]
    assert fake.spotify["pl1"]["name"] == "Chill 2" and fake.spotify["pl1"]["public"] is False
    d = web.get(f"/api/playlists/lists/{pid}").json()
    assert not d["playlist"]["dirty"] and [x["state"] for x in d["items"]] == ["synced"] * 4
    assert [x["title"] for x in d["items"]] == ["Track 03", "Track 01", "Track 07", "Track 08"]
    # discard drops pending changes
    web.post(f"/api/playlists/lists/{pid}/edit", json={"ops": [{"op": "remove", "ids": [d["items"][0]["id"]]}]})
    assert web.get(f"/api/playlists/lists/{pid}").json()["pending"]["removed"] == 1
    web.post(f"/api/playlists/lists/{pid}/discard")
    assert web.get(f"/api/playlists/lists/{pid}").json()["pending"] == {"added": 0, "removed": 0, "moved": 0}
    # someone else's playlist cannot be edited; a new one can be made
    other = next(p["id"] for p in web.get("/api/playlists").json()["playlists"] if p["name"] == "Not mine")
    assert web.post(f"/api/playlists/lists/{other}/edit", json={"ops": [{"op": "remove", "ids": [1]}]}).status_code == 409
    r = web.post("/api/playlists/accounts/spotify/create", json={"name": "Fresh", "public": True}).json()
    assert r["external_id"] == "pl3" and fake.spotify["pl3"]["name"] == "Fresh"
    assert web.delete(f"/api/playlists/lists/{r['id']}").status_code == 200, "forgetting the copy"
    assert "pl3" in fake.spotify, "the playlist on the service is not touched"


def test_push_youtube_as_a_diff(api, stash, fake):
    web = stash["web"]
    connect(stash, "youtube")
    web.post("/api/playlists/accounts/youtube/sync")
    pid = web.get("/api/playlists").json()["playlists"][0]["id"]
    a, b, c = (x["id"] for x in web.get(f"/api/playlists/lists/{pid}").json()["items"])
    web.post(f"/api/playlists/lists/{pid}/edit", json={"ops": [
        {"op": "remove", "ids": [b]},
        {"op": "add", "items": ["https://youtu.be/vidvidvid09"], "position": 0},
        {"op": "order", "ids": [c]}]})
    d = web.get(f"/api/playlists/lists/{pid}").json()
    assert [x["item_id"] for x in d["items"] if x["state"] != "removed"] == ["vidvidvid03", "vidvidvid09", "vidvidvid01"]
    fake.calls.clear()
    r = web.post(f"/api/playlists/lists/{pid}/push").json()
    assert [v for _, v in fake.youtube["PL1"]["items"]] == ["vidvidvid03", "vidvidvid09", "vidvidvid01"]
    writes = [x for x in fake.calls if x.split()[0] in ("DELETE", "POST", "PUT")]
    assert writes.count("DELETE www.googleapis.com/youtube/v3/playlistItems") == 1
    assert writes.count("POST www.googleapis.com/youtube/v3/playlistItems") == 1
    assert r["pushed"]["removed"] == 1 and r["pushed"]["added"] == 1 and r["pushed"]["calls"] <= 5, "a diff, not a rewrite"
    d = web.get(f"/api/playlists/lists/{pid}").json()
    assert not d["playlist"]["dirty"] and [x["title"] for x in d["items"]] == ["Video 03", "Video 09", "Video 01"]


def test_mcp_tools(api, stash, fake):
    connect(stash, "spotify")
    mcp = Client(stash["raw"], new_key(stash)["key"])
    err, text = mcp.call("playlists_overview")
    assert not err and "spotify: connected as Me" in text and "youtube: not connected" in text and "No playlists fetched yet" in text
    err, text = mcp.call("playlists_sync", service="spotify")
    assert not err and text == "Fetched 2 of 2 playlists (4 items) from spotify."
    err, text = mcp.call("playlists_overview")
    assert "[spotify] Chill — 3 items" in text and "Not mine — 1 items, someone else's" in text
    pid = int(next(line for line in text.splitlines() if "Chill" in line).split()[0][1:])
    err, text = mcp.call("playlist_show", playlist_id=pid)
    assert not err and text.startswith(f"P{pid} Chill [spotify] 3 items") and "1. Track 01 — Artist (3:00)" in text
    assert text.splitlines()[1] == "slow", "the description comes after the heading"
    first = int(next(line for line in text.splitlines() if line.startswith("#")).split()[0][1:])
    err, text = mcp.call("playlist_edit", playlist_id=pid, ops=[{"op": "remove", "ids": [first]}, {"op": "add", "items": ["x" * 22]}])
    assert not err and text.startswith("Pending changes: 1 added, 1 removed") and "until playlist_push" in text
    err, text = mcp.call("playlist_show", playlist_id=pid)
    assert "[removed]" in text and "[added]" in text and "pending: +1 −1" in text
    reader = Client(stash["raw"], new_key(stash, can_write=False)["key"])
    err, text = reader.call("playlist_push", playlist_id=pid)
    assert err and "read_only_key" in text
    err, text = reader.call("playlist_show", playlist_id=pid)
    assert not err
    err, text = mcp.call("playlist_push", playlist_id=pid)
    assert not err and text.startswith("Pushed: 1 added, 1 removed")
    assert fake.spotify["pl1"]["tracks"] == ["t" * 20 + "02", "t" * 20 + "03", "x" * 22]
    r = mcp.rpc("tools/call", {"name": "playlist_create", "arguments": {"service": "spotify", "name": "From MCP"}}).json()["result"]
    assert not r.get("isError") and "Created P" in r["content"][0]["text"]
    err, text = mcp.call("playlist_push", playlist_id=999999)
    assert err and "no_such_playlist" in text
    err, text = mcp.call("playlists_sync", service="youtube")
    assert err and "not_connected" in text


def test_youtube_api_disabled_is_explained(api, stash, fake):
    """Google lets the user connect while the YouTube Data API is off, then refuses everything: the page gets a
    code with the project number (to link to the right API page) instead of a raw 403, and so does MCP."""
    web = stash["web"]
    fake.api_disabled = True
    r = web.post("/api/playlists/accounts/youtube/connect",
                 json={"client_id": "123456789012-x.apps.googleusercontent.com", "client_secret": "s"})
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    r = web.get(f"/api/playlists/accounts/youtube/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/#/playlists?connected=youtube&problem=youtube_api_disabled%3A123456789012"
    accounts = web.get("/api/playlists").json()["accounts"]
    assert next(a for a in accounts if a["service"] == "youtube")["connected"], "the tokens are kept"
    r = web.post("/api/playlists/accounts/youtube/sync")
    assert r.status_code == 409 and r.json()["detail"] == "youtube_api_disabled:123456789012"
    err, text = Client(stash["raw"], new_key(stash)["key"]).call("playlists_sync", service="youtube")
    assert err and "youtube.googleapis.com?project=123456789012" in text
    fake.api_disabled = False   # the user enabled it: fetching works without connecting again
    r = web.post("/api/playlists/accounts/youtube/sync").json()
    assert r == {"service": "youtube", "total": 1, "fetched": 1, "items": 3, "skipped_with_local_changes": [], "failed": []}


def test_fetch_summary_and_progress(api, stash, fake):
    """One playlist the service will not give does not stop the others; the summary says which and why."""
    from app import playlists as pl

    web = stash["web"]
    connect(stash, "youtube")
    fake.youtube["PL9"] = {"title": "Broken", "description": "", "privacy": "private", "items": [("x", "vidvidvid07")]}
    fake.broken.add("PL9")
    assert web.get("/api/playlists/accounts/youtube/progress").json() == {"running": False}
    r = web.post("/api/playlists/accounts/youtube/sync").json()
    assert r["total"] == 2 and r["fetched"] == 1 and r["items"] == 3
    assert r["failed"] == [{"name": "Broken", "error": "404 playlistNotFound"}]
    assert web.get("/api/playlists/accounts/youtube/progress").json() == {"running": False}, "cleared when done"
    # while a fetch runs, the progress endpoint reports it
    seen = []
    real = pl._remote_items

    def watching(http, service, external_id):
        seen.append(web.get("/api/playlists/accounts/youtube/progress").json())
        return real(http, service, external_id)

    pl._remote_items = watching
    try:
        web.post("/api/playlists/accounts/youtube/sync")
    finally:
        pl._remote_items = real
    assert seen[0]["running"] and seen[0]["total"] == 2 and seen[0]["done"] == 0 and seen[0]["name"] == "Live"
    assert seen[1]["done"] == 1 and seen[1]["items"] == 3 and seen[1]["name"] == "Broken"
    assert stash["raw"].get("/api/playlists/accounts/youtube/progress").status_code == 401


def test_synchronise_both_ways(api, stash, fake):
    """Synchronise pushes every playlist's pending changes, then fetches everything; a plain fetch leaves
    pending changes alone."""
    web = stash["web"]
    connect(stash, "spotify")
    web.post("/api/playlists/accounts/spotify/sync")
    pid = next(p["id"] for p in web.get("/api/playlists").json()["playlists"] if p["name"] == "Chill")
    first = web.get(f"/api/playlists/lists/{pid}").json()["items"][0]["id"]
    web.post(f"/api/playlists/lists/{pid}/edit", json={"ops": [{"op": "remove", "ids": [first]}]})
    fake.spotify["pl2"]["name"] = "Not mine (renamed on Spotify)"
    r = web.post("/api/playlists/accounts/spotify/sync").json()
    assert r["skipped_with_local_changes"] == ["Chill"] and fake.spotify["pl1"]["tracks"][0] == "t" * 20 + "01"
    r = web.post("/api/playlists/accounts/spotify/sync-all").json()
    assert r["pushed"] == ["Chill"] and r["push_failed"] == [] and r["fetched"] == 2 and r["skipped_with_local_changes"] == []
    assert fake.spotify["pl1"]["tracks"] == ["t" * 20 + "02", "t" * 20 + "03"], "the change reached the service"
    names = {p["name"]: p for p in web.get("/api/playlists").json()["playlists"]}
    assert not names["Chill"]["pending"] and names["Chill"]["count"] == 2
    assert "Not mine (renamed on Spotify)" in names, "and the service's own changes came back"
    assert web.get("/api/playlists/accounts/spotify/progress").json() == {"running": False}
    # nothing pending: a synchronise is just a fetch
    r = web.post("/api/playlists/accounts/spotify/sync-all").json()
    assert r["pushed"] == [] and r["fetched"] == 2
    # over MCP, push_pending=true does the same
    web.post(f"/api/playlists/lists/{pid}/edit", json={"ops": [{"op": "add", "items": ["n" * 22]}]})
    err, text = Client(stash["raw"], new_key(stash)["key"]).call("playlists_sync", service="spotify", push_pending=True)
    assert not err and text.startswith("Pushed 1 playlists with pending changes (Chill). Fetched 2 of 2")
    assert fake.spotify["pl1"]["tracks"][-1] == "n" * 22
    reader = Client(stash["raw"], new_key(stash, can_write=False)["key"])
    err, text = reader.call("playlists_sync", service="spotify", push_pending=True)
    assert err and "read_only_key" in text
