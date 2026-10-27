"""Subscriptions against fake YouTube and Spotify: fetching, tags and notes, pending subscribes and unsubscribes,
pushing them, the activity check, Stash-only YouTube playlists, a connection that lacks a permission, and MCP."""
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_mcp import ACCEPT, Client, new_key
from test_playlists import Fake, connect

from app import playlists

CH = {"UC" + "a" * 22: ("Alpha Music", 120, "2026-10-01T10:00:00Z"),
      "UC" + "b" * 22: ("Beta Cooking", 40, "2023-01-05T10:00:00Z"),
      "UC" + "c" * 22: ("Gamma Quiet", 3, None)}
NEW_CH = "UC" + "d" * 22


class SubFake(Fake):
    def __init__(self):
        super().__init__()
        self.subs = {f"sub-{cid}": cid for cid in CH}           # YouTube: subscription id -> channel id
        self.artists = {"a" * 22: "Artist A", "b" * 22: "Artist B"}
        self.followed_artists = {"a" * 22}
        self.unfollowed_playlists: set[str] = set()
        self.scope_missing = False

    def youtube_api(self, request, path, q, body):
        if path == "/subscriptions":
            if request.method == "GET":
                items = [{"id": sid, "snippet": {"title": (CH.get(cid) or ("Delta New", 0, None))[0], "description": "",
                                                 "publishedAt": "2025-03-04T05:06:07Z", "resourceId": {"channelId": cid}},
                          "contentDetails": {"totalItemCount": (CH.get(cid) or (0, 7))[1]}} for sid, cid in self.subs.items()]
                return httpx.Response(200, json={"items": items, "pageInfo": {"totalResults": len(items)}})
            if request.method == "POST":
                cid = body["snippet"]["resourceId"]["channelId"]
                self.subs[f"sub-{cid}"] = cid
                return httpx.Response(200, json={"id": f"sub-{cid}"})
            if request.method == "DELETE":
                self.subs.pop(q["id"])
                return httpx.Response(204)
        if path == "/channels" and ("forHandle" in q or "id" in q):
            if q.get("forHandle") == "@delta" or q.get("id") == NEW_CH:
                return httpx.Response(200, json={"items": [{"id": NEW_CH, "snippet": {"title": "Delta New", "description": "d"},
                                                            "statistics": {"videoCount": "7"}}]})
            return httpx.Response(200, json={"items": []})
        if path == "/playlistItems" and q.get("playlistId", "").startswith("UU"):
            last = CH.get("UC" + q["playlistId"][2:], (None, None, None))[2]
            items = [{"contentDetails": {"videoPublishedAt": last}}] if last else []
            return httpx.Response(200, json={"items": items})
        if path == "/playlists" and q.get("id") == "PLsomeoneelse12345":
            return httpx.Response(200, json={"items": [{"id": "PLsomeoneelse12345", "snippet": {
                "title": "Someone's mix", "description": "", "channelTitle": "Someone"}, "contentDetails": {"itemCount": 42}}]})
        return super().youtube_api(request, path, q, body)

    def spotify_api(self, request, path, q, body):
        forbidden = httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
        if path == "/me/following":   # since February 2026 only GET is left; following goes through /me/library
            if self.scope_missing:
                return httpx.Response(403, json={"error": {"status": 403, "message": "Insufficient client scope"}})
            if request.method != "GET":
                return forbidden
            items = [{"id": a, "name": self.artists[a], "genres": ["rock"]} for a in sorted(self.followed_artists)]
            return httpx.Response(200, json={"artists": {"items": items, "total": len(items), "next": None}})
        if path == "/me/library":
            for uri in q["uris"].split(","):
                _, kind, sid = uri.split(":")
                if kind == "artist":
                    self.followed_artists = self.followed_artists | {sid} if request.method == "PUT" else self.followed_artists - {sid}
                elif request.method == "DELETE":
                    self.unfollowed_playlists.add(sid)
                    self.spotify.pop(sid, None)
            return httpx.Response(200)
        if path.endswith("/followers"):
            return forbidden
        if path.startswith("/artists/"):
            a = path.split("/")[2]
            return httpx.Response(200, json={"id": a, "name": self.artists[a], "genres": []})
        return super().spotify_api(request, path, q, body)


@pytest.fixture
def fake(monkeypatch):
    from app import db

    con = db.connect()
    for table in ("playlist_accounts", "playlists", "subscriptions"):
        con.execute(f"DELETE FROM {table}")
    con.execute("DELETE FROM config WHERE key LIKE 'rate_block:%'")
    con.close()
    f = SubFake()
    monkeypatch.setattr(playlists, "TRANSPORT", httpx.MockTransport(f))
    monkeypatch.setattr(playlists, "MIN_INTERVAL", {})
    monkeypatch.setattr(playlists, "RETRY_PAUSE", 0)
    yield f


def ids(web, service, **query):
    q = "&".join(f"{k}={v}" for k, v in query.items())
    return {x["title"]: x for x in web.get(f"/api/subscriptions/{service}?{q}").json()["items"]}


def test_youtube_fetch_tag_and_push(api, stash, fake):
    web = stash["web"]
    assert stash["raw"].get("/api/subscriptions").status_code == 401
    connect(stash, "youtube")
    r = web.post("/api/subscriptions/youtube/sync").json()
    assert r["total"] == 3 and r["new"] == 3 and r["by_kind"] == {"channel": 3} and r["pushed"] == []
    subs = ids(web, "youtube")
    assert set(subs) == {"Alpha Music", "Beta Cooking", "Gamma Quiet"}
    assert subs["Alpha Music"]["item_count"] == 120 and subs["Alpha Music"]["url"].endswith("/channel/UC" + "a" * 22)
    a, b = subs["Alpha Music"]["id"], subs["Beta Cooking"]["id"]
    # tags and notes are Stash's own
    r = web.post("/api/subscriptions/youtube/edit", json={"ops": [
        {"op": "tag", "ids": [a, b], "add": ["Music", "fav"]}, {"op": "tag", "ids": [b], "remove": ["music"]},
        {"op": "note", "id": a, "note": "the good one\x07"}]}).json()
    assert r["tagged"] == 3 and r["noted"] == 1
    subs = ids(web, "youtube")
    assert subs["Alpha Music"]["tags"] == ["music", "fav"] and subs["Beta Cooking"]["tags"] == ["fav"]
    assert subs["Alpha Music"]["note"] == "the good one"
    assert set(ids(web, "youtube", tag="music")) == {"Alpha Music"}
    assert web.get("/api/subscriptions").json()["tags"] == {"fav": 2, "music": 1}
    # unsubscribe one, subscribe a new one by @handle: pending until a push
    r = web.post("/api/subscriptions/youtube/edit", json={"ops": [
        {"op": "unsubscribe", "ids": [b]}, {"op": "subscribe", "items": ["https://www.youtube.com/@delta", "https://example.com/x"]}]}).json()
    assert r["unsubscribed"] == 1 and r["subscribed"] == 1 and r["unknown"] == ["https://example.com/x"]
    p = web.get("/api/subscriptions/youtube/pending").json()
    assert [x["title"] for x in p["subscribe"]] == ["Delta New"] and [x["title"] for x in p["unsubscribe"]] == ["Beta Cooking"]
    assert p["quota_units"] == 100
    assert "sub-UC" + "b" * 22 in fake.subs, "nothing changed on YouTube yet"
    # a plain fetch keeps the pending changes
    web.post("/api/subscriptions/youtube/sync")
    assert ids(web, "youtube")["Beta Cooking"]["state"] == "removed" and ids(web, "youtube")["Delta New"]["state"] == "added"
    # keep undoes a pending unsubscribe; then unsubscribe again and push
    web.post("/api/subscriptions/youtube/edit", json={"ops": [{"op": "keep", "ids": [b]}]})
    assert ids(web, "youtube")["Beta Cooking"]["state"] == "synced"
    web.post("/api/subscriptions/youtube/edit", json={"ops": [{"op": "unsubscribe", "ids": [b]}]})
    r = web.post("/api/subscriptions/youtube/sync?push=true").json()
    assert sorted((x["action"], x["name"]) for x in r["pushed"]) == [("subscribe", "Delta New"), ("unsubscribe", "Beta Cooking")]
    assert r["push_failed"] == [] and "sub-UC" + "b" * 22 not in fake.subs and f"sub-{NEW_CH}" in fake.subs
    subs = ids(web, "youtube")
    assert subs["Delta New"]["state"] == "synced" and subs["Beta Cooking"]["gone"], "kept as a record, marked gone"
    assert subs["Beta Cooking"]["tags"] == ["fav"], "with its tags"
    # forget drops Stash's copy only
    web.post("/api/subscriptions/youtube/edit", json={"ops": [{"op": "forget", "ids": [b]}]})
    assert "Beta Cooking" not in ids(web, "youtube")
    export = web.get("/api/subscriptions/export")
    assert export.headers["content-disposition"].startswith("attachment") and len(export.json()["subscriptions"]) == 3


def test_activity_and_stash_only_playlists(api, stash, fake):
    web = stash["web"]
    connect(stash, "youtube")
    web.post("/api/subscriptions/youtube/sync")
    r = web.post("/api/subscriptions/youtube/activity", json={"ids": []}).json()
    assert r["checked"] == 3 and r["failed"] == []
    quiet = ids(web, "youtube", inactive_days=365, sort="activity")
    assert list(quiet) == ["Gamma Quiet", "Beta Cooking"], "no uploads at all first, then the oldest"
    assert ids(web, "youtube")["Alpha Music"]["last_activity"] > 0
    # a YouTube playlist saved from someone else: kept only in Stash, never pushed
    r = web.post("/api/subscriptions/youtube/edit", json={"ops": [
        {"op": "subscribe", "items": ["https://www.youtube.com/playlist?list=PLsomeoneelse12345"]}]}).json()
    assert r["kept_local"] == 1 and r["subscribed"] == 0
    mix = ids(web, "youtube", kind="playlist")["Someone's mix"]
    assert mix["state"] == "local" and mix["owner"] == "Someone" and mix["item_count"] == 42
    assert web.get("/api/subscriptions/youtube/pending").json()["subscribe"] == []
    fake.calls.clear()
    web.post("/api/subscriptions/youtube/sync?push=true")
    assert not [c for c in fake.calls if c.startswith("POST")], "nothing sent to YouTube for it"
    assert ids(web, "youtube", kind="playlist")["Someone's mix"]["state"] == "local", "and a fetch leaves it alone"
    stats = web.get("/api/subscriptions").json()["stats"]["youtube"]
    assert stats["local"] == 1 and stats["by_kind"] == {"channel": 3, "playlist": 1}


def test_scheduled_activity_check(api, stash, fake):
    """media-sync looks up the channels not checked for a day, the longest-waiting first, and leaves the rest."""
    from app import db, jobs
    from app.main import Ctx

    web = stash["web"]
    connect(stash, "youtube")
    web.post("/api/subscriptions/youtube/sync")
    con = db.connect()
    c = Ctx(con, con.execute("SELECT * FROM users WHERE username='tester'").fetchone())
    fake.calls.clear()
    r = jobs._stale_activity(c)
    assert r["checked"] == 3 and r["failed"] == [], r
    assert jobs._stale_activity(c)["total"] == 0, "nothing is due again for a day"
    con.execute("UPDATE subscriptions SET activity_checked=activity_checked-90000 WHERE title='Beta Cooking'")
    con.execute("UPDATE subscriptions SET activity_checked=NULL WHERE title='Gamma Quiet'")
    con.commit()
    monkey = jobs.ACTIVITY_BATCH
    jobs.ACTIVITY_BATCH = 1
    try:
        assert jobs._stale_activity(c)["total"] == 1, "the batch size holds"
        assert con.execute("SELECT activity_checked FROM subscriptions WHERE title='Gamma Quiet'").fetchone()[0], \
            "the never-checked channel goes first"
    finally:
        jobs.ACTIVITY_BATCH = monkey
    assert jobs._stale_activity(c)["checked"] == 1, "then the stale one"
    assert not [x for x in fake.calls if x.startswith("POST")], "the check sends nothing"
    con.close()


def test_spotify_artists_and_followed_playlists(api, stash, fake):
    web = stash["web"]
    connect(stash, "spotify")
    r = web.post("/api/subscriptions/spotify/sync").json()
    assert r["by_kind"] == {"artist": 1, "playlist": 1}, "pl2 is owned by someone else: a followed playlist"
    subs = ids(web, "spotify")
    assert subs["Artist A"]["kind"] == "artist" and subs["Not mine"]["owner"] == "other"
    web.post("/api/subscriptions/spotify/edit", json={"ops": [
        {"op": "unsubscribe", "ids": [subs["Not mine"]["id"]]},
        {"op": "subscribe", "items": ["https://open.spotify.com/artist/" + "b" * 22 + "?si=x"]}]})
    r = web.post("/api/subscriptions/spotify/sync?push=true").json()
    assert len(r["pushed"]) == 2 and r["push_failed"] == []
    assert fake.followed_artists == {"a" * 22, "b" * 22} and fake.unfollowed_playlists == {"pl2"}
    subs = ids(web, "spotify")
    assert subs["Artist B"]["state"] == "synced" and subs["Not mine"]["gone"]
    assert web.get("/api/subscriptions/spotify/pending").json()["quota_units"] == 0


def test_missing_permission_asks_to_connect_again(api, stash, fake):
    web = stash["web"]
    connect(stash, "spotify")
    fake.scope_missing = True
    r = web.post("/api/subscriptions/spotify/sync")
    assert r.status_code == 409 and r.json()["detail"] == "reconnect_needed:spotify"
    err, text = Client(stash["raw"], new_key(stash)["key"]).call("subscriptions_sync", service="spotify")
    assert err and "Connect again" in text
    assert "user-follow-read" in parse_qs(urlsplit(web.post("/api/playlists/accounts/spotify/connect",
                                                            json={"client_id": "c", "client_secret": "s"}).json()["url"]).query)["scope"][0]


def test_mcp_tools(api, stash, fake):
    connect(stash, "youtube")
    mcp = Client(stash["raw"], new_key(stash)["key"])
    err, text = mcp.call("subscriptions_sync", service="youtube")
    assert not err and text == "Fetched 3 subscriptions from youtube (3 channels): 3 new, 0 gone."
    err, text = mcp.call("subscriptions_activity")
    assert not err and text.startswith("Checked the newest upload of 3 of 3 channels")
    err, text = mcp.call("subscriptions_list", service="youtube", inactive_days=365)
    assert not err and text.startswith("2 subscriptions") and "Gamma Quiet" in text and "no uploads" in text
    sid = int(next(line for line in text.splitlines() if "Gamma Quiet" in line).split()[0][1:])
    err, text = mcp.call("subscriptions_edit", service="youtube", ops=[{"op": "unsubscribe", "ids": [sid]},
                                                                      {"op": "tag", "ids": [sid], "add": ["quiet"]}])
    assert not err and "1 unsubscribed" in text and "about 50 YouTube quota units" in text and "push=true" in text
    reader = Client(stash["raw"], new_key(stash, can_write=False)["key"])
    for name, args in (("subscriptions_edit", {"service": "youtube", "ops": [{"op": "keep", "ids": [sid]}]}),
                       ("subscriptions_sync", {"service": "youtube", "push": True}),
                       ("subscriptions_activity", {})):
        err, text = reader.call(name, **args)
        assert err and "read_only_key" in text, name
    err, text = reader.call("subscriptions_list", service="youtube", tag="quiet")
    assert not err and "[removed]" in text
    err, text = mcp.call("subscriptions_sync", service="youtube", push=True)
    assert not err and text.startswith("Pushed 1 changes.") and "sub-UC" + "c" * 22 not in fake.subs
    err, text = mcp.call("subscriptions_overview")
    assert not err and "youtube: 2 channels, 1 gone from the service" in text and "quiet (1)" in text
    for name in ("subscriptions_overview", "subscriptions_list", "subscriptions_edit", "subscriptions_sync", "subscriptions_activity"):
        r = stash["raw"].post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": name, "arguments": {"service": "youtube", "ops": []}}}, headers=ACCEPT)
        assert r.status_code == 401, name
    assert json.loads(stash["web"].get("/api/subscriptions/youtube/progress").text) == {"running": False}
