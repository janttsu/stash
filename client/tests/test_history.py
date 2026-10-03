import time

from conftest import FakeLLM, ids_by_title

from stashai.agent import Agent
from stashai.render import plan_text
from stashai.store import Store


def step(tool, **args):
    return {"thought": tool, "tool": tool, "args": args}


def send_history(api, items):
    now = int(time.time())
    r = api.http.post("/history/macbook", json={"reset": True, "done": True, "browser": "firefox", "items": [
        {"url": u, "title": t, "visits": n, "visits_30d": n, "visits_90d": n, "visits_365d": n, "last_visit": now}
        for u, t, n in items]})
    assert r.status_code == 200, r.text


def make(api, script):
    agent = Agent(api=api, llm=FakeLLM(script), store=Store())
    agent.refresh()
    return agent


def test_without_history_nothing_changes(api):
    agent = make(api, [step("history", text="x"), step("answer", message="ok")])
    assert "visits" not in agent.store.line(next(iter(agent.store.bookmarks)))
    assert "BROWSING HISTORY" not in agent.store.overview()
    agent.ask("mitä käytän eniten?")
    assert "no browsing history in Stash yet" in agent.llm.calls[1][-1]["content"]


def test_usage_on_lines_search_and_history_tool(api):
    send_history(api, [("https://acme.example/docs", "ACME docs", 40), ("http://recipes.example/pulla/", "Pulla", 3),
                       ("https://often.example/", "Often used, no bookmark", 25)])
    agent = make(api, [
        step("history", bookmarked="no", min_visits=5),
        step("search", used_min=2, sort="use"),
        step("answer", message="ok"),
    ])
    t = ids_by_title(agent.store)
    assert "visits 30d 40, 90d 40, all 40" in agent.store.line(t["ACME documentation"])
    assert "visits: none" in agent.store.line(t["ACME blog"])
    assert "BROWSING HISTORY from macbook" in agent.store.overview() and "2 bookmarks visited" in agent.store.overview()
    assert agent.store.search(unused_days=30) and t["ACME documentation"] not in agent.store.search(unused_days=30)
    agent.ask("mitä käytän usein mutta en ole tallentanut?")
    hist = agent.llm.calls[1][-1]["content"]
    assert "https://often.example/ | Often used, no bookmark | 30d 25" in hist and "NOT bookmarked" in hist
    assert "acme.example" not in hist
    found = agent.llm.calls[2][-1]["content"]
    assert found.index("ACME documentation") < found.index("Pulla recipe"), "most used first"


def test_sort_by_use_orders_bookmarks_and_categories(api):
    api.changes([
        {"op": "create", "url": "https://a.example/", "title": "Rarely", "tab": "Työ", "category": "Asiakkaat"},
        {"op": "create", "url": "https://b.example/", "title": "Daily", "tab": "Työ", "category": "Asiakkaat"},
        {"op": "create", "url": "https://c.example/", "title": "Tools", "tab": "Työ", "category": "Työkalut"},
    ], "setup", dry_run=False)
    send_history(api, [("https://b.example/", "", 50), ("https://a.example/", "", 1), ("https://c.example/", "", 200)])
    agent = make(api, [step("propose", summary="Käytetyimmät ylös", ops=[{"op": "sort_by_use", "tab": "työ"}])])
    out = agent.ask("järjestä työ-välilehti käytön mukaan")
    p = out.plan.preview
    pos = {e["title"]: e["position"] for e in p["diff"]}
    assert pos == {"Daily": [3, 1], "ACME blog": [1, 3]}  # Rarely (1 visit) stays second
    text = "\n".join(plan_text(out.plan.summary, p))
    assert "place 3 → 1" in text
    if p["categories"]:  # both categories in one column: Työkalut (200 visits) goes first
        assert "ORDER   Työ / Työkalut" in text
    agent.apply()
    agent.undo()
    assert {b.title: b.position for b in agent.store.bookmarks.values() if b.category == "Asiakkaat"} == \
        {"ACME blog": 0, "Rarely": 1, "Daily": 2}
