import asyncio

from conftest import FakeLLM
from textual.widgets import RichLog

from stashai import cli, tui
from stashai.agent import Agent
from stashai.config import Config
from stashai.store import Store


def text_of(log: RichLog) -> str:
    return "\n".join(line.text for line in log.lines)


def test_ask_preview_apply_and_undo(api, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    def step(tool, **args):
        return {"thought": f"calling {tool}", "tool": tool, "args": args}

    llm = FakeLLM([
        step("search", label="buns", text=["buns"]),
        step("propose", summary="Delete the bun recipe", ops=[{"op": "delete", "set": "S1"}]),
        step("search", label="acme", text=["acme"]),
        step("answer", message="Four matches.", show="S2"),
    ])
    logs = []

    class Log(list):
        path = tmp_path / "session.log"

        def __call__(self, text):
            logs.append(text)

        def close(self):
            pass

    log = Log()
    monkeypatch.setattr(cli, "newer_version", lambda url: ("9.9.9", "https://stash.test/dl/x.whl"))
    monkeypatch.setattr(cli, "build", lambda cfg: (Agent(api=api, llm=llm, store=Store(), log=log), log))
    cfg = Config(stash_url="https://stash.test", api_key="k", model="fake", model_chosen=True,
                 rules_path=tmp_path / "rules.md")

    async def scenario():
        app = tui.StashAI(cfg)
        async with app.run_test(size=(160, 40)) as pilot:
            for _ in range(50):
                await pilot.pause(0.05)
                if app.ready and not app.busy:
                    break
            log_w, view = app.query_one("#log", RichLog), app.query_one("#view", RichLog)
            assert "7 bookmarks" in text_of(log_w)
            for _ in range(20):
                await pilot.pause(0.05)
            assert "stashai 9.9.9 is available" in text_of(log_w)

            async def send(text):
                app.query_one("#prompt").value = text
                await pilot.press("enter")
                for _ in range(100):
                    await pilot.pause(0.05)
                    if not app.busy:
                        break

            await send("delete the bun recipe")
            assert "Proposal: Delete the bun recipe" in text_of(log_w)
            assert "DELETE  #" in text_of(view) and "Bun recipe" in text_of(view)
            assert len(app.agent.store.bookmarks) == 7
            status = str(app.query_one("#status").render())
            assert "context " in status and "k/32k (" in status, status
            await send("y")
            assert "Done: change #" in text_of(log_w) and len(app.agent.store.bookmarks) == 6
            await send("/undo")
            assert "Undid #" in text_of(log_w) and len(app.agent.store.bookmarks) == 7
            await send("list acme")
            assert "Four matches." in text_of(log_w) and "S2: 4 bookmarks" in text_of(view)
            await send("/history")
            assert "Delete the bun recipe" in text_of(view) and "undone" in text_of(view)
            await send("/nonsense")
            assert "Unknown command" in text_of(log_w)
            await pilot.press("f1")
            assert "Commands:" in text_of(view)
            await pilot.press("f2")
            for _ in range(40):
                await pilot.pause(0.05)
                if isinstance(app.screen, tui.ModelPicker):
                    break
            assert isinstance(app.screen, tui.ModelPicker), "F2 changes the model mid-session"
            await pilot.pause(0.2)
            await pilot.press("escape")
            for _ in range(40):
                await pilot.pause(0.05)
                if not app.busy and not isinstance(app.screen, tui.ModelPicker):
                    break
            assert "Model fake ready" in text_of(log_w)

    asyncio.run(scenario())
    assert any("APPLIED as change" in x for x in logs)


def test_asks_for_the_model_when_none_was_given(api, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    llm = FakeLLM([])

    class Log:
        path = tmp_path / "session.log"

        def __call__(self, text):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "newer_version", lambda url: None)
    monkeypatch.setattr(cli, "build", lambda cfg: (Agent(api=api, llm=llm, store=Store(), log=Log()), Log()))
    cfg = Config(stash_url="https://stash.test", api_key="k", model="qwen3.6:35b-a3b", rules_path=tmp_path / "r.md")

    async def scenario():
        app = tui.StashAI(cfg)
        async with app.run_test(size=(160, 40)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if isinstance(app.screen, tui.ModelPicker):
                    break
            assert isinstance(app.screen, tui.ModelPicker), "the model list is shown first"
            await pilot.pause(0.2)
            options = app.screen.option_list
            labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
            assert len(labels) == 3 and "23.9 GB" in labels[1] and labels[1].endswith("(default)")
            assert options.highlighted == 1, "the configured model is offered first"
            await pilot.press("down", "enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if app.ready:
                    break
            assert app.ready and llm.model == "small:9b" and cfg.model == "small:9b"
            assert "Model small:9b ready" in text_of(app.query_one("#log", RichLog))
        # the next start offers the model chosen now
        from stashai.config import last_model
        assert last_model() == "small:9b"

    asyncio.run(scenario())
