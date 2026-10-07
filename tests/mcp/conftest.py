"""The MCP tools against a real Stash app on a throwaway database:  .venv/bin/python -m pytest -q tests/mcp"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

STASH_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("STASH_DATA", tempfile.mkdtemp(prefix="stash-mcp-test-"))
os.environ["STASH_MAINTENANCE"] = "off"
os.environ["STASH_INSECURE_COOKIE"] = "1"
sys.path.insert(0, str(STASH_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.mcpserver.local import LocalAPI  # noqa: E402
from app.mcpserver.store import Store  # noqa: E402

BOOKMARKS = [
    {"url": "https://acme.example/docs", "title": "ACME documentation", "tags": ["acme", "docs"]},
    {"url": "https://www.acme.example/blog", "title": "ACME blog", "tags": ["acme"], "tab": "Work", "category": "Clients"},
    {"url": "https://support.acme.example/kb/12", "title": "Knowledge base: DNS", "tags": ["dns"]},
    {"url": "https://news.example/acme-buys-widgets", "title": "Acme buys Widgets Inc", "tags": ["news"]},
    {"url": "https://recipes.example/buns", "title": "Bun recipe", "tags": ["food"]},
    {"url": "https://x.example/", "title": "X", "tags": []},
    {"url": "https://example.org/max", "title": "Maximum likelihood", "tags": ["ml"]},
]


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
    """The tools' view of Stash for the test user's key, reset to BOOKMARKS for each test."""
    from app import db

    con = db.connect()
    con.execute("DELETE FROM bookmarks")
    con.execute("DELETE FROM trash")
    con.execute("DELETE FROM changesets")
    con.execute("DELETE FROM categories WHERE name='Clients'")
    con.execute("DELETE FROM tabs WHERE name='Work'")
    key_id = con.execute("SELECT id FROM api_keys WHERE prefix=?", (stash["key"][:12],)).fetchone()[0]
    con.close()
    client = LocalAPI(key_id)
    # plain HTTP for what the tests send on their own (e.g. browsing history)
    client.http = TestClient(stash["app"], base_url="http://testserver/api/v1",
                             headers={"Authorization": f"Bearer {stash['key']}"})
    client.changes([{"op": "create", **b} for b in BOOKMARKS], "test data", dry_run=False)
    return client


@pytest.fixture
def store(api):
    return Store(api.snapshot())


def ids_by_title(store: Store) -> dict[str, int]:
    return {b.title: b.id for b in store.bookmarks.values()}
