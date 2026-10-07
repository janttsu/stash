"""The /mcp endpoint end to end: JSON-RPC over HTTP with an API key, as Claude Code or Qwen Code send it."""
import json

from conftest import ids_by_title

from app.mcpserver.store import Store

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class Client:
    def __init__(self, http, key):
        self.http, self.headers, self.n = http, {**ACCEPT, "Authorization": f"Bearer {key}"}, 0

    def rpc(self, method, params=None, headers=None):
        self.n += 1
        return self.http.post("/mcp", json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}},
                              headers=headers or self.headers)

    def call(self, name, **arguments):
        r = self.rpc("tools/call", {"name": name, "arguments": arguments})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        return result.get("isError", False), "\n".join(c["text"] for c in result["content"])


def new_key(stash, can_write=True):
    return stash["web"].post("/api/keys", json={"name": "assistant", "can_write": can_write}).json()


def test_keys_guard_the_endpoint(api, stash):
    raw = stash["raw"]
    r = raw.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=ACCEPT)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    bad = Client(raw, "stash_not-a-real-key")
    assert bad.rpc("tools/list").status_code == 401
    good = Client(raw, stash["key"])
    assert good.rpc("tools/list", headers={**good.headers, "Host": "evil.example"}).status_code == 421


def test_session_through_http(api, stash):
    web, raw = stash["web"], stash["raw"]
    assert web.patch("/api/settings", json={"assistant_rules": "Recipes always have the tag food."}).status_code == 200
    mcp = Client(raw, stash["key"])
    init = mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
    assert init["serverInfo"]["name"] == "stash" and "preview_changes" in init["instructions"]
    names = {t["name"] for t in mcp.rpc("tools/list").json()["result"]["tools"]}
    assert {"overview", "search", "show", "preview_changes", "apply_changes", "undo", "check_links",
            "refresh_titles", "refresh_icons", "browsing_history"} <= names

    err, text = mcp.call("overview")
    assert not err and "7 bookmarks" in text and "Recipes always have the tag food." in text, "rules reach the model"
    err, text = mcp.call("search", text=["buns"], label="buns")
    assert text.startswith("S1 (buns): 1 bookmark")
    err, text = mcp.call("preview_changes", ops=[{"op": "add_tags", "set": "S1", "tags": ["baking"]}], summary="Tag")
    assert "Preview P1: Tag" in text and "Nothing has changed yet" in text
    store = Store(api.snapshot())
    assert "baking" not in store.bookmarks[ids_by_title(store)["Bun recipe"]].tags, "a preview changes nothing"
    err, text = mcp.call("apply_changes", preview_id="P1")
    assert not err and text.startswith("Applied as change #")
    store = Store(api.snapshot())
    assert "baking" in store.bookmarks[ids_by_title(store)["Bun recipe"]].tags
    err, text = mcp.call("show", set_name="S42")
    assert err and "S42" in text, "a mistake comes back as a tool error the model can read"
    err, text = mcp.call("undo")
    assert not err and text.startswith("Undid #")


def test_read_only_and_revoked_keys(api, stash):
    web, raw = stash["web"], stash["raw"]
    made = new_key(stash, can_write=False)
    mcp = Client(raw, made["key"])
    mcp.call("search", text=["buns"])
    err, text = mcp.call("preview_changes", ops=[{"op": "delete", "set": "S1"}], summary="Delete")
    assert not err and "Preview P1" in text, "previews work with a read-only key"
    err, text = mcp.call("apply_changes", preview_id="P1")
    assert err and "read_only_key" in text
    assert web.patch(f"/api/keys/{made['id']}", json={"can_write": True}).status_code == 200
    err, text = mcp.call("apply_changes", preview_id="P1")
    assert not err and "deleted" in text, "allowing changes in Settings works at once"
    assert web.delete(f"/api/keys/{made['id']}").status_code == 200
    assert mcp.rpc("tools/list").status_code == 401, "a revoked key stops working at once"
    other = web.patch("/api/keys/999999", json={"can_write": True})
    assert other.status_code == 404


def test_list_urls_and_delete_by_ids(api, stash):
    """What stash-linkcheck.py does: whole addresses of a set, then a delete by ids through a preview."""
    mcp = Client(stash["raw"], stash["key"])
    mcp.call("search", text=["buns"])
    err, text = mcp.call("list_urls", set_name="S1")
    data = json.loads(text)
    assert not err and data["total"] == 1 and data["items"][0]["url"].startswith("http")
    assert json.loads(mcp.call("list_urls", set_name="ALL", limit=2)[1])["total"] == 7
    bid = data["items"][0]["id"]
    err, text = mcp.call("preview_changes", ops=[{"op": "delete", "ids": [bid]}], summary="Gone")
    assert not err and "1 deleted" in text
    err, text = mcp.call("apply_changes", preview_id=text.split()[1].rstrip(":"))
    assert not err and bid not in Store(api.snapshot()).bookmarks
