"""Background upkeep against a throwaway database:  .venv/bin/python tests/maintenance.py"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_INSECURE_COOKIE"] = "1"
os.environ["STASH_MAINTENANCE"] = "off"  # we drive the cycle by hand, not the loop

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db, maintenance, net  # noqa: E402
from app.main import FAVICONS, app  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"icon-bytes"


def ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}"
    return r.json()


def tags(con, bid):
    return sorted(r[0] for r in con.execute("SELECT tag FROM bookmark_tags WHERE bookmark_id=?", (bid,)))


def run(coro):
    return asyncio.run(coro)


with TestClient(app):
    web = TestClient(app, headers={"X-Stash": "1"})
    cli.main(["invite"])
    code = db.connect().execute("SELECT code FROM invites").fetchone()["code"]
    ok(web.post("/api/register", json={"username": "alice", "password": "correct horse", "invite": code}))

    def add(url, title=None):
        return ok(web.post("/api/bookmarks", json={"url": url, "title": title} if title else {"url": url}))["id"]

    con = db.connect()
    uid = con.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    assert maintenance.active_user_ids(con) == [uid]

    # --- dead links: tagged only after DEAD_THRESHOLD failures in a row, cleared when alive again ---
    dead_url = "https://gone.example/page"
    bid = add(dead_url)
    DEAD = {dead_url}

    async def fake_dead(client, url):
        return url in DEAD

    net.is_dead = fake_dead
    for n in range(maintenance.DEAD_THRESHOLD):
        con.execute("UPDATE bookmarks SET checked_at=NULL")  # pretend the recheck interval has passed
        run(maintenance.check_dead_batch(con, None, [uid]))
        streak, dead = con.execute("SELECT dead_streak, dead FROM bookmarks WHERE id=?", (bid,)).fetchone()
        expected_tag = n + 1 >= maintenance.DEAD_THRESHOLD
        assert streak == n + 1, (streak, n)
        assert bool(dead) == expected_tag and (maintenance.DEAD_TAG in tags(con, bid)) == expected_tag

    DEAD.clear()  # the page answers again
    con.execute("UPDATE bookmarks SET checked_at=NULL")
    run(maintenance.check_dead_batch(con, None, [uid]))
    dead, streak = con.execute("SELECT dead, dead_streak FROM bookmarks WHERE id=?", (bid,)).fetchone()
    assert dead == 0 and streak == 0 and maintenance.DEAD_TAG not in tags(con, bid), "cleared when alive"

    # a one-off 404 (streak resets before the threshold) never tags
    DEAD.add(dead_url)
    con.execute("UPDATE bookmarks SET checked_at=NULL")
    run(maintenance.check_dead_batch(con, None, [uid]))
    DEAD.clear()
    con.execute("UPDATE bookmarks SET checked_at=NULL")
    run(maintenance.check_dead_batch(con, None, [uid]))
    assert con.execute("SELECT dead_streak FROM bookmarks WHERE id=?", (bid,)).fetchone()[0] == 0

    # --- duplicates: every copy after the first is tagged, and the tag clears when a copy goes ---
    dup = "https://dup.example/x"
    first, second = add(dup), add(dup)
    maintenance.reconcile_duplicates(con, uid)
    assert maintenance.DUPLICATE_TAG not in tags(con, first), "the oldest copy is not a duplicate"
    assert maintenance.DUPLICATE_TAG in tags(con, second)
    third = add(dup)
    maintenance.reconcile_duplicates(con, uid)
    assert all(maintenance.DUPLICATE_TAG in tags(con, b) for b in (second, third))
    ok(web.delete(f"/api/bookmarks/{second}"))
    ok(web.delete(f"/api/bookmarks/{third}"))
    maintenance.reconcile_duplicates(con, uid)
    assert maintenance.DUPLICATE_TAG not in tags(con, first), "no copies left, so no duplicate tag"

    # --- titles and icons -----------------------------------------------------------
    unnamed = add("https://site.example/a")                 # title defaults to the url
    named = add("https://site.example/b", "My own title")   # a title the user wrote
    blocked = add("https://blocked.example/c")              # the page only gives a junk title
    assert con.execute("SELECT title FROM bookmarks WHERE id=?", (unnamed,)).fetchone()[0] == "https://site.example/a"

    TITLES = {"https://site.example/a": "The Real Title", "https://site.example/b": "Ignored",
              "https://blocked.example/c": "Just a moment..."}

    async def fake_title(client, url):
        return TITLES.get(url, "")

    async def fake_favicon(client, host):
        return PNG

    net.page_title = fake_title
    net.favicon = fake_favicon
    rev0 = db.get_config(con, "favicon_rev", "")
    out = run(maintenance.refresh_meta_batch(con, None, [uid]))
    assert con.execute("SELECT title FROM bookmarks WHERE id=?", (unnamed,)).fetchone()[0] == "The Real Title"
    assert con.execute("SELECT title FROM bookmarks WHERE id=?", (named,)).fetchone()[0] == "My own title", "kept"
    assert con.execute("SELECT title FROM bookmarks WHERE id=?", (blocked,)).fetchone()[0] == "https://blocked.example/c"
    assert out["titled"] == 1 and out["icons_new"] >= 1
    assert (FAVICONS / "site.example").read_bytes() == PNG
    assert db.get_config(con, "favicon_rev", "") != rev0, "a new icon bumps the version"
    # nothing is due a second time right away
    assert run(maintenance.refresh_meta_batch(con, None, [uid]))["checked"] == 0

    # --- the opt-out is respected ---------------------------------------------------
    ok(web.patch("/api/settings", json={"auto_maintain": False}))
    con2 = db.connect()
    assert maintenance.active_user_ids(con2) == [], "a user who turned it off is skipped"
    con2.close()

    con.close()

print("all maintenance tests passed")
