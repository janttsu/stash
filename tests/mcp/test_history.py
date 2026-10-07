import time

from conftest import ids_by_title

from app.mcpserver.session import Session


def send_history(api, items):
    now = int(time.time())
    r = api.http.post("/history/macbook", json={"reset": True, "done": True, "browser": "firefox", "items": [
        {"url": u, "title": t, "visits": n, "visits_30d": n, "visits_90d": n, "visits_365d": n, "last_visit": now}
        for u, t, n in items]})
    assert r.status_code == 200, r.text


def make(api):
    s = Session(api)
    s.ensure(force=True)
    return s


def test_without_history_nothing_changes(api):
    s = make(api)
    assert "visits" not in s.store.line(next(iter(s.store.bookmarks)))
    assert "BROWSING HISTORY" not in s.store.overview()
    assert "No browsing history in Stash yet" in s.browsing_history(text="x")
    assert "Browsing history: none sent yet" in s.overview()


def test_usage_on_lines_search_and_history_tool(api):
    send_history(api, [("https://acme.example/docs", "ACME docs", 40), ("http://recipes.example/buns/", "Pulla", 3),
                       ("https://often.example/", "Often used, no bookmark", 25)])
    s = make(api)
    t = ids_by_title(s.store)
    assert "visits 30d 40, 90d 40, all 40" in s.store.line(t["ACME documentation"])
    assert "visits: none" in s.store.line(t["ACME blog"])
    assert "BROWSING HISTORY from macbook" in s.store.overview() and "2 bookmarks visited" in s.store.overview()
    assert s.store.search(unused_days=30) and t["ACME documentation"] not in s.store.search(unused_days=30)
    hist = s.browsing_history(bookmarked="no", min_visits=5)
    assert "https://often.example/ | Often used, no bookmark | 30d 25" in hist and "NOT bookmarked" in hist
    assert "acme.example" not in hist
    assert "Stash holds 3 visited addresses" in hist and "min_visits=5, period=90d, bookmarked=no: 1 match" in hist
    found = s.search(used_min=2, sort="use")
    assert found.index("ACME documentation") < found.index("Bun recipe"), "most used first"


def test_sort_by_use_orders_bookmarks_and_categories(api):
    api.changes([
        {"op": "create", "url": "https://a.example/", "title": "Rarely", "tab": "Work", "category": "Clients"},
        {"op": "create", "url": "https://b.example/", "title": "Daily", "tab": "Work", "category": "Clients"},
        {"op": "create", "url": "https://c.example/", "title": "Tools", "tab": "Work", "category": "Utilities"},
    ], "setup", dry_run=False)
    send_history(api, [("https://b.example/", "", 50), ("https://a.example/", "", 1), ("https://c.example/", "", 200)])
    s = make(api)
    text = s.preview_changes([{"op": "sort_by_use", "tab": "work"}], "Most used first")
    preview = s.previews["P1"].result
    pos = {e["title"]: e["position"] for e in preview["diff"]}
    assert pos == {"Daily": [3, 1], "ACME blog": [1, 3]}  # Rarely (1 visit) stays second
    assert "place 3 → 1" in text and 'apply_changes("P1")' in text
    if preview["categories"]:  # both categories in one column: Utilities (200 visits) goes first
        assert "ORDER   Work / Utilities" in text
    assert "Applied as change #" in s.apply_changes("P1")
    assert "Undid #" in s.undo()
    assert {b.title: b.position for b in s.store.bookmarks.values() if b.category == "Clients"} == \
        {"ACME blog": 0, "Rarely": 1, "Daily": 2}
