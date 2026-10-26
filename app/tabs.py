"""Open browser tabs, sent to Stash to be tidied later.

The idea: a browser with two hundred tabs is emptied by sending the tabs here (the extension's menu, or a pasted
list of addresses), then an assistant drops the ones not worth keeping, saves the ones worth keeping as
bookmarks, and marks the rest, so the person only has to look through the few that were really left unfinished.

A sending is a list (`open_tab_lists`); each tab in it has a state: open (still to look at), done (finished with),
saved (now a bookmark) or dropped (not worth keeping). Saving makes ordinary bookmarks through the same code as
every other way of adding them; the list itself is not bookmark data and has no changesets, trash or undo.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import db, importer
from .main import (MAX_BOOKMARKS, Ctx, bookmark_count, clean_text, clean_url, ctx, err, insert_bookmark,
                   next_position, norm_tags, own, url_host)

MAX_TABS_PER_LIST = 2000
MAX_LISTS = 100
MAX_NOTE = 500
STATES = ("open", "done", "saved", "dropped")
State = Literal["open", "done", "saved", "dropped"]
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

session_api = APIRouter(prefix="/api/open-tabs")   # /api/tabs belongs to the Dashboard's tabs


def _short(value, limit: int) -> str:
    return clean_text(_CONTROL.sub(" ", str(value or "")), limit)


# --- storing ------------------------------------------------------------------------------------------

def import_tabs(c: Ctx, items: list[dict], name: str = "", text: str = "") -> dict:
    """Store a new list from tabs ({url, title}) and/or the addresses found in pasted text."""
    found: list[dict] = []
    seen: set[str] = set()
    for item in items:
        found.append({"url": str(item.get("url") or ""), "title": _short(item.get("title"), 500)})
    if text:
        urls, _ = importer.find_urls(text)
        found += [{"url": u, "title": ""} for u in urls]
    tabs: list[dict] = []
    invalid = 0
    for item in found:
        try:
            url = clean_url(item["url"], guess=False)
        except Exception:  # noqa: BLE001  (HTTPException: not a web address, e.g. about:blank)
            invalid += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        tabs.append({"url": url, "title": item["title"] or url})
    if not tabs:
        raise err(422, "no_tabs: nothing in the list is a web address")
    tabs = tabs[:MAX_TABS_PER_LIST]
    con = c.con
    if con.execute("SELECT COUNT(*) FROM open_tab_lists WHERE user_id=?", (c.uid,)).fetchone()[0] >= MAX_LISTS:
        raise err(422, "too_many_lists: delete old lists first")
    t = db.now()
    with db.tx(con):
        lid = con.execute("INSERT INTO open_tab_lists(user_id, name, created_at) VALUES(?,?,?)",
                          (c.uid, _short(name, 100), t)).lastrowid
        # tabs already bookmarked are marked saved at once, pointing at the bookmark
        have = {r["url"]: r["id"] for r in con.execute("SELECT id, url FROM bookmarks WHERE user_id=?", (c.uid,))}
        con.executemany(
            "INSERT INTO open_tabs(user_id, list_id, position, url, host, title, state, bookmark_id, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(c.uid, lid, i, x["url"], url_host(x["url"]), x["title"], "saved" if x["url"] in have else "open",
              have.get(x["url"]), t) for i, x in enumerate(tabs)])
    already = sum(1 for x in tabs if x["url"] in have)
    return {"list_id": lid, "tabs": len(tabs), "already_bookmarked": already, "invalid": invalid}


def lists(c: Ctx) -> list[dict]:
    rows = c.con.execute(
        "SELECT l.id, l.name, l.created_at,"
        " SUM(t.state='open') AS open, SUM(t.state='done') AS done, SUM(t.state='saved') AS saved,"
        " SUM(t.state='dropped') AS dropped, COUNT(t.id) AS total"
        " FROM open_tab_lists l LEFT JOIN open_tabs t ON t.list_id=l.id WHERE l.user_id=?"
        " GROUP BY l.id ORDER BY l.created_at DESC, l.id DESC", (c.uid,)).fetchall()
    return [{k: (r[k] or 0) if k in ("open", "done", "saved", "dropped", "total") else r[k] for k in r.keys()}
            for r in rows]


def tab_json(r) -> dict:
    return {"id": r["id"], "list_id": r["list_id"], "url": r["url"], "host": r["host"], "title": r["title"],
            "state": r["state"], "note": r["note"], "bookmark_id": r["bookmark_id"], "updated_at": r["updated_at"]}


def tabs_of(c: Ctx, list_id: Optional[int] = None, state: str = "", text: str = "", host: str = "",
            offset: int = 0, limit: int = 200, ids: list[int] | None = None) -> dict:
    where, args = ["user_id=?"], [c.uid]
    if ids is not None:
        ids = ids[:MAX_TABS_PER_LIST]
        where.append(f"id IN ({db.marks(len(ids))})")
        args += ids
    if list_id is not None:
        own(c.con, "open_tab_lists", list_id, c.uid)
        where.append("list_id=?")
        args.append(list_id)
    if state:
        if state not in STATES:
            raise err(422, "bad_state")
        where.append("state=?")
        args.append(state)
    if host:
        where.append("(host=? OR host LIKE ?)")
        args += [host.lower(), f"%.{host.lower()}"]
    for word in text.casefold().split()[:10]:
        where.append("(lower(title) LIKE ? OR lower(url) LIKE ? OR lower(note) LIKE ?)")
        args += [f"%{word}%"] * 3
    sql = f"FROM open_tabs WHERE {' AND '.join(where)}"
    total = c.con.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
    rows = c.con.execute(f"SELECT * {sql} ORDER BY list_id DESC, position LIMIT ? OFFSET ?",
                         [*args, max(1, min(1000, limit)), max(0, offset)]).fetchall()
    return {"total": total, "offset": max(0, offset), "items": [tab_json(r) for r in rows]}


def _own_tabs(c: Ctx, ids: list[int]) -> list:
    rows = []
    for part in db.chunks(ids):
        rows += c.con.execute(f"SELECT * FROM open_tabs WHERE user_id=? AND id IN ({db.marks(len(part))}) ORDER BY id",
                              [c.uid, *part]).fetchall()
    if not rows:
        raise err(404, "no_such_tabs")
    return rows


def resolve(c: Ctx, ids: list[int], state: str, note: Optional[str] = None) -> dict:
    """Mark tabs open, done or dropped (saved is set by save). A note explains the decision or what is left."""
    if state not in ("open", "done", "dropped"):
        raise err(422, "bad_state: open, done or dropped")
    rows = _own_tabs(c, ids)
    t = db.now()
    with db.tx(c.con):
        for part in db.chunks([r["id"] for r in rows]):
            if note is None:
                c.con.execute(f"UPDATE open_tabs SET state=?, updated_at=? WHERE id IN ({db.marks(len(part))})",
                              [state, t, *part])
            else:
                c.con.execute(f"UPDATE open_tabs SET state=?, note=?, updated_at=? WHERE id IN ({db.marks(len(part))})",
                              [state, _short(note, MAX_NOTE), t, *part])
    return {"updated": len(rows), "state": state}


def save(c: Ctx, ids: list[int], tags: list[str] | None = None, category_id: Optional[int] = None,
         note: Optional[str] = None) -> dict:
    """Make bookmarks of these tabs (in a Dashboard category, or the Catalog) and mark them saved. A tab whose
    address is already bookmarked is just marked saved, pointing at that bookmark."""
    rows = _own_tabs(c, ids)
    con = c.con
    if category_id is not None:
        own(con, "categories", category_id, c.uid)
    tags = norm_tags(tags or [])
    room = MAX_BOOKMARKS - bookmark_count(con, c.uid)
    have = {r["url"]: r["id"] for r in con.execute("SELECT id, url FROM bookmarks WHERE user_id=?", (c.uid,))}
    t, made, existing, new_ids = db.now(), 0, 0, []
    with db.tx(con):
        position = next_position(con, category_id)
        for r in rows:
            bid = have.get(r["url"])
            if bid is None:
                if made >= room:
                    raise err(422, "limit_reached")
                bid = insert_bookmark(con, c.uid, r["url"], r["title"] or r["url"], "", tags, category_id, position, t)
                have[r["url"]] = bid
                new_ids.append(bid)
                position += 1
                made += 1
            else:
                existing += 1
            if note is None:
                con.execute("UPDATE open_tabs SET state='saved', bookmark_id=?, updated_at=? WHERE id=?", (bid, t, r["id"]))
            else:
                con.execute("UPDATE open_tabs SET state='saved', bookmark_id=?, note=?, updated_at=? WHERE id=?",
                            (bid, _short(note, MAX_NOTE), t, r["id"]))
        db.reindex(con, new_ids)
    return {"saved": len(rows), "bookmarks_made": made, "already_bookmarked": existing, "bookmark_ids": new_ids}


def mark_saved(c: Ctx, ids: list[int], note: Optional[str] = None) -> dict:
    """After bookmarks were made another way (the MCP tools create them as an undoable change): point the tabs
    at the bookmarks with the same address and mark them saved."""
    rows = _own_tabs(c, ids)
    con = c.con
    have = {r["url"]: r["id"] for r in con.execute("SELECT id, url FROM bookmarks WHERE user_id=?", (c.uid,))}
    t, missing = db.now(), 0
    with db.tx(con):
        for r in rows:
            bid = have.get(r["url"])
            if bid is None:
                missing += 1
                continue
            if note is None:
                con.execute("UPDATE open_tabs SET state='saved', bookmark_id=?, updated_at=? WHERE id=?", (bid, t, r["id"]))
            else:
                con.execute("UPDATE open_tabs SET state='saved', bookmark_id=?, note=?, updated_at=? WHERE id=?",
                            (bid, _short(note, MAX_NOTE), t, r["id"]))
    return {"saved": len(rows) - missing, "not_bookmarked": missing}


def delete_list(c: Ctx, list_id: int) -> dict:
    own(c.con, "open_tab_lists", list_id, c.uid)
    with db.tx(c.con):
        c.con.execute("DELETE FROM open_tab_lists WHERE id=?", (list_id,))
    return {"deleted": list_id}


# --- the web UI, with the session cookie ----------------------------------------------------------------

class TabIn(BaseModel):
    url: str = Field(max_length=4000)
    title: str = Field("", max_length=2000)


class ImportBody(BaseModel):
    name: str = Field("", max_length=100)
    items: list[TabIn] = Field(default_factory=list, max_length=MAX_TABS_PER_LIST)
    text: str = Field("", max_length=400_000)


class Resolve(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_TABS_PER_LIST)
    state: Literal["open", "done", "dropped"]
    note: Optional[str] = Field(None, max_length=MAX_NOTE)


class SaveBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_TABS_PER_LIST)
    tags: list[str] = Field(default_factory=list, max_length=100)
    category_id: Optional[int] = None
    note: Optional[str] = Field(None, max_length=MAX_NOTE)


@session_api.get("")
def web_lists(c: Ctx = Depends(ctx)):
    return {"lists": lists(c)}


@session_api.post("/import")
def web_import(body: ImportBody, c: Ctx = Depends(ctx)):
    return import_tabs(c, [x.model_dump() for x in body.items], body.name, body.text)


@session_api.get("/items")
def web_items(list_id: Optional[int] = None, state: str = "", q: str = "", host: str = "", offset: int = 0,
              limit: int = 200, c: Ctx = Depends(ctx)):
    return tabs_of(c, list_id, state, q, host, offset, limit)


@session_api.post("/resolve")
def web_resolve(body: Resolve, c: Ctx = Depends(ctx)):
    return resolve(c, body.ids, body.state, body.note)


@session_api.post("/save")
def web_save(body: SaveBody, c: Ctx = Depends(ctx)):
    return save(c, body.ids, body.tags, body.category_id, body.note)


@session_api.delete("/{list_id}")
def web_delete(list_id: int, c: Ctx = Depends(ctx)):
    return delete_list(c, list_id)
