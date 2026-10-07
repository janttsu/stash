"""The trash against a throwaway database:  .venv/bin/python tests/trash.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_INSECURE_COOKIE"] = "1"
os.environ["STASH_MAINTENANCE"] = "off"

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db  # noqa: E402
from app.main import TRASH_DAYS, app, purge_trash  # noqa: E402


def ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}"
    return r.json()


with TestClient(app):
    web = TestClient(app, headers={"X-Stash": "1"})
    cli.main(["invite"])
    code = db.connect().execute("SELECT code FROM invites").fetchone()["code"]
    ok(web.post("/api/register", json={"username": "alice", "password": "correct horse", "invite": code}))
    ok(web.put("/api/admin/config", json={"registration": "open"}))
    other = TestClient(app, headers={"X-Stash": "1"})
    ok(other.post("/api/register", json={"username": "bob", "password": "bob password"}))

    def dash():
        return ok(web.get("/api/dashboard"))

    def bookmarks():
        return {b["url"]: b for b in dash()["bookmarks"]}

    tab = ok(web.post("/api/tabs", json={"name": "Health"}))["id"]
    cat = ok(web.post("/api/categories", json={"tab_id": tab, "name": "Knee"}))["id"]
    knee = ok(web.post("/api/bookmarks", json={"url": "https://knee.example/", "title": "Knee", "tags": ["polvi"],
                                                "category_id": cat}))["id"]

    # --- one bookmark: to the trash, then back where it was, with its tags ---
    res = ok(web.delete(f"/api/bookmarks/{knee}"))
    assert len(res["trash"]) == 1 and "https://knee.example/" not in bookmarks()
    items = ok(web.get("/api/trash"))
    assert items["days"] == TRASH_DAYS and items["items"][0]["place"] == "Health / Knee"
    back = ok(web.post("/api/trash/restore", json={"ids": res["trash"]}))
    assert back == {"restored": 1, "already_there": 0, "ids": [knee]}, "the old id comes back when free"
    b = bookmarks()["https://knee.example/"]
    assert b["category_id"] == cat and b["tags"] == ["polvi"]
    assert ok(web.get("/api/trash"))["items"] == []

    # --- a whole category: its bookmarks remember the names; restoring makes it again ---
    ok(web.post("/api/bookmarks", json={"url": "https://knee2.example/", "category_id": cat}))
    res = ok(web.delete(f"/api/categories/{cat}"))
    assert len(res["trash"]) == 2
    assert {i["place"] for i in ok(web.get("/api/trash"))["items"]} == {"Health / Knee"}
    ok(web.post("/api/trash/restore", json={"all": True}))
    d = dash()
    again = [k for k in d["categories"] if k["name"] == "Knee"]
    assert len(again) == 1 and again[0]["tab_id"] == tab, "the category is made again on the same tab"
    assert {b["url"] for b in d["bookmarks"] if b["category_id"] == again[0]["id"]} == \
        {"https://knee.example/", "https://knee2.example/"}

    # --- a whole tab: the tab comes back too ---
    res = ok(web.delete(f"/api/tabs/{tab}"))
    assert len(res["trash"]) == 2 and "Health" not in [t["name"] for t in dash()["tabs"]]
    ok(web.post("/api/trash/restore", json={"all": True}))
    assert "Health" in [t["name"] for t in dash()["tabs"]]

    # --- "delete but keep the bookmarks" moves them to the Catalog: nothing goes to the trash ---
    cat2 = next(k["id"] for k in dash()["categories"] if k["name"] == "Knee")
    assert ok(web.delete(f"/api/categories/{cat2}?bookmarks=catalog"))["trash"] == []

    # --- many at once, an API key, and saving the same address again ---
    ids = [ok(web.post("/api/bookmarks", json={"url": f"https://many{i}.example/"}))["id"] for i in range(3)]
    res = ok(web.post("/api/bookmarks/bulk", json={"action": "delete", "ids": ids}))
    assert res["count"] == 3 and len(res["trash"]) == 3
    ok(web.post("/api/bookmarks", json={"url": "https://many0.example/"}))
    assert len(ok(web.get("/api/trash"))["items"]) == 2, "saved again: no longer in the trash"
    key = ok(web.post("/api/keys", json={"name": "k", "can_write": True}))["key"]
    W = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    gone = db.connect().execute("SELECT id FROM bookmarks WHERE url='https://knee2.example/'").fetchone()[0]
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "delete", "ids": [gone]}]}))
    assert len(ok(web.get("/api/trash"))["items"]) == 3
    # restoring something that was saved again in the meantime does not make a copy
    ok(web.post("/api/bookmarks", json={"url": "https://many1.example/"}))
    con = db.connect()
    con.execute("INSERT INTO trash(user_id, bookmark_id, title, url, created_at, deleted_at)"
                " SELECT id, 999999, 'x', 'https://many1.example/', 0, 0 FROM users WHERE username='alice'")
    stale = con.execute("SELECT id FROM trash WHERE url='https://many1.example/'").fetchone()[0]
    assert ok(web.post("/api/trash/restore", json={"ids": [stale]}))["already_there"] == 1

    # --- delete for good, and the 30-day limit ---
    first = ok(web.get("/api/trash"))["items"][0]["id"]
    assert ok(web.post("/api/trash/delete", json={"ids": [first]}))["deleted"] == 1
    con.execute("UPDATE trash SET deleted_at=?", (db.now() - (TRASH_DAYS + 1) * 86400,))
    assert purge_trash(con) >= 1 and ok(web.get("/api/trash"))["items"] == []
    ok(web.post("/api/bookmarks", json={"url": "https://last.example/"}))
    ok(web.post("/api/bookmarks/bulk", json={"action": "delete", "filter": {"q": "last"}}))
    assert ok(web.post("/api/trash/delete", json={"all": True}))["deleted"] == 1

    # --- each user sees only their own trash; deleting an account leaves no trash behind ---
    bid = ok(other.post("/api/bookmarks", json={"url": "https://bob.example/"}))["id"]
    ok(other.delete(f"/api/bookmarks/{bid}"))
    assert ok(web.get("/api/trash"))["items"] == [] and len(ok(other.get("/api/trash"))["items"]) == 1
    ok(web.post("/api/trash/restore", json={"ids": [ok(other.get("/api/trash"))["items"][0]["id"]]}))
    assert ok(other.get("/api/trash"))["items"], "someone else's item is not touched"
    ok(other.post("/api/bookmarks", json={"url": "https://bob2.example/"}))
    ok(other.post("/api/account/delete", json={"password": "bob password"}))
    assert con.execute("SELECT count(*) FROM trash WHERE user_id NOT IN (SELECT id FROM users)").fetchone()[0] == 0

print("all trash tests passed")
