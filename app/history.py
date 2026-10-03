"""Browsing history sent by your devices (scripts/stash-history-sync.py), for stashai to study.

Each device ("source") sends one row per address it has visited, with visit counts for the last
30, 90 and 365 days as of the sync. A sync replaces the device's earlier rows. The history is
matched to bookmarks by a loose address key (http/https, www. and a trailing slash are ignored).
It is never part of the bookmark export and is deleted with the account or per device in Settings.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from . import db
from .api_v1 import KeyCtx, key_ctx, writer
from .main import Ctx, clean_text, ctx, err, like_escape

MAX_ITEMS_PER_CALL = 5000
MAX_HISTORY = 300_000

v1 = APIRouter(prefix="/api/v1/history")
session_api = APIRouter(prefix="/api/history")


def url_key(url: str) -> str:
    """The same page whatever the scheme, a leading www. or a trailing slash; the fragment is dropped."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.hostname or "").lower().removeprefix("www.")
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = p.path.rstrip("/")
    return f"{host}{port}{path}" + (f"?{p.query}" if p.query else "")


class Item(BaseModel):
    url: str = Field(max_length=4000)
    title: str = Field("", max_length=1000)
    visits: int = Field(0, ge=0)
    visits_30d: int = Field(0, ge=0)
    visits_90d: int = Field(0, ge=0)
    visits_365d: int = Field(0, ge=0)
    first_visit: int | None = None
    last_visit: int | None = None


class Upload(BaseModel):
    items: list[Item] = Field([], max_length=MAX_ITEMS_PER_CALL)
    browser: str = Field("", max_length=50)
    reset: bool = False    # first call of a sync: forget this device's earlier rows
    done: bool = False     # last call of a sync: record the time and the count


def source_name(source: str) -> str:
    name = clean_text(source, 60)
    if not name or not re.fullmatch(r"[\w .@-]+", name):
        raise err(422, "bad_source")
    return name


@v1.post("/{source}")
def upload(source: str, body: Upload, c: KeyCtx = Depends(writer)):
    source = source_name(source)
    con = c.con
    rows = []
    for it in body.items:
        if not it.url.startswith(("http://", "https://")):
            continue
        host = (urlsplit(it.url).hostname or "").lower()
        rows.append((c.uid, source, it.url, url_key(it.url), host, clean_text(it.title, 300), it.visits,
                     it.visits_30d, it.visits_90d, it.visits_365d, it.first_visit, it.last_visit))
    with db.tx(con):
        if body.reset:
            con.execute("DELETE FROM history WHERE user_id=? AND source=?", (c.uid, source))
        total = con.execute("SELECT COUNT(*) FROM history WHERE user_id=?", (c.uid,)).fetchone()[0]
        if total + len(rows) > MAX_HISTORY:
            raise err(422, "limit_reached")
        con.executemany(
            "INSERT INTO history(user_id, source, url, url_key, host, title, visits, visits_30d, visits_90d,"
            " visits_365d, first_visit, last_visit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id, source, url) DO UPDATE SET title=excluded.title, visits=excluded.visits,"
            " visits_30d=excluded.visits_30d, visits_90d=excluded.visits_90d, visits_365d=excluded.visits_365d,"
            " first_visit=excluded.first_visit, last_visit=excluded.last_visit", rows)
        if body.done or body.reset:
            count = con.execute("SELECT COUNT(*) FROM history WHERE user_id=? AND source=?",
                                (c.uid, source)).fetchone()[0]
            con.execute(
                "INSERT INTO history_sources(user_id, source, browser, items, synced_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_id, source) DO UPDATE SET browser=excluded.browser, items=excluded.items,"
                " synced_at=COALESCE(excluded.synced_at, synced_at)",
                (c.uid, source, clean_text(body.browser, 50), count, db.now() if body.done else None))
    return {"stored": len(rows)}


def sources(c: Ctx) -> list[dict]:
    """Devices with their row count and the time span their history covers."""
    return [dict(r) for r in c.con.execute(
        "SELECT s.source, s.browser, s.items, s.synced_at,"
        " (SELECT MIN(first_visit) FROM history h WHERE h.user_id=s.user_id AND h.source=s.source) AS first_visit,"
        " (SELECT MAX(last_visit) FROM history h WHERE h.user_id=s.user_id AND h.source=s.source) AS last_visit"
        " FROM history_sources s WHERE s.user_id=? ORDER BY s.source", (c.uid,))]


def delete_source(c: Ctx, source: str) -> int:
    with db.tx(c.con):
        n = c.con.execute("DELETE FROM history WHERE user_id=? AND source=?", (c.uid, source)).rowcount
        c.con.execute("DELETE FROM history_sources WHERE user_id=? AND source=?", (c.uid, source))
    return n


@v1.get("/sources")
def get_sources(c: KeyCtx = Depends(key_ctx)):
    return {"sources": sources(c)}


@v1.delete("/{source}")
def remove_source(source: str, c: KeyCtx = Depends(writer)):
    return {"deleted": delete_source(c, source)}


def bookmark_keys(c: Ctx) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for r in c.con.execute("SELECT id, url FROM bookmarks WHERE user_id=?", (c.uid,)):
        out.setdefault(url_key(r["url"]), []).append(r["id"])
    return out


AGG = ("SELECT url_key, MAX(url) AS url, MAX(title) AS title, MAX(host) AS host, SUM(visits) AS visits,"
       " SUM(visits_30d) AS visits_30d, SUM(visits_90d) AS visits_90d, SUM(visits_365d) AS visits_365d,"
       " MIN(first_visit) AS first_visit, MAX(last_visit) AS last_visit FROM history")


@v1.get("/usage")
def usage(c: KeyCtx = Depends(key_ctx)):
    """Visit counts of every bookmark that appears in the history: {bookmark id: {...}}."""
    keys = bookmark_keys(c)
    out = {}
    for r in c.con.execute(f"{AGG} WHERE user_id=? GROUP BY url_key", (c.uid,)):
        for bid in keys.get(r["url_key"], []):
            out[bid] = {k: r[k] for k in ("visits", "visits_30d", "visits_90d", "visits_365d", "last_visit")}
    return {"usage": out, "sources": sources(c)}


SORTS = {"visits_90d": "visits_90d DESC", "visits_30d": "visits_30d DESC", "visits_365d": "visits_365d DESC",
         "visits": "visits DESC", "last_visit": "last_visit DESC"}


@v1.get("")
def search(q: str = "", host: str = "", min_visits: int = Query(1, ge=0),
           period: Literal["30d", "90d", "365d", "all"] = "90d", bookmarked: Literal["any", "yes", "no"] = "any",
           sort: str = "visits_90d", offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000),
           c: KeyCtx = Depends(key_ctx)):
    """Visited addresses across devices, most visited first, with the bookmarks they match."""
    where, params = ["user_id=?"], [c.uid]
    for term in q.casefold().split()[:10]:
        where.append("(lower(url) LIKE ? ESCAPE '\\' OR lower(title) LIKE ? ESCAPE '\\')")
        params += [f"%{like_escape(term)}%"] * 2
    if host:
        h = host.lower().removeprefix("www.")
        where.append("(host=? OR host=? OR host LIKE ? ESCAPE '\\')")
        params += [h, "www." + h, "%." + like_escape(h)]
    column = "visits" if period == "all" else f"visits_{period}"
    rows = c.con.execute(f"{AGG} WHERE {' AND '.join(where)} GROUP BY url_key HAVING SUM({column}) >= ?"
                         f" ORDER BY {SORTS.get(sort, SORTS['visits_90d'])}, url_key", [*params, min_visits]).fetchall()
    keys = bookmark_keys(c)
    items = []
    for r in rows:
        ids = keys.get(r["url_key"], [])
        if bookmarked == "yes" and not ids or bookmarked == "no" and ids:
            continue
        items.append({k: r[k] for k in ("url", "title", "host", "visits", "visits_30d", "visits_90d", "visits_365d",
                                        "first_visit", "last_visit")} | {"bookmarks": ids})
    stored = c.con.execute("SELECT COUNT(DISTINCT url_key) FROM history WHERE user_id=?", (c.uid,)).fetchone()[0]
    return {"total": len(items), "stored": stored, "items": items[offset:offset + limit]}


# --- for the web UI -------------------------------------------------------------

@session_api.get("/sources")
def web_sources(c: Ctx = Depends(ctx)):
    return {"sources": sources(c)}


@session_api.delete("/{source}")
def web_delete(source: str, c: Ctx = Depends(ctx)):
    return {"deleted": delete_source(c, source)}
