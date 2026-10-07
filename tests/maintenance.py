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

    # --- only titles and icons are looked after: duplicates and dead links are not touched any more ---
    assert not hasattr(maintenance, "reconcile_duplicates") and not hasattr(maintenance, "check_dead_batch")

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
