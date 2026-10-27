"""Watch later is not in YouTube's API but is in Google Takeout: its CSV becomes a playlist kept only in Stash,
with titles from YouTube, edited at once, never pushed, left alone by fetches, updated by a new import."""
from test_playlists import connect, fake  # noqa: F401  (the fixture)

from app import playlists

OLD_TAKEOUT = """Playlist Id,Channel Id,Time Created,Time Updated,Title,Description,Visibility
WL,UCxyz,2016-01-01 10:00:00 UTC,2026-10-01 10:00:00 UTC,Watch later,,Private

Video Id,Time Added
vidvidvid01,2026-09-01 10:00:00 UTC
vidvidvid09,2026-09-02 11:00:00 UTC
vidvidvid01,2026-09-03 11:00:00 UTC
not-an-id,2026-09-04 11:00:00 UTC
"""
NEW_TAKEOUT = "﻿Video ID,Playlist Video Creation Timestamp\nvidvidvid03,2026-10-01T08:00:00+00:00\nvidvidvid04,2026-10-02T08:00:00+00:00\n"


def test_parse_both_takeout_formats():
    old = playlists.parse_takeout_csv(OLD_TAKEOUT)
    assert [v for v, _ in old] == ["vidvidvid01", "vidvidvid09"] and all(when for _, when in old)
    assert [v for v, _ in playlists.parse_takeout_csv(NEW_TAKEOUT)] == ["vidvidvid03", "vidvidvid04"]
    assert playlists.parse_takeout_csv("just,some\ntext,here\n") == []


def test_takeout_watch_later(api, stash, fake):  # noqa: F811
    web = stash["web"]
    connect(stash, "youtube")
    fake.deleted.add("vidvidvid09")
    r = web.post("/api/playlists/takeout", json={"files": [
        {"name": "Watch later-videos.csv", "text": OLD_TAKEOUT}, {"name": "notes.csv", "text": "a,b\n1,2\n"}]}).json()
    assert r["failed"] == [{"file": "notes", "error": "no_video_ids"}]
    imp = r["imported"][0]
    assert imp["file"] == "Watch later" and imp["videos"] == 2 and imp["unavailable"] == 1 and not imp["updated"]
    pid = imp["playlist_id"]
    d = web.get(f"/api/playlists/lists/{pid}").json()
    assert d["playlist"]["local"] and d["playlist"]["url"] == "" and d["playlist"]["service"] == "youtube"
    assert [(x["title"], x["unavailable"]) for x in d["items"]] == [("Video 01", False), ("vidvidvid09", True)]
    # edits take effect at once; there is nothing to push
    web.post(f"/api/playlists/lists/{pid}/edit", json={"ops": [
        {"op": "remove", "ids": [d["items"][1]["id"]]}, {"op": "add", "items": ["https://youtu.be/vidvidvid05"], "position": 0}]})
    d = web.get(f"/api/playlists/lists/{pid}").json()
    assert [x["title"] for x in d["items"]] == ["Video 05", "Video 01"] and not d["playlist"]["dirty"]
    assert all(x["state"] == "synced" for x in d["items"])
    assert web.post(f"/api/playlists/lists/{pid}/push").status_code == 409
    # a fetch of the account leaves it alone; fetching it alone reads the titles again
    web.post("/api/playlists/accounts/youtube/sync")
    assert not web.get(f"/api/playlists/lists/{pid}").json()["playlist"]["gone"]
    fake.renamed["vidvidvid05"] = "Video 05 (new name)"
    assert web.post(f"/api/playlists/lists/{pid}/sync").status_code == 200
    assert web.get(f"/api/playlists/lists/{pid}").json()["items"][0]["title"] == "Video 05 (new name)"
    # a new Takeout of the same list replaces its contents
    r = web.post("/api/playlists/takeout", json={"files": [{"name": "Watch later-videos.csv", "text": NEW_TAKEOUT}]}).json()
    assert r["imported"][0]["updated"] and r["imported"][0]["playlist_id"] == pid
    assert [x["item_id"] for x in web.get(f"/api/playlists/lists/{pid}").json()["items"]] == ["vidvidvid03", "vidvidvid04"]
    assert stash["raw"].post("/api/playlists/takeout", json={"files": [{"name": "x.csv", "text": NEW_TAKEOUT}]},
                             headers={"X-Stash": "1"}).status_code == 401, "needs a session"
