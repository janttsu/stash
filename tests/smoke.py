"""End-to-end API smoke test against a throwaway database:  .venv/bin/python tests/smoke.py"""
import io
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_INSECURE_COOKIE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db, importer, net, security  # noqa: E402
from app import main as main_module  # noqa: E402
from app.main import app, loose_url  # noqa: E402

H = {"X-Stash": "1"}


def client():
    return TestClient(app, headers=H)


def ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}"
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r


with TestClient(app):  # runs startup (schema creation)
    a, b, anon = client(), client(), client()

    # --- accounts ---
    assert ok(anon.get("/api/public")) == {"registration": "invite", "first_user": True}
    ok(anon.post("/api/register", json={"username": "mallory", "password": "mallory password"}), 403)
    assert cli.main(["invite"]) == 0
    first_invite = db.connect().execute("SELECT code FROM invites").fetchone()["code"]
    ok(anon.get("/api/me"), 401)
    r = TestClient(app).post("/api/register", json={"username": "x", "password": "y" * 8})
    assert r.status_code == 403 and r.json()["detail"] == "csrf", "state-changing calls need the X-Stash header"
    ok(a.post("/api/register", json={"username": "alice", "password": "correct horse", "lang": "fi", "invite": first_invite}))
    me = ok(a.get("/api/me"))
    assert me["is_admin"] and me["username"] == "alice"
    assert ok(anon.get("/api/public"))["registration"] == "invite"
    ok(b.post("/api/register", json={"username": "bob", "password": "bob password"}), 403)
    code = ok(a.post("/api/admin/invites"))["code"]
    ok(b.post("/api/register", json={"username": "Alice", "password": "bob password", "invite": code}), 409)
    ok(b.post("/api/register", json={"username": "bob", "password": "bob password", "invite": code}))
    ok(anon.post("/api/register", json={"username": "carol", "password": "carol password", "invite": code}), 403)
    assert not ok(b.get("/api/me"))["is_admin"]
    ok(b.get("/api/admin"), 403)
    ok(anon.post("/api/login", json={"username": "alice", "password": "wrong"}), 401)

    # --- dashboard structure ---
    dash = ok(a.get("/api/dashboard"))
    assert [t["name"] for t in dash["tabs"]] == ["Koti"] and dash["categories"][0]["name"] == "Suosikit"
    tab1, cat1 = dash["tabs"][0]["id"], dash["categories"][0]["id"]
    tab2 = ok(a.post("/api/tabs", json={"name": "Work"}))["id"]
    cat2 = ok(a.post("/api/categories", json={"tab_id": tab1, "name": "News"}))["id"]
    cat3 = ok(a.post("/api/categories", json={"tab_id": tab1, "name": "Äänitteet"}))["id"]
    cols = {c["id"]: c["col"] for c in ok(a.get("/api/dashboard"))["categories"]}
    assert sorted(cols.values()) == [0, 1, 2], "new categories fill the emptiest column"
    ok(a.patch(f"/api/tabs/{tab1}", json={"color": "blue", "columns": 4}))
    ok(a.patch(f"/api/tabs/{tab1}", json={"color": "nope"}), 422)
    ok(a.post("/api/tabs/order", json={"ids": [tab2, tab1]}))
    assert [t["id"] for t in ok(a.get("/api/dashboard"))["tabs"]] == [tab2, tab1]
    ok(a.post(f"/api/tabs/{tab1}/layout", json={"columns": [[cat3, cat1], [], [cat2]]}))
    ok(a.post(f"/api/tabs/{tab1}/sort"))
    ok(a.post(f"/api/tabs/{tab1}/collapse", json={"collapsed": True}))
    assert all(c["collapsed"] for c in ok(a.get("/api/dashboard"))["categories"])
    ok(b.patch(f"/api/tabs/{tab1}", json={"name": "hacked"}), 404)
    ok(b.post("/api/categories", json={"tab_id": tab1, "name": "hacked"}), 404)

    # --- bookmarks ---
    b1 = ok(a.post("/api/bookmarks", json={"url": "example.com/page", "title": "Example", "tags": ["Ref", "ref", "a,b"],
                                           "notes": "Hyvä sivu", "category_id": cat1}))["id"]
    b2 = ok(a.post("/api/bookmarks", json={"url": "https://news.ycombinator.com/", "title": "Hacker News",
                                           "tags": ["news"], "category_id": cat2}))["id"]
    b3 = ok(a.post("/api/bookmarks", json={"url": "jane@example.com", "title": "Jane"}))["id"]
    b4 = ok(a.post("/api/bookmarks", json={"url": "+358 40 000 0000"}))["id"]
    b5 = ok(a.post("/api/bookmarks", json={"url": "https://yle.fi/", "title": "ÄÄNIKIRJAT ja Öljy", "tags": ["news"]}))["id"]
    ok(a.post("/api/bookmarks", json={"url": "javascript:alert(1)"}), 422)
    ok(a.post("/api/bookmarks", json={"url": "data:text/html,x"}), 422)
    ok(a.post("/api/bookmarks", json={"url": "https://x.example", "category_id": cat1}, headers={"X-Stash": "0"}), 403)
    ok(b.post("/api/bookmarks", json={"url": "https://x.example", "category_id": cat1}), 404)
    items = {i["id"]: i for i in ok(a.get("/api/bookmarks"))["items"]}
    assert items[b1]["url"] == "https://example.com/page" and items[b1]["tags"] == ["a b", "ref"]
    assert items[b1]["tab_name"] == "Koti" and items[b1]["category_name"] == "Suosikit"
    assert items[b3]["url"] == "mailto:jane@example.com" and items[b4]["url"] == "tel:+358400000000"
    assert items[b4]["title"] == "tel:+358400000000"
    assert ok(a.get("/api/bookmarks", params={"scope": "catalog"}))["total"] == 3
    assert ok(a.get("/api/bookmarks", params={"scope": "dashboard"}))["total"] == 2
    assert ok(b.get("/api/bookmarks"))["total"] == 0, "users only see their own bookmarks"

    def search(q, mode="exact", **kw):
        return {i["id"] for i in ok(a.get("/api/bookmarks", params={"q": q, "mode": mode, **kw}))["items"]}

    assert search("äänikirjat") == {b5}, "search is case-insensitive beyond ASCII"
    assert search("hyvä SIVU") == {b1}
    assert search("sivu hyvä") == set() and search("sivu hyvä", "all") == {b1}
    assert search("hacker öljy", "any") == {b2, b5} and search("hacker öljy", "all") == set()
    assert search("ref") == {b1} and search("100%") == set() and search("_") == set()
    assert search("", tags="news") == {b2, b5} and search("", tags="news", scope="catalog") == {b5}
    assert search("", untagged="true") == {b3, b4}
    assert ok(a.get("/api/tags"))["tags"][0] == ["news", 2]
    assert ok(a.post("/api/bookmarks/lookup", json={"url": "example.com/page/"}))["bookmark"]["id"] == b1
    assert ok(a.post("/api/bookmarks/lookup", json={"url": "https://nope.example"}))["bookmark"] is None
    assert ok(b.post("/api/bookmarks/lookup", json={"url": "https://example.com/page"}))["bookmark"] is None

    ok(a.patch(f"/api/bookmarks/{b1}", json={"title": "Example 2", "tags": ["x"], "color": "red"}))
    ok(b.patch(f"/api/bookmarks/{b1}", json={"title": "hacked"}), 404)
    ok(b.delete(f"/api/bookmarks/{b1}"))
    assert search("example 2") == {b1} and search("ref") == set()
    ok(a.post(f"/api/categories/{cat2}/order", json={"ids": [b1, b2]}))
    moved = {i["id"]: i for i in ok(a.get("/api/dashboard"))["bookmarks"]}
    assert moved[b1]["category_id"] == cat2 and (moved[b1]["position"], moved[b2]["position"]) == (0, 1)
    ok(b.post(f"/api/categories/{cat2}/order", json={"ids": [b1]}), 404)

    # auto-tagging on the way to the Catalog
    ok(a.patch("/api/settings", json={"auto_tag_catalog": True, "theme": "dark"}))
    ok(a.patch("/api/settings", json={"theme": "neon"}), 422)
    ok(a.patch(f"/api/bookmarks/{b1}", json={"category_id": None}))
    assert set(ok(a.get("/api/bookmarks", params={"q": "example 2"}))["items"][0]["tags"]) == {"x", "koti", "news"}
    assert search("koti") == {b1}, "the search index follows tag changes"

    # bulk operations and tag management
    assert ok(a.post("/api/bookmarks/bulk", json={"action": "add_tags", "ids": [b1, b2, 999999], "tags": ["Bulk"]}))["count"] == 2
    assert ok(b.post("/api/bookmarks/bulk", json={"action": "delete", "ids": [b1, b2]}))["count"] == 0
    assert search("", tags="bulk") == {b1, b2}
    ok(a.post("/api/bookmarks/bulk", json={"action": "replace_tag", "filter": {"tags": ["bulk"]}, "old": "bulk", "new": "massa"}))
    assert search("", tags="massa") == {b1, b2} and search("bulk") == set()
    ok(a.post("/api/bookmarks/bulk", json={"action": "remove_tags", "ids": [b2], "tags": ["massa"]}))
    assert ok(a.post("/api/tags/rename", json={"old": "massa", "new": "news"}))["count"] == 1
    assert search("", tags="news") == {b1, b2, b5}
    ok(a.post("/api/bookmarks/bulk", json={"action": "move", "ids": [b5, b1], "category_id": cat3}))
    assert ok(a.get("/api/bookmarks", params={"scope": "catalog"}))["total"] == 2
    ok(a.post("/api/bookmarks/bulk", json={"action": "move", "ids": [b5], "category_id": 424242}), 404)
    ok(a.post("/api/tags/rename", json={"old": "koti", "new": ""}))
    assert search("koti") == set()

    # --- duplicates ---
    assert net.login_gated("https://docs.google.com/spreadsheets/d/abc/edit") and not net.login_gated("https://notgoogle.com.example/")
    assert net.login_gated("https://prox.intra.example.fi/x") and net.login_gated("https://app.notion.com/p/abc")
    assert loose_url("HTTP://WWW.Example.com/A/") == loose_url("https://example.com/A")
    d1 = ok(a.post("/api/bookmarks", json={"url": "http://www.example.com/page/"}))["id"]
    d2 = ok(a.post("/api/bookmarks", json={"url": "https://example.com/page"}))["id"]
    res = ok(a.post("/api/tools/duplicates", json={"strict": True}))
    assert res["duplicates"] == 1 and search("", tags="duplicate") == {d2}
    res = ok(a.post("/api/tools/duplicates", json={"strict": False, "mark": "both", "tag": "dupe"}))
    assert res["duplicates"] == 2 and search("", tags="dupe") == {b1, d1, d2}
    ok(a.post("/api/bookmarks/bulk", json={"action": "delete", "ids": [d1, d2]}))
    ok(a.post("/api/tags/rename", json={"old": "dupe"}))

    # --- sharing ---
    token = ok(a.put(f"/api/tabs/{tab1}/share", json={"title": "My links", "subtitle": "hello"}))["token"]
    shared = ok(anon.post(f"/api/share/{token}", json={}))
    assert shared["title"] == "My links" and len(shared["bookmarks"]) == 3
    assert "notes" not in shared["bookmarks"][0] and "tags" not in shared["bookmarks"][0]
    ok(a.put(f"/api/tabs/{tab1}/share", json={"title": "", "password": "s3cret"}))
    assert ok(anon.post(f"/api/share/{token}", json={}), 401)["detail"] == "password_required"
    assert ok(anon.post(f"/api/share/{token}", json={"password": "nope"}), 401)["detail"] == "wrong_password"
    assert ok(anon.post(f"/api/share/{token}", json={"password": "s3cret"}))["title"] == "Koti"
    ok(a.put(f"/api/tabs/{tab1}/share", json={"title": "kept"}))
    ok(anon.post(f"/api/share/{token}", json={}), 401)
    ok(a.put(f"/api/tabs/{tab1}/share", json={"password": ""}))
    ok(anon.post(f"/api/share/{token}", json={}))
    ok(b.delete(f"/api/tabs/{tab1}/share"))
    ok(anon.post(f"/api/share/{token}", json={}))
    ok(a.delete(f"/api/tabs/{tab1}/share"))
    ok(anon.post(f"/api/share/{token}", json={}), 404)

    # --- export / import round trip ---
    ok(a.patch(f"/api/bookmarks/{b2}", json={"notes": "line <b>1</b>\nline 2 & more"}))
    exported = ok(a.get("/api/export")).text
    before = sorted((i["url"], i["title"], tuple(i["tags"]), i["notes"], i["tab_name"], i["category_name"])
                    for i in ok(a.get("/api/bookmarks"))["items"])
    parsed = importer.parse(exported)
    assert len(parsed) == len(before) == 5
    stats = ok(b.post("/api/import", params={"mode": "structure"}, content=exported.encode()))
    assert stats == {"found": 5, "imported": 5, "skipped": 0, "duplicates": 0}, stats
    after = sorted((i["url"], i["title"], tuple(i["tags"]), i["notes"], i["tab_name"], i["category_name"])
                   for i in ok(b.get("/api/bookmarks"))["items"])
    assert after == before, f"\n{before}\n{after}"
    assert ok(b.post("/api/import", params={"mode": "structure"}, content=exported.encode()))["duplicates"] == 5

    browser_file = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<TITLE>Bookmarks</TITLE><H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>
    <DL><p>
        <DT><A HREF="https://a.example/" ADD_DATE="1600000000" ICON="data:image/png;base64,AAAA">A &amp; co</A>
        <DT><H3>Dev Tools</H3>
        <DL><p>
            <DT><A HREF="https://b.example/x?y=1&amp;z=2" ADD_DATE="1600000001000" TAGS="one,Two">B</A>
            <DD>a note
            <DT><A HREF="javascript:void(0)">bookmarklet</A>
            <DT><A HREF="https://dupe.example/" NOTES="attribute note" TAGS="x, y">Notes attribute</A>
            <DT><A HREF="place:sort=8">Most visited</A>
        </DL><p>
    </DL><p>
    <DT><A HREF="https://c.example/">Loose</A>
</DL><p>
"""
    stats = ok(b.post("/api/import", params={"mode": "catalog"}, content=browser_file.encode()))
    assert stats == {"found": 6, "imported": 4, "skipped": 2, "duplicates": 0}, stats
    got = {i["url"]: i for i in ok(b.get("/api/bookmarks", params={"scope": "catalog", "q": ".example/"}))["items"]}
    bx = got["https://b.example/x?y=1&z=2"]
    assert bx["tags"] == ["bookmarks bar", "dev tools", "one", "two"] and bx["notes"] == "a note"
    assert got["https://dupe.example/"]["notes"] == "attribute note" and got["https://dupe.example/"]["tags"][-2:] == ["x", "y"]
    assert bx["created_at"] == 1600000001 and got["https://a.example/"]["title"] == "A & co"
    stats = ok(b.post("/api/import", params={"mode": "tab", "tab_name": "Imp", "skip_duplicates": "false"},
                      content=browser_file.encode()))
    names = {c["name"] for c in ok(b.get("/api/dashboard"))["categories"]}
    assert stats["imported"] == 4 and {"Bookmarks bar", "Bookmarks bar / Dev Tools", "Imp"} <= names

    # --- deleting containers ---
    ok(a.delete(f"/api/categories/{cat3}", params={"bookmarks": "catalog"}))
    assert search("example 2", scope="catalog") == {b1}
    ok(a.delete(f"/api/tabs/{tab1}"))
    assert search("hacker") == set(), "bookmarks are deleted with their tab unless moved to the Catalog"

    # --- two-factor authentication and passwords ---
    setup = ok(a.post("/api/account/totp/setup"))
    assert setup["qr"].startswith("data:image/svg+xml")
    ok(a.post("/api/account/totp/enable", json={"code": "000000"}), 422)
    code_now = security._totp_at(setup["secret"], int(time.time()) // 30)
    ok(a.post("/api/account/totp/enable", json={"code": code_now}))
    fresh = client()
    assert ok(fresh.post("/api/login", json={"username": "alice", "password": "correct horse"}), 401)["detail"] == "totp_required"
    assert ok(fresh.post("/api/login", json={"username": "alice", "password": "correct horse", "totp": code_now}), 401)[
        "detail"] == "bad_totp", "a code cannot be used twice"
    code_next = security._totp_at(setup["secret"], int(time.time()) // 30 + 1)
    ok(fresh.post("/api/login", json={"username": "ALICE", "password": "correct horse", "totp": code_next}))
    ok(fresh.get("/api/me"))
    ok(a.post("/api/account/password", json={"current": "nope", "new": "new password 1"}), 403)
    ok(a.post("/api/account/password", json={"current": "correct horse", "new": "new password 1"}))
    ok(fresh.get("/api/me"), 401)  # other sessions are signed out
    ok(a.post("/api/account/totp/disable", json={"password": "new password 1"}))

    # --- admin ---
    users = ok(a.get("/api/admin"))["users"]
    bob = next(u for u in users if u["username"] == "bob")
    assert bob["bookmarks"] == 13
    temp = ok(a.post(f"/api/admin/users/{bob['id']}/reset-password"))["password"]
    ok(b.get("/api/me"), 401)
    ok(b.post("/api/login", json={"username": "bob", "password": temp}))
    ok(a.post("/api/account/delete", json={"password": "new password 1"}), 409)  # last admin with other users
    ok(a.put("/api/admin/config", json={"registration": "open"}))
    assert ok(a.get("/api/admin"))["registration_fixed"] is False
    # STASH_REGISTRATION overrides the stored setting and locks it
    main_module.REGISTRATION_ENV = "closed"
    try:
        assert ok(anon.get("/api/public"))["registration"] == "closed"
        assert ok(a.get("/api/admin"))["registration_fixed"] is True
        ok(a.put("/api/admin/config", json={"registration": "open"}), 409)
        ok(anon.post("/api/register", json={"username": "carol", "password": "carol password"}), 403)
        main_module.REGISTRATION_ENV = "open"
        assert ok(anon.get("/api/public"))["registration"] == "open"
    finally:
        main_module.REGISTRATION_ENV = None
    assert ok(anon.get("/api/public"))["registration"] == "open"  # the stored setting is still there
    ok(anon.post("/api/register", json={"username": "carol", "password": "carol password", "lang": "sv"}))
    carol_dash = ok(anon.get("/api/dashboard"))
    assert carol_dash["tabs"][0]["name"] == "Hem" and carol_dash["categories"][0]["name"] == "Favoriter"
    ok(anon.patch("/api/settings", json={"lang": "sv"}))
    ok(anon.patch("/api/settings", json={"lang": "de"}), 422)
    ok(a.delete(f"/api/admin/users/{bob['id']}"))
    ok(b.get("/api/me"), 401)
    ok(anon.post("/api/logout"))
    ok(anon.get("/api/me"), 401)

    # --- pages and headers ---
    for path in ("/", "/add", "/share/abc", "/static/js/app.js", "/static/css/app.css"):
        r = anon.get(path)
        assert r.status_code == 200 and anon.head(path).status_code == 200, path
        assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    z = anon.get("/extension.zip")
    assert z.status_code == 200 and z.content[:2] == b"PK"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert {f"stash-extension/_locales/{lang}/messages.json" for lang in ("en", "fi", "sv")} <= set(names), names
    ok(anon.get("/favicon/localhost"), 404)
    ok(anon.get("/favicon/..%2f..%2fetc"), 404)

print("all smoke tests passed")
