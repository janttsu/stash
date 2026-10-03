"""Browsing history: the sync script against a fake Firefox profile, and the API:  .venv/bin/python tests/history.py"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_INSECURE_COOKIE"] = "1"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="stash-cfg-")

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db  # noqa: E402
from app.main import app  # noqa: E402  (before app.history, which it imports at its end)
from app.history import url_key  # noqa: E402,I001

spec = importlib.util.spec_from_file_location("sync", ROOT / "scripts" / "stash-history-sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}"
    return r.json()


def fake_profile() -> Path:
    """A Firefox profile with the parts of places.sqlite the script reads."""
    root = Path(tempfile.mkdtemp(prefix="ff-")) / "Profiles" / "abcd.default-release"
    root.mkdir(parents=True)
    con = sqlite3.connect(root / "places.sqlite")
    con.executescript("""
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER DEFAULT 0,
                                 hidden INTEGER DEFAULT 0, last_visit_date INTEGER);
        CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER, visit_type INTEGER);
    """)
    now, day = int(time.time() * 1e6), 86400 * 10**6
    places = [  # url, title, [(days ago, visit type)], hidden
        ("https://www.mail.example/inbox", "Mail", [(1, 1)] * 20 + [(60, 2)] * 10 + [(200, 1)] * 5, 0),
        ("https://wiki.example/page?token=SECRET&id=7#top", "Wiki page", [(10, 1)] * 3, 0),
        ("https://user:pw@intra.example/x", "Intra", [(5, 2)], 0),
        ("https://bank.example/account", "Bank", [(2, 1)] * 9, 0),
        ("https://news.example/", "News", [(3, 1)] * 4 + [(3, 9)] * 50, 0),   # reloads do not count
        ("https://ads.example/frame", "Ad", [(1, 4)] * 30, 0),                  # embeds only
        ("https://redirect.example/", "", [(1, 1)], 1),                         # hidden
        ("https://old.example/", "Old", [(800, 1)], 0),                         # older than --days
        ("file:///Users/me/notes.txt", "notes", [(1, 1)], 0),
    ]
    for pid, (url, title, visits, hidden) in enumerate(places, 1):
        last = max(now - d * day for d, _ in visits)
        con.execute("INSERT INTO moz_places VALUES(?,?,?,?,?,?)", (pid, url, title, len(visits), hidden, last))
        con.executemany("INSERT INTO moz_historyvisits(place_id, visit_date, visit_type) VALUES(?,?,?)",
                        [(pid, now - d * day, t) for d, t in visits])
    con.commit()
    con.close()
    (root.parent.parent / "profiles.ini").write_text(
        "[Install4F96D1932A9F858E]\nDefault=Profiles/abcd.default-release\n\n"
        "[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/abcd.default-release\n")
    return root


# --- the script reads Firefox ---
profile = fake_profile()
items = {i["url"]: i for i in sync.read_history(profile, 365, ["bank.example"])}
assert set(items) == {"https://www.mail.example/inbox", "https://wiki.example/page?id=7",
                      "https://intra.example/x", "https://news.example/"}, items.keys()
mail = items["https://www.mail.example/inbox"]
assert (mail["visits_30d"], mail["visits_90d"], mail["visits_365d"], mail["visits"]) == (20, 30, 35, 35)
assert items["https://news.example/"]["visits_30d"] == 4, "reloads are not visits"
assert sync.clean_url("https://a.example/?q=x&access_token=1&sessionid=2&code=3&page=2#f") == "https://a.example/?q=x&page=2"
sync.firefox_roots = lambda: [profile.parent.parent]
assert sync.find_profile(None) == profile

with TestClient(app):
    web = TestClient(app, headers={"X-Stash": "1"})
    cli.main(["invite"])
    code = db.connect().execute("SELECT code FROM invites").fetchone()["code"]
    ok(web.post("/api/register", json={"username": "alice", "password": "correct horse", "invite": code}))
    wkey = ok(web.post("/api/keys", json={"name": "mac", "can_write": True}))["key"]
    rkey = ok(web.post("/api/keys", json={"name": "read", "can_write": False}))["key"]
    W = TestClient(app, headers={"Authorization": f"Bearer {wkey}"})
    R = TestClient(app, headers={"Authorization": f"Bearer {rkey}"})
    bm = ok(W.post("/api/v1/changes", json={"ops": [
        {"op": "create", "url": "http://mail.example/inbox/", "title": "Mail"},
        {"op": "create", "url": "https://wiki.example/page?id=7", "title": "Wiki"},
        {"op": "create", "url": "https://never.example/", "title": "Never visited"}]}))
    ids = {e["title"]: e["id"] for e in bm["diff"]}

    # --- the script sends it (through the real API, in chunks) ---
    def fake_call(cfg, method, path, body=None):
        client = W if cfg["key"] == wkey else R
        r = client.request(method, "/api/v1" + path, content=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        return r.json()
    sync.call = fake_call
    sync.CHUNK = 2
    cfg = {"url": "https://stash.test", "key": wkey, "source": "macbook", "exclude": ["bank.example"]}
    sync.sync(SimpleNamespace(profile=str(profile), days=365, exclude=[], dry_run=False), cfg)
    src = ok(R.get("/api/v1/history/sources"))["sources"]
    assert src[0]["source"] == "macbook" and src[0]["items"] == 4 and src[0]["synced_at"] and src[0]["browser"] == "firefox"
    stored = [r[0] for r in db.connect().execute("SELECT url FROM history")]
    assert not any("SECRET" in u or "pw@" in u or "bank" in u for u in stored), stored

    # --- usage of bookmarks, matched loosely ---
    use = ok(R.get("/api/v1/history/usage"))["usage"]
    assert use[str(ids["Mail"])]["visits_30d"] == 20 and use[str(ids["Wiki"])]["visits_90d"] == 3
    assert str(ids["Never visited"]) not in use
    assert url_key("http://www.Mail.example/inbox/") == url_key("https://mail.example/inbox")

    # --- searching the history ---
    res = ok(R.get("/api/v1/history", params={"bookmarked": "no"}))["items"]
    assert [i["url"] for i in res] == ["https://news.example/", "https://intra.example/x"]
    res = ok(R.get("/api/v1/history", params={"q": "wiki", "period": "all"}))["items"]
    assert res[0]["bookmarks"] == [ids["Wiki"]]
    assert ok(R.get("/api/v1/history", params={"host": "mail.example", "min_visits": 25}))["total"] == 1
    assert ok(R.get("/api/v1/history", params={"min_visits": 25, "period": "30d"}))["total"] == 0

    # --- a second device adds up; a new sync replaces only its own rows ---
    ok(W.post("/api/v1/history/work laptop", json={"reset": True, "done": True, "browser": "firefox", "items": [
        {"url": "https://mail.example/inbox", "visits": 5, "visits_30d": 5, "visits_90d": 5, "visits_365d": 5}]}))
    assert ok(R.get("/api/v1/history/usage"))["usage"][str(ids["Mail"])]["visits_30d"] == 25
    sync.sync(SimpleNamespace(profile=str(profile), days=365, exclude=[], dry_run=False), cfg)
    assert ok(R.get("/api/v1/history/usage"))["usage"][str(ids["Mail"])]["visits_30d"] == 25, "no double counting"

    # --- permissions, deleting ---
    ok(R.post("/api/v1/history/x", json={"items": []}), 403)
    ok(W.post("/api/v1/history/bad%3Cname%3E", json={"items": []}), 422)
    assert [s["source"] for s in ok(web.get("/api/history/sources"))["sources"]] == ["macbook", "work laptop"]
    ok(web.delete("/api/history/work laptop"))
    sync.sync.__globals__["load_config"] = lambda: cfg
    assert ok(W.delete("/api/v1/history/macbook"))["deleted"] == 4
    assert ok(R.get("/api/v1/history/usage"))["usage"] == {}
    assert ok(R.get("/api/v1/history/sources"))["sources"] == []
    assert "history" not in ok(R.get("/api/v1/snapshot")), "history is not part of the bookmark data"
    assert TestClient(app).get("/dl/stash-history-sync.py").status_code == 200  # downloadable without signing in
    assert TestClient(app).get("/dl/stash-history-sync.py").text.startswith("#!/usr/bin/env python3")

    # --- ordering: bookmarks inside a category, categories inside a tab, both undoable ---
    tab = ok(R.get("/api/v1/structure"))["tabs"][0]
    cat_ids = [c["id"] for c in tab["categories"]]
    extra = ok(W.post("/api/v1/changes", json={"ops": [
        {"op": "move", "ids": list(ids.values()), "category_id": cat_ids[0]},
        {"op": "create", "url": "https://c2.example/", "title": "C2", "tab": tab["name"], "category": "Toinen"},
        {"op": "create", "url": "https://c3.example/", "title": "C3", "tab": tab["name"], "category": "Kolmas"}]}))
    tab = ok(R.get("/api/v1/structure"))["tabs"][0]
    order = lambda: [b["title"] for b in sorted(  # noqa: E731
        (b for b in ok(R.get("/api/v1/snapshot"))["bookmarks"] if b["category_id"] == cat_ids[0]),
        key=lambda b: b["position"])]
    assert order() == ["Mail", "Wiki", "Never visited"]
    res = ok(W.post("/api/v1/changes", json={"summary": "järjestys", "ops": [
        {"op": "order_bookmarks", "category_id": cat_ids[0], "ids": [ids["Never visited"], 999999]},
        {"op": "order_categories", "tab": tab["name"], "categories": [c["id"] for c in reversed(tab["categories"])]}]}))
    assert order() == ["Never visited", "Mail", "Wiki"]
    pos = {e["title"]: e.get("position") for e in res["diff"]}
    assert pos["Never visited"] == [3, 1] and pos["Mail"] == [1, 2]
    assert res["skipped"] == [{"reason": "not_in_category", "count": 1}]
    cols = {}
    for c in ok(R.get("/api/v1/structure"))["tabs"][0]["categories"]:
        cols.setdefault(c["col"], []).append(c["name"])
    before_cols = {}
    for c in tab["categories"]:
        before_cols.setdefault(c["col"], []).append(c["name"])
    assert {k: v for k, v in cols.items()} == {k: list(reversed(v)) for k, v in before_cols.items()}, (cols, before_cols)
    assert res["categories"] or all(len(v) == 1 for v in cols.values())
    ok(W.post(f"/api/v1/changes/{res['changeset']}/undo"))
    assert order() == ["Mail", "Wiki", "Never visited"]
    assert [c["name"] for c in ok(R.get("/api/v1/structure"))["tabs"][0]["categories"]] == [c["name"] for c in tab["categories"]]
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "order_bookmarks", "tab": "Nope", "category": "X", "ids": []}]}), 404)

print("all history tests passed")
