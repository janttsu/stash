"""API keys and /api/v1 against a throwaway database:  .venv/bin/python tests/api_v1.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_INSECURE_COOKIE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db  # noqa: E402
from app.main import app  # noqa: E402


def ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}"
    return r.json()


with TestClient(app):
    web = TestClient(app, headers={"X-Stash": "1"})
    cli.main(["invite"])
    code = db.connect().execute("SELECT code FROM invites").fetchone()["code"]
    ok(web.post("/api/register", json={"username": "alice", "password": "correct horse", "invite": code}))
    cli.main(["invite"])
    code2 = db.connect().execute("SELECT code FROM invites WHERE used_by IS NULL").fetchone()["code"]
    other = TestClient(app, headers={"X-Stash": "1"})
    ok(other.post("/api/register", json={"username": "bob", "password": "bob password", "invite": code2}))
    bob_bm = ok(other.post("/api/bookmarks", json={"url": "https://bob.example/", "tags": ["x"]}))["id"]

    # --- keys ---
    ok(TestClient(app).post("/api/keys", json={"name": "k"}), 403)  # the web API keeps its CSRF header
    rkey = ok(web.post("/api/keys", json={"name": "lukija"}))["key"]
    wkey = ok(web.post("/api/keys", json={"name": "kone", "can_write": True}))["key"]
    assert rkey.startswith("stash_") and len(rkey) > 40
    keys = ok(web.get("/api/keys"))["keys"]
    assert [k["name"] for k in keys] == ["lukija", "kone"] and "key_hash" not in keys[0]
    assert keys[0]["prefix"] == rkey[:12] and keys[1]["can_write"] and not keys[0]["can_write"]
    stored = db.connect().execute("SELECT key_hash FROM api_keys").fetchall()
    assert all(rkey not in r[0] and wkey not in r[0] for r in stored), "only hashes are stored"

    R = TestClient(app, headers={"Authorization": f"Bearer {rkey}"})
    W = TestClient(app, headers={"Authorization": f"Bearer {wkey}"})
    anon = TestClient(app)
    ok(anon.get("/api/v1/me"), 401)
    ok(TestClient(app, headers={"Authorization": "Bearer stash_nope"}).get("/api/v1/me"), 401)
    ok(web.get("/api/v1/me"), 401)  # a session cookie is not enough here
    assert ok(R.get("/api/v1/me")) == {"user": "alice", "key": "lukija", "can_write": False, "bookmarks": 0}

    # --- creating through changes ---
    res = ok(W.post("/api/v1/changes", json={"summary": "adding", "ops": [
        {"op": "create", "url": "https://acme.example/docs", "title": "ACME docs", "tags": ["Acme", "docs"]},
        {"op": "create", "url": "https://www.acme.example/blog", "title": "ACME blog", "tags": ["acme"],
         "tab": "Work", "category": "Acme"},
        {"op": "create", "url": "https://news.example/", "title": "News", "tags": ["news"]},
        {"op": "create", "url": "https://news.example/", "title": "News again"},
    ]}))
    assert res["counts"]["created"] == 3 and res["skipped"][0]["reason"] == "exists"
    assert res["created_tabs"] and res["created_categories"] and res["changeset"]
    ok(R.post("/api/v1/changes", json={"ops": [{"op": "delete", "ids": [1]}]}), 403)  # read-only key
    snap = ok(R.get("/api/v1/snapshot"))
    by_title = {b["title"]: b for b in snap["bookmarks"]}
    assert len(snap["bookmarks"]) == 3, "bob's bookmark is not visible"
    assert by_title["ACME blog"]["tab"] == "Work" and by_title["ACME blog"]["category"] == "Acme"
    assert by_title["ACME docs"]["tags"] == ["acme", "docs"] and by_title["ACME docs"]["category_id"] is None
    assert ["acme", 2] in snap["tags"]
    assert [t["name"] for t in snap["tabs"]][-1] == "Work"
    docs, blog, news = by_title["ACME docs"]["id"], by_title["ACME blog"]["id"], by_title["News"]["id"]

    # --- reading ---
    assert ok(R.get("/api/v1/bookmarks", params={"host": "acme.example"}))["total"] == 2
    assert ok(R.get("/api/v1/bookmarks", params={"q": "acme blog"}))["total"] == 1
    assert ok(R.get("/api/v1/bookmarks", params={"q": "acme news", "mode": "any"}))["total"] == 3
    assert ok(R.get("/api/v1/bookmarks", params={"tags": "acme,docs"}))["total"] == 1
    assert ok(R.get("/api/v1/bookmarks", params={"ids": f"{docs},{bob_bm}"}))["total"] == 1
    assert ok(R.get("/api/v1/structure"))["catalog"] == 2

    # --- dry run changes nothing and shows the diff ---
    ops = {"summary": "acme -> company-x", "ops": [
        {"op": "rename_tag", "old": "acme", "new": "company-x"},
        {"op": "add_tags", "ids": [news, bob_bm], "tags": ["to-read"]},
        {"op": "move", "ids": [docs], "tab": "Work", "category": "Acme"},
    ]}
    dry = ok(R.post("/api/v1/changes", json={**ops, "dry_run": True}))  # dry runs are fine with a read key
    assert dry["dry_run"] and dry["changeset"] is None and dry["counts"]["updated"] == 3
    assert dry["skipped"][0]["ids"] == [bob_bm]
    d = {e["id"]: e for e in dry["diff"]}
    assert d[docs]["tags"] == [["acme", "docs"], ["company-x", "docs"]] and d[docs]["location"] == ["Catalog", "Work / Acme"]
    assert ok(R.get("/api/v1/bookmarks", params={"tags": "acme"}))["total"] == 2, "dry run rolled back"
    real = ok(W.post("/api/v1/changes", json=ops))
    assert real["changeset"] and real["counts"] == dry["counts"]
    assert ok(R.get("/api/v1/bookmarks", params={"tags": "company-x"}))["total"] == 2
    assert ok(R.get("/api/v1/bookmarks", params={"q": "company-x"}))["total"] == 2, "search text reindexed"
    assert ok(other.get("/api/tags"))["tags"] == [["x", 1]], "bob untouched"

    # --- delete, then undo ---
    gone = ok(W.post("/api/v1/changes", json={"summary": "cleanup", "ops": [{"op": "delete", "ids": [docs, news]}]}))
    assert gone["counts"]["deleted"] == 2
    assert ok(R.get("/api/v1/me"))["bookmarks"] == 1
    hist = ok(R.get("/api/v1/changes"))["changes"]
    assert [h["summary"] for h in hist] == ["cleanup", "acme -> company-x", "adding"]
    ok(W.post(f"/api/v1/changes/{real['changeset']}/undo"), 409)  # a later change touched the same bookmarks
    ok(R.post(f"/api/v1/changes/{gone['changeset']}/undo"), 403)
    assert ok(W.post(f"/api/v1/changes/{gone['changeset']}/undo")) == {"restored": 2, "removed": 0}
    ok(W.post(f"/api/v1/changes/{gone['changeset']}/undo"), 409)
    back = {b["id"]: b for b in ok(R.get("/api/v1/snapshot"))["bookmarks"]}
    assert back[docs]["tags"] == ["company-x", "docs"] and back[docs]["category"] == "Acme"
    assert back[news]["tags"] == ["news", "to-read"]
    # now the rename can be undone, through the web UI's route
    ok(web.post(f"/api/changes/{real['changeset']}/undo"))
    back = {b["id"]: b for b in ok(R.get("/api/v1/snapshot"))["bookmarks"]}
    assert back[docs]["tags"] == ["acme", "docs"] and back[docs]["category_id"] is None
    assert back[news]["tags"] == ["news"] and back[blog]["tags"] == ["acme"]
    # undoing the creation removes the created bookmarks, category and tab
    first = hist[-1]["id"]
    assert ok(W.post(f"/api/v1/changes/{first}/undo")) == {"restored": 0, "removed": 3}
    assert ok(R.get("/api/v1/snapshot"))["tabs"] == [t for t in ok(R.get("/api/v1/structure"))["tabs"]]
    assert "Work" not in [t["name"] for t in ok(R.get("/api/v1/structure"))["tabs"]]
    assert ok(web.get("/api/changes"))["changes"][0]["undone_at"]

    # --- updating one bookmark's address and title ---
    upd = ok(W.post("/api/v1/changes", json={"ops": [
        {"op": "update", "id": bob_bm, "url": "https://x.example/"},  # someone else's: skipped
        {"op": "create", "url": "https://old.example/a", "title": "Old"}]}))
    new_id = upd["diff"][0]["id"]
    fix = ok(W.post("/api/v1/changes", json={"ops": [{"op": "update", "id": new_id, "url": "https://new.example/a",
                                                     "title": "New"}]}))
    assert fix["counts"]["updated"] == 1 and fix["diff"][0]["fields"]["url"] == ["https://old.example/a", "https://new.example/a"]
    assert fix["diff"][0]["fields"]["title"] == ["Old", "New"]
    assert fix["diff"][0]["title"] == "New" and fix["diff"][0]["url"] == "https://new.example/a", "current values stay strings"
    assert ok(R.get("/api/v1/bookmarks", params={"host": "new.example"}))["total"] == 1
    same = ok(W.post("/api/v1/changes", json={"ops": [{"op": "update", "id": new_id, "title": "New"}]}))
    assert same["counts"]["unchanged"] == 1 and same["changeset"] is None

    # --- validation ---
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "move", "ids": [1]}]}), 422)
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "add_tags", "ids": [1], "tags": [" "]}]}), 422)
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "create", "url": "javascript:alert(1)"}]}), 422)
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "frobnicate"}]}), 422)
    bob_cat = db.connect().execute("SELECT c.id FROM categories c JOIN users u ON u.id=c.user_id WHERE u.username='bob'").fetchone()[0]
    ok(W.post("/api/v1/changes", json={"ops": [{"op": "move", "ids": [blog], "category_id": bob_cat}]}), 404)
    ok(R.get(f"/api/v1/changes/{hist[0]['id']}"))
    ok(TestClient(app, headers={"Authorization": f"Bearer {wkey}"}).get("/api/v1/changes/99999"), 404)

    # --- revoking ---
    kid = ok(web.get("/api/keys"))["keys"][1]["id"]
    ok(web.delete(f"/api/keys/{kid}"))
    ok(W.get("/api/v1/me"), 401)
    ok(anon.get("/dl/../../etc/passwd"), 404)
    ok(anon.get("/dl/stashai-9.9-py3-none-any.whl"), 404)

print("all api v1 tests passed")
