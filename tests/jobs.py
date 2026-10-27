"""Scheduled tasks and the version history, end to end on a throwaway database:
    .venv/bin/python tests/jobs.py
Needs sqlite3 and zstd (as scripts/backup.sh does)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_MAINTENANCE"] = "off"
os.environ["STASH_INSECURE_COOKIE"] = "1"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db, jobs  # noqa: E402
from app.main import app  # noqa: E402


def register(web, name):
    cli.main(["invite"])
    code = db.connect().execute("SELECT code FROM invites WHERE used_by IS NULL").fetchone()["code"]
    assert web.post("/api/register", json={"username": name, "password": "password " + name, "invite": code}).status_code == 200


def add(web, url, **extra):
    r = web.post("/api/bookmarks", json={"url": url, "title": url, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"] if "id" in r.json() else r.json()["bookmark"]["id"]


def tags_of(uid):
    con = db.connect()
    rows = con.execute("SELECT b.url, group_concat(t.tag) AS tags FROM bookmarks b LEFT JOIN bookmark_tags t ON t.bookmark_id=b.id"
                       " WHERE b.user_id=? GROUP BY b.id ORDER BY b.id", (uid,)).fetchall()
    return [(r["url"], r["tags"] or "") for r in rows]


def backup():
    r = subprocess.run([str(ROOT / "scripts" / "backup.sh")], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "STASH_DATA": os.environ["STASH_DATA"]})
    assert r.returncode == 0, r.stderr


def main():
    with TestClient(app):
        web = TestClient(app, headers={"X-Stash": "1"})
        other = TestClient(app, headers={"X-Stash": "1"})
        register(web, "alice")
        register(other, "bob")
        con = db.connect()
        alice, bob = (con.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0] for n in ("alice", "bob"))

        # --- duplicates: the later copies get the tag, never the oldest; http/https count as the same ---------
        add(web, "https://example.com/a")
        time.sleep(1.1)
        add(web, "http://example.com/a")
        add(web, "https://www.example.com/a/")
        add(web, "https://example.com/b", tags=["duplicate"])      # not a duplicate: a stale tag is taken away
        add(other, "https://example.com/a")                        # another account: never compared with alice's
        r = jobs.duplicates()
        assert r["ok"], r
        assert tags_of(alice) == [("https://example.com/a", ""), ("http://example.com/a", "duplicate"),
                                  ("https://www.example.com/a/", "duplicate"), ("https://example.com/b", "")], tags_of(alice)
        assert tags_of(bob) == [("https://example.com/a", "")]
        first = jobs.duplicates()   # again: nothing changes
        assert "(+0 −0)" in first["summary"], first

        # --- the job record and the warning ------------------------------------------------------------------
        status = {j["name"]: j for j in jobs.status(con)}
        assert not status["duplicates"]["overdue"] and status["duplicates"]["ok"]
        assert status["media-sync"]["overdue"] and status["backup"]["overdue"], "never ran"
        me = web.get("/api/me").json()
        assert set(me["jobs_overdue"]) == {"media-sync", "backup", "youtube-push"}, "the administrator (first account) is warned"
        assert "jobs_overdue" not in other.get("/api/me").json(), "other accounts are not"
        r = jobs.media_sync()
        assert r["ok"] and r["summary"] == "no music service connected"
        # as the timers run them: each task in a process of its own (a wrong import order shows only there)
        for name in ("media-sync", "duplicates", "youtube-push"):
            run = subprocess.run([sys.executable, "-m", "app.cli", "job", name], cwd=ROOT, capture_output=True, text=True,
                                 env=os.environ)
            assert run.returncode == 0 and f"{name}: ok" in run.stdout, run.stderr[-800:]
        backup()
        assert web.get("/api/me").json()["jobs_overdue"] == []
        assert {j["name"] for j in web.get("/api/admin").json()["jobs"]} == {"backup", "media-sync", "duplicates", "youtube-push"}
        rec = json.loads(db.get_config(con, "job:duplicates"))
        db.set_config(con, "job:duplicates", json.dumps(rec | {"last_ok": rec["last_ok"] - 29 * 3600}))
        assert web.get("/api/me").json()["jobs_overdue"] == ["duplicates"], "late: the warning comes back"
        assert subprocess.run([sys.executable, "-m", "app.cli", "jobs"], cwd=ROOT, capture_output=True, text=True,
                              env=os.environ).stdout.count("OVERDUE") == 1

        # --- version history: restore one's own bookmarks, nobody else's ------------------------------------
        tab = web.post("/api/tabs", json={"name": "Work"}).json()
        cat = web.post("/api/categories", json={"tab_id": tab["id"], "name": "Docs"}).json()
        add(web, "https://docs.example/one", category_id=cat["id"], tags=["docs"])
        web.patch("/api/settings", json={"assistant_rules": "old rules"})
        time.sleep(61)   # a backup per minute: the names carry minutes
        backup()
        versions = web.get("/api/versions").json()["versions"]
        name = versions[0]["name"]
        before = web.get(f"/api/versions/{name}").json()
        assert before["then"]["bookmarks"] == 5 and before["then"]["tabs"] == 2 and before["now"] == before["then"]
        # things go wrong: the tab is deleted, a bookmark is added, the rules change; bob changes his too
        assert web.delete(f"/api/tabs/{tab['id']}").status_code == 200
        add(web, "https://new.example/")
        web.patch("/api/settings", json={"assistant_rules": "new rules"})
        add(other, "https://bob.example/")
        p = web.get(f"/api/versions/{name}").json()
        assert p["now"]["bookmarks"] == 5 and p["now"]["tabs"] == 1 and p["then"]["tabs"] == 2
        r = web.post(f"/api/versions/{name}/restore", json={"areas": ["bookmarks", "settings"]})
        assert r.status_code == 200, r.text
        r = r.json()
        assert r["restored"]["bookmarks"] > 0 and r["saved_before"].startswith("stash-"), r
        urls = [u for u, _ in tags_of(alice)]
        assert "https://docs.example/one" in urls and "https://new.example/" not in urls
        assert ("https://docs.example/one", "docs") in tags_of(alice), "tags come back"
        structure = web.get("/api/dashboard").json() if web.get("/api/dashboard").status_code == 200 else None
        con2 = db.connect()
        assert "Work" in {r[0] for r in con2.execute("SELECT name FROM tabs WHERE user_id=?", (alice,))}
        assert con2.execute("SELECT COUNT(*) FROM trash WHERE user_id=? AND url='https://new.example/'", (alice,)).fetchone()[0] == 1, \
            "what the restore removed waits in the trash"
        assert json.loads(con2.execute("SELECT settings FROM users WHERE id=?", (alice,)).fetchone()[0])["assistant_rules"] == "old rules"
        assert [u for u, _ in tags_of(bob)] == ["https://example.com/a", "https://bob.example/"], "bob is untouched"
        assert any(v["name"] == r["saved_before"] for v in web.get("/api/versions").json()["versions"]), \
            "the state before the restore is a version too"
        assert web.get("/api/versions/stash-..%2Fetc.db.zst").status_code == 404
        assert web.post(f"/api/versions/{name}/restore", json={"areas": ["nothing"]}).status_code == 422
        assert TestClient(app).get("/api/versions").status_code == 401
        del structure
    print("all jobs and version history tests passed")


if __name__ == "__main__":
    main()
