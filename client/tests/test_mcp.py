import asyncio

from conftest import ids_by_title

from stashai.config import Config
from stashai.mcp_server import build_server
from stashai.session import Session


def text(result) -> str:
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(c.text for c in content)


def test_tools_over_mcp(api, tmp_path):
    rules = tmp_path / "rules.md"
    rules.write_text("# example\n- Recipes always have the tag food.\n", encoding="utf-8")
    session = Session(api)
    server = build_server(session, Config(rules_path=rules))
    assert "THE USER'S STANDING RULES" in server.instructions and "tag food" in server.instructions
    assert "# example" not in server.instructions

    async def scenario():
        names = {t.name for t in await server.list_tools()}
        assert {"overview", "search", "show", "preview_changes", "apply_changes", "undo", "check_links",
                "refresh_titles", "refresh_icons", "browsing_history"} <= names
        assert "7 bookmarks" in text(await server.call_tool("overview", {}))
        found = text(await server.call_tool("search", {"text": ["buns"], "label": "buns"}))
        assert found.startswith("S1 (buns): 1 bookmark")
        preview = text(await server.call_tool("preview_changes", {
            "ops": [{"op": "add_tags", "set": "S1", "tags": ["baking"]}], "summary": "Tag the buns"}))
        assert "Preview P1: Tag the buns" in preview and "Nothing has changed yet" in preview
        applied = text(await server.call_tool("apply_changes", {"preview_id": "P1"}))
        assert applied.startswith("Applied as change #")
        t = ids_by_title(session.store)
        assert "baking" in session.store.bookmarks[t["Bun recipe"]].tags
        # a mistake comes back as a tool error the model can read, not as a crash
        try:
            await server.call_tool("show", {"set_name": "S42"})
        except Exception as e:  # noqa: BLE001
            assert "S42" in str(e)
        else:
            raise AssertionError("an unknown set must be an error")
        assert "Undid #" in text(await server.call_tool("undo", {}))

    asyncio.run(scenario())
