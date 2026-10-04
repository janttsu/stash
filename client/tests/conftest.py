"""A real Stash app on a throwaway database, and a scripted stand-in for the model."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

STASH_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("STASH_DATA", tempfile.mkdtemp(prefix="stashai-test-"))
os.environ["STASH_INSECURE_COOKIE"] = "1"
sys.path.insert(0, str(STASH_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from stashai.api import StashAPI  # noqa: E402
from stashai.store import Store  # noqa: E402

BOOKMARKS = [
    {"url": "https://acme.example/docs", "title": "ACME documentation", "tags": ["acme", "docs"]},
    {"url": "https://www.acme.example/blog", "title": "ACME blog", "tags": ["acme"], "tab": "Work", "category": "Clients"},
    {"url": "https://support.acme.example/kb/12", "title": "Knowledge base: DNS", "tags": ["dns"]},
    {"url": "https://news.example/acme-buys-widgets", "title": "Acme buys Widgets Inc", "tags": ["news"]},
    {"url": "https://recipes.example/buns", "title": "Bun recipe", "tags": ["food"]},
    {"url": "https://x.example/", "title": "X", "tags": []},
    {"url": "https://example.org/max", "title": "Maximum likelihood", "tags": ["ml"]},
]


class FakeLLM:
    """Answers from a script: each item is a dict (sent as JSON), a string, or a function of the messages."""

    def __init__(self, script=None, judge=None):
        self.script = list(script or [])
        self.judge = judge            # function(question, ids) -> (match, unsure)
        self.calls: list[list[dict]] = []
        self.model = "fake"
        self.num_ctx = 0
        self.last_stats: dict = {}

    def chat(self, messages, *, schema=None, temperature=None, max_tokens=0, cancel=None, on_progress=None):
        self.calls.append([dict(m) for m in messages])  # a copy: the agent keeps appending to its list
        if cancel is not None and cancel.is_set():
            from stashai.llm import Cancelled
            raise Cancelled()
        if messages[0]["content"] == "Reply with {}":  # the TUI's warm-up request
            return "{}"
        if messages[0]["content"].startswith("You check bookmarks"):
            text = messages[1]["content"]
            question = text.split("\n", 1)[0].removeprefix("QUESTION: ")
            ids = [int(line[1:].split()[0]) for line in text.splitlines() if line.startswith("#")]
            match, unsure = self.judge(question, ids) if self.judge else ([], [])
            return json.dumps({"match": match, "unsure": unsure})
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        return item if isinstance(item, str) else json.dumps(item)

    def context_length(self):
        return 32768

    def model_details(self):
        return [{"name": n, "size_gb": g, "params": "", "quant": "", "loaded": False}
                for n, g in (("fake", 1.0), ("qwen3.6:35b-a3b", 23.9), ("small:9b", 6.6))]

    def health(self):
        return True, "fake"


@pytest.fixture(scope="session")
def stash():
    from app import cli, db
    from app.main import app

    with TestClient(app) as raw:
        web = TestClient(app, headers={"X-Stash": "1"})
        cli.main(["invite"])
        code = db.connect().execute("SELECT code FROM invites WHERE used_by IS NULL").fetchone()["code"]
        r = web.post("/api/register", json={"username": "tester", "password": "tester password", "invite": code})
        assert r.status_code == 200, r.text
        key = web.post("/api/keys", json={"name": "test", "can_write": True}).json()["key"]
        yield {"app": app, "key": key, "web": web, "raw": raw}


@pytest.fixture
def api(stash):
    """A StashAPI talking to the in-process app, reset to BOOKMARKS for each test."""
    from app import db

    con = db.connect()
    con.execute("DELETE FROM bookmarks")
    con.execute("DELETE FROM changesets")
    con.execute("DELETE FROM categories WHERE name='Clients'")
    con.execute("DELETE FROM tabs WHERE name='Work'")
    con.close()
    client = StashAPI("https://stash.test", stash["key"])
    client.http = TestClient(stash["app"], base_url="http://testserver/api/v1",
                             headers={"Authorization": f"Bearer {stash['key']}"})
    client.changes([{"op": "create", **b} for b in BOOKMARKS], "test data", dry_run=False)
    return client


@pytest.fixture
def store(api):
    return Store(api.snapshot())


def ids_by_title(store: Store) -> dict[str, int]:
    return {b.title: b.id for b in store.bookmarks.values()}
