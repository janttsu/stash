import threading

from conftest import FakeLLM, ids_by_title

from stashai.agent import Agent
from stashai.llm import Cancelled
from stashai.render import plan_text, set_text
from stashai.store import Store


def make(api, script, judge=None, rules=""):
    agent = Agent(api=api, llm=FakeLLM(script, judge), store=Store(), rules=lambda: rules, judge_batch=3)
    agent.refresh()
    return agent


def step(tool, **args):
    return {"thought": f"calling {tool}", "tool": tool, "args": args}


def test_list_then_delete_those(api):
    agent = make(api, [
        step("search", label="acme", text=["acme"]),
        step("show", set="S1"),
        step("search", label="acme, not the support article", in_set="S1", tags_any=["acme", "uutiset"]),
        step("answer", message="Acmeen liittyy 3 kirjanmerkkiä.", show="S2"),
        # second request: "delete those"
        step("propose", summary="Poista Acmeen liittyvät", ops=[{"op": "delete", "set": "S2"}]),
    ])
    events = []
    out = agent.ask("listaa kaikki mikä liittyy acmeen", emit=lambda k, t: events.append((k, t)))
    assert out.kind == "answer" and out.show == "S2" and "3 kirjanmerkkiä" in out.message
    assert len(set_text(agent.store, "S2")) == 2 + 3
    assert ("thought", "calling search") in events and any(k == "result" and t.startswith("S1") for k, t in events)
    # the model saw the overview, the rules and the request; the show result has full lines
    first = agent.llm.calls[0]
    assert "OVERVIEW OF THE BOOKMARKS" in first[0]["content"] and "acme 2" in first[0]["content"]
    assert "REQUEST FROM THE USER:\nlistaa kaikki" in first[1]["content"]
    assert "#1 ACME documentation" in agent.llm.calls[2][-1]["content"] or "ACME documentation |" in agent.llm.calls[2][-1]["content"]

    out = agent.ask("poista ne")
    conversation = agent.llm.calls[-1][1]["content"]
    assert "showed S2: 3 bookmarks" in conversation and "S2: 3 bookmarks" in conversation
    assert out.kind == "plan" and out.plan.preview["counts"]["deleted"] == 3
    assert len(api.snapshot()["bookmarks"]) == 7, "nothing changes before the user accepts"
    text = "\n".join(plan_text(out.plan.summary, out.plan.preview))
    assert "3 deleted" in text and text.count("DELETE") == 3

    result = agent.apply()
    assert result["changeset"] and len(agent.store.bookmarks) == 4
    assert "accepted, applied as change" in agent.conversation()
    target, undo = agent.undo()
    assert target["id"] == result["changeset"] and undo["restored"] == 3
    assert len(agent.store.bookmarks) == 7


def test_move_topic_to_new_tag_with_judge(api):
    def judge(question, ids):
        assert question == "Is this about the company Acme?"
        return [i for i in ids if i in acme], []

    agent = make(api, [
        step("search", label="acme candidates", text=["acme"]),
        step("judge", set="S1", question="Is this about the company Acme?", label="acme"),
        step("propose", summary="Acme → yritys-x", ops=[
            {"op": "add_tags", "set": "S2", "tags": "yritys-x"},
            {"op": "remove_tags", "set": "S2", "tags": ["acme"]},
        ]),
    ], judge=judge)
    t = ids_by_title(agent.store)
    acme = {t["ACME documentation"], t["ACME blog"], t["Acme buys Widgets Inc"]}
    out = agent.ask("siirrä kaikki acme-yhtiöön liittyvät tagille yritys-x")
    assert out.kind == "plan"
    assert set(agent.store.resolve("S2")) == acme
    # 4 candidates in batches of 3 = 2 model calls
    assert sum(1 for c in agent.llm.calls if c[0]["content"].startswith("You check")) == 2
    diff = {e["id"]: e for e in out.plan.preview["diff"]}
    assert diff[t["ACME documentation"]]["tags"] == [["acme", "docs"], ["docs", "yritys-x"]]
    assert diff[t["Acme buys Widgets Inc"]]["tags"] == [["uutiset"], ["uutiset", "yritys-x"]]
    agent.discard()
    assert agent.pending is None and "rejected by the user" in agent.conversation()
    assert ["acme", 2] in api.snapshot()["tags"]


def test_mistakes_are_sent_back_to_the_model(api):
    agent = make(api, [
        "this is not json",
        step("frobnicate"),
        step("show", set="S7"),
        step("propose", summary="x", ops=[{"op": "delete"}]),
        step("propose", summary="x", ops=[{"op": "move", "ids": [1]}]),
        step("search", label="pulla", query="pulla", colour="red"),
        step("search", label="pulla", query="pulla"),
        step("propose", summary="already so", ops=[{"op": "add_tags", "set": "S1", "tags": ["ruoka"]}]),
        step("answer", message="Ei muutettavaa."),
    ])
    out = agent.ask("tee jotain")
    assert out.kind == "answer" and out.message == "Ei muutettavaa."
    results = [c[-1]["content"] for c in agent.llm.calls[1:]]
    assert "invalid reply" in results[0]
    assert "unknown tool 'frobnicate'" in results[1]
    assert "no set named 'S7'" in results[2]
    assert "delete needs a set" in results[3]
    assert "Stash refused the proposal: target_required" in results[4]
    assert "unknown search fields: colour (use text" in results[5]
    assert results[6].startswith("RESULT of search:\nS1 (pulla): 1 bookmarks")
    assert "changes nothing" in results[7]


def test_repeated_calls_and_step_limit(api):
    agent = make(api, [step("show", set="ALL", limit=2)] * 30)
    agent.max_steps = 5
    out = agent.ask("loop")
    assert out.kind == "error" and "5 steps" in out.message
    assert "already made this exact call" in agent.llm.calls[-1][-1]["content"]


def test_cancel(api):
    agent = make(api, [step("search", text=["acme"])])
    cancel = threading.Event()
    cancel.set()
    out = agent.ask("anything", cancel=cancel)
    assert out.kind == "cancelled"
    assert isinstance(Cancelled(), Exception)


def test_create_and_move_to_new_category(api):
    agent = make(api, [
        step("search", label="pulla", text=["pulla"]),
        step("propose", summary="Reseptit työpöydälle", ops=[
            {"op": "move", "set": "S1", "tab": "Koti", "category": "Reseptit"},
            {"op": "create", "url": "https://recipes.example/korvapuusti", "title": "Korvapuusti", "tags": ["ruoka"],
             "tab": "Koti", "category": "Reseptit"},
            {"op": "create", "url": "https://recipes.example/pulla", "title": "dupe"},
        ]),
    ])
    out = agent.ask("vie reseptit työpöydälle")
    p = out.plan.preview
    assert p["counts"] == {"created": 1, "deleted": 0, "updated": 1, "unchanged": 0}
    assert p["skipped"][0]["reason"] == "exists"
    text = "\n".join(plan_text(out.plan.summary, p))
    assert "Catalog → Koti / Reseptit" in text and "ADD     Korvapuusti" in text and "already bookmarked" in text
    agent.apply()
    b = {x.title: x for x in agent.store.bookmarks.values()}
    assert b["Korvapuusti"].where == "Koti / Reseptit" and b["Pulla recipe"].where == "Koti / Reseptit"


def test_rules_and_pending_plan_reach_the_model(api):
    agent = make(api, [
        step("search", text=["pulla"]),
        step("propose", summary="poista pulla", ops=[{"op": "delete", "set": "S1"}]),
        step("answer", message="ok"),
    ], rules="- Never delete recipes.")
    agent.ask("poista pulla")
    agent.ask("älä sittenkään")
    system, user = agent.llm.calls[-1][0]["content"], agent.llm.calls[-1][1]["content"]
    assert "USER RULES:\n- Never delete recipes." in system
    assert "A PROPOSAL IS WAITING" in user and "poista pulla: 0 changed, 1 deleted" in user


def test_context_is_kept_small(api):
    agent = make(api, [])
    agent.context = 3000
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    messages += [{"role": "user", "content": "RESULT of show:\n" + "x" * 3000} for _ in range(5)]
    messages += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": "RESULT of show:\n" + "y" * 3000}]
    agent.fit(messages)
    assert messages[2]["content"].endswith("[shortened: an older result]")
    assert messages[-1]["content"].endswith("y"), "the newest result stays whole"
