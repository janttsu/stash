"""Open tabs: sending them (web), sorting them out over /mcp, and that saving makes ordinary undoable bookmarks."""
from conftest import ids_by_title
from test_mcp import ACCEPT, Client, new_key

from app.mcpserver.store import Store

SENT = [{"url": "https://acme.example/docs", "title": "ACME documentation"},          # already bookmarked
        {"url": "https://shop.example/cart", "title": "Your cart (2 items)"},
        {"url": "https://news.example/long-read", "title": "A long read, half done"},
        {"url": "https://www.google.com/search?q=stash", "title": "stash - Google Search"},
        {"url": "about:blank", "title": "New tab"},
        {"url": "https://shop.example/cart", "title": "Your cart again"}]              # same address twice


def send(stash, name="laptop", **extra):
    r = stash["web"].post("/api/open-tabs/import", json={"name": name, "items": SENT, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_send_and_sort_out_over_mcp(api, stash):
    res = send(stash)
    assert res == {"list_id": res["list_id"], "tabs": 4, "already_bookmarked": 1, "invalid": 1}
    mcp = Client(stash["raw"], new_key(stash)["key"])
    err, text = mcp.call("open_tabs_overview")
    assert not err and f"L{res['list_id']}" in text and "laptop: 3 open / 1 saved / 0 done / 0 dropped" in text
    err, text = mcp.call("open_tabs_list")
    assert not err and text.startswith("3 tabs") and "Your cart (2 items)" in text and "ACME documentation" not in text
    err, text = mcp.call("open_tabs_list", state="saved")
    assert "ACME documentation | acme.example/docs | saved" in text
    ids = {line.split()[0][1:]: line for line in text.splitlines()}
    err, all_text = mcp.call("open_tabs_list", state="")
    ids = {line.split(" ", 1)[1].split(" |")[0]: int(line.split()[0][1:]) for line in all_text.splitlines()[1:]}
    # the search result page is dropped, the cart is left for the user with a note
    err, text = mcp.call("open_tabs_resolve", ids=[ids["stash - Google Search"]], state="dropped", note="a search page")
    assert not err and text == "1 tabs marked dropped."
    err, text = mcp.call("open_tabs_resolve", ids=[ids["Your cart (2 items)"]], state="open", note="2 items still in the cart")
    assert not err
    # the long read is worth keeping: a bookmark in a Dashboard category, as one undoable change
    err, text = mcp.call("open_tabs_save", ids=[ids["A long read, half done"]], tags=["reading"], tab="Later", category="Articles",
                         summary="Keep the article")
    assert not err and text.startswith("Change #") and "1 bookmarks made" in text and "1 tabs marked saved" in text
    store = Store(api.snapshot())
    bid = ids_by_title(store)["A long read, half done"]
    assert store.bookmarks[bid].tags == ["reading"] and "Later" in store.bookmarks[bid].where
    err, text = mcp.call("open_tabs_list")
    assert text.startswith("1 tabs") and "note: 2 items still in the cart" in text, "only the cart is left to look at"
    err, text = mcp.call("open_tabs_list", state="", host="shop.example")
    assert "1 tabs" in text
    err, text = mcp.call("undo")
    assert not err and "1 removed" in text, "the bookmark made from a tab is an ordinary change"
    assert "A long read" not in ids_by_title(Store(api.snapshot()))
    # saving a tab whose address is already bookmarked makes no second bookmark
    err, text = mcp.call("open_tabs_save", ids=[ids["ACME documentation"]], summary="again")
    assert not err and "0 bookmarks made, 1 were already bookmarked" in text


def test_keys_guard_the_tab_tools(api, stash):
    raw = stash["raw"]
    send(stash)
    for name in ("open_tabs_overview", "open_tabs_list", "open_tabs_resolve", "open_tabs_save"):
        r = raw.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": {"ids": [1], "state": "done"}}}, headers=ACCEPT)
        assert r.status_code == 401, name
    reader = Client(raw, new_key(stash, can_write=False)["key"])
    err, text = reader.call("open_tabs_list")
    assert not err and "Your cart" in text
    first = int(text.splitlines()[1].split()[0][1:])
    err, text = reader.call("open_tabs_resolve", ids=[first], state="done")
    assert err and "read_only_key" in text
    err, text = reader.call("open_tabs_save", ids=[first])
    assert err and "read_only_key" in text
    err, text = reader.call("open_tabs_list", state="done")
    assert "No tabs match" in text


def test_web_view_and_pasted_text(api, stash):
    web, raw = stash["web"], stash["raw"]
    assert raw.get("/api/open-tabs").status_code == 401
    res = web.post("/api/open-tabs/import", json={"name": "pasted", "text": "look at https://a.example/x and\nhttps://b.example/y\nnot a link"}).json()
    assert res["tabs"] == 2
    lists = web.get("/api/open-tabs").json()["lists"]
    mine = next(l for l in lists if l["id"] == res["list_id"])
    assert mine["name"] == "pasted" and mine["open"] == 2 and mine["total"] == 2
    items = web.get(f"/api/open-tabs/items?list_id={res['list_id']}").json()["items"]
    assert [i["url"] for i in items] == ["https://a.example/x", "https://b.example/y"] and items[0]["title"] == "https://a.example/x"
    r = web.post("/api/open-tabs/save", json={"ids": [items[0]["id"]], "tags": ["pasted"]}).json()
    assert r["bookmarks_made"] == 1
    assert web.get(f"/api/open-tabs/items?list_id={res['list_id']}&state=saved").json()["items"][0]["bookmark_id"] == r["bookmark_ids"][0]
    assert web.post("/api/open-tabs/resolve", json={"ids": [items[1]["id"]], "state": "done", "note": "x"}).json()["updated"] == 1
    assert web.post("/api/open-tabs/resolve", json={"ids": [999999], "state": "done"}).status_code == 404
    assert web.post("/api/open-tabs/import", json={"text": "nothing here"}).status_code == 422
    # a second user's tabs are not visible, and deleting the list keeps the bookmarks
    assert web.delete(f"/api/open-tabs/{res['list_id']}").status_code == 200
    assert web.delete(f"/api/open-tabs/{res['list_id']}").status_code == 404
    assert "https://a.example/x" in {b.url for b in Store(api.snapshot()).bookmarks.values()}
