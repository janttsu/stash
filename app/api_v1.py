"""Programming interface for other machines and tools (/api/v1), authenticated with API keys.

A key is sent as `Authorization: Bearer stash_…`. Session cookies are never accepted here, so the
CSRF header of the browser API is not needed either. Keys are created in Settings → API keys and are
read-only unless created with write access.

Every change goes through POST /api/v1/changes: a list of operations applied in one transaction.
With `dry_run` the operations run and are rolled back, so the preview shows exactly what would happen.
A real run is stored as a changeset with the earlier state of every touched bookmark, and can be undone.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from . import db, security
from .main import (
    MAX_BOOKMARKS, Ctx, Filter, add_tags, bookmark_count, check_color, clean_text, clean_url, client_ip, ctx, err,
    filter_sql, get_db, insert_bookmark, move_bookmarks, new_category, new_tab, next_position, norm_tag,
    norm_tags, own, remove_tags, set_tags, tags_for, url_host,
)
from .security import limiter

KEY_PREFIX = "stash_"
MAX_KEYS = 20
MAX_OPS = 500
KEEP_CHANGESETS = 200
SNAPSHOT_COLUMNS = ("id", "category_id", "title", "url", "host", "notes", "color", "position", "created_at",
                    "updated_at")

v1 = APIRouter(prefix="/api/v1")
session_api = APIRouter(prefix="/api")


# --- authentication ----------------------------------------------------------

class KeyCtx(Ctx):
    def __init__(self, con, user, key):
        super().__init__(con, user)
        self.key = key


def key_ctx(request: Request, con=Depends(get_db)) -> KeyCtx:
    auth = request.headers.get("authorization", "")
    if not auth[:7].lower() == "bearer ":
        raise err(401, "api_key_required")
    ip = client_ip(request)
    if limiter.blocked(f"apikey:{ip}", 20, 600):
        raise err(429, "too_many_attempts")
    token = auth[7:].strip()
    key = con.execute("SELECT * FROM api_keys WHERE key_hash=?", (security.token_hash(token),)).fetchone() \
        if token.startswith(KEY_PREFIX) else None
    if not key:
        limiter.hit(f"apikey:{ip}")
        raise err(401, "bad_api_key")
    user = con.execute("SELECT * FROM users WHERE id=?", (key["user_id"],)).fetchone()
    t = db.now()
    if not key["last_used"] or t - key["last_used"] > 60 or key["last_ip"] != ip:
        con.execute("UPDATE api_keys SET last_used=?, last_ip=? WHERE id=?", (t, ip, key["id"]))
    return KeyCtx(con, user, key)


def writer(c: KeyCtx = Depends(key_ctx)) -> KeyCtx:
    if not c.key["can_write"]:
        raise err(403, "read_only_key")
    return c


# --- reading -------------------------------------------------------------------

def locations(con, uid: int) -> dict[int, tuple[str, str]]:
    """category id -> (tab name, category name)"""
    return {r["id"]: (r["tab"], r["name"]) for r in con.execute(
        "SELECT c.id, c.name, t.name AS tab FROM categories c JOIN tabs t ON t.id=c.tab_id WHERE c.user_id=?", (uid,))}


def item_json(row, tags, where) -> dict:
    tab, cat = where.get(row["category_id"], ("", "")) if row["category_id"] else ("", "")
    return {"id": row["id"], "url": row["url"], "title": row["title"], "notes": row["notes"], "host": row["host"],
            "tags": tags, "category_id": row["category_id"], "tab": tab, "category": cat, "position": row["position"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def structure(con, uid: int) -> list[dict]:
    counts = {r[0]: r[1] for r in con.execute(
        "SELECT category_id, COUNT(*) FROM bookmarks WHERE user_id=? AND category_id IS NOT NULL GROUP BY 1", (uid,))}
    tabs = []
    for t in con.execute("SELECT id, name FROM tabs WHERE user_id=? ORDER BY position, id", (uid,)).fetchall():
        cats = con.execute("SELECT id, name, col, position FROM categories WHERE tab_id=? ORDER BY col, position, id",
                           (t["id"],)).fetchall()
        tabs.append({"id": t["id"], "name": t["name"],
                     "categories": [{"id": k["id"], "name": k["name"], "count": counts.get(k["id"], 0), "col": k["col"],
                                     "position": k["position"]} for k in cats]})
    return tabs


def tag_counts(con, uid: int) -> list[list]:
    return [[r[0], r[1]] for r in con.execute(
        "SELECT bt.tag, COUNT(*) AS n FROM bookmark_tags bt JOIN bookmarks b ON b.id=bt.bookmark_id"
        " WHERE b.user_id=? GROUP BY bt.tag ORDER BY n DESC, bt.tag", (uid,))]


@v1.get("/me")
def me(c: KeyCtx = Depends(key_ctx)):
    return {"user": c.user["username"], "key": c.key["name"], "can_write": bool(c.key["can_write"]),
            "bookmarks": bookmark_count(c.con, c.uid)}


@v1.get("/bookmarks")
def list_bookmarks(q: str = "", tags: str = "", mode: Literal["exact", "all", "any"] = "all", untagged: bool = False,
                   scope: Literal["all", "dashboard", "catalog"] = "all", host: str = "", ids: str = "",
                   offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000),
                   c: KeyCtx = Depends(key_ctx)):
    f = Filter(scope=scope, tags=[t for t in tags.split(",") if t], q=q[:300], mode=mode, untagged=untagged)
    where, params = filter_sql(c.uid, f)
    if host:
        host = host.lower().removeprefix("www.")
        where += " AND (b.host=? OR b.host=? OR b.host LIKE ? ESCAPE '\\')"
        params += [host, "www." + host, "%." + host.replace("%", "\\%").replace("_", "\\_")]
    if ids:
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()][:1000]
        where += f" AND b.id IN ({db.marks(len(wanted))})" if wanted else " AND 0"
        params += wanted
    total = c.con.execute(f"SELECT COUNT(*) FROM bookmarks b WHERE {where}", params).fetchone()[0]
    rows = c.con.execute(f"SELECT b.* FROM bookmarks b WHERE {where} ORDER BY b.id LIMIT ? OFFSET ?",
                         [*params, limit, offset]).fetchall()
    tag_map, where_map = tags_for(c.con, [r["id"] for r in rows]), locations(c.con, c.uid)
    return {"total": total, "items": [item_json(r, tag_map.get(r["id"], []), where_map) for r in rows]}


@v1.get("/snapshot")
def snapshot_all(c: KeyCtx = Depends(key_ctx)):
    """Everything at once: all bookmarks, the Dashboard structure and the tags."""
    rows = c.con.execute("SELECT * FROM bookmarks WHERE user_id=? ORDER BY id", (c.uid,)).fetchall()
    tag_map, where_map = tags_for(c.con, [r["id"] for r in rows]), locations(c.con, c.uid)
    return {"bookmarks": [item_json(r, tag_map.get(r["id"], []), where_map) for r in rows],
            "tabs": structure(c.con, c.uid), "tags": tag_counts(c.con, c.uid)}


@v1.get("/tags")
def list_tags(c: KeyCtx = Depends(key_ctx)):
    return {"tags": tag_counts(c.con, c.uid)}


@v1.get("/structure")
def get_structure(c: KeyCtx = Depends(key_ctx)):
    return {"tabs": structure(c.con, c.uid),
            "catalog": c.con.execute("SELECT COUNT(*) FROM bookmarks WHERE user_id=? AND category_id IS NULL",
                                     (c.uid,)).fetchone()[0]}


# --- changes -------------------------------------------------------------------

class Op(BaseModel):
    op: Literal["add_tags", "remove_tags", "set_tags", "rename_tag", "delete", "move", "update", "create",
                "order_bookmarks", "order_categories"]
    ids: list[int] = Field([], max_length=MAX_BOOKMARKS)
    tags: list[str] = Field([], max_length=100)
    old: str = Field("", max_length=100)       # rename_tag
    new: str = Field("", max_length=100)       # rename_tag; "" removes the tag everywhere
    # move / create: a category by id, or by tab and category name (created when missing), or the Catalog
    category_id: Optional[int] = None
    tab: str = Field("", max_length=200)
    category: str = Field("", max_length=200)
    catalog: bool = False
    # update / create
    id: Optional[int] = None
    url: Optional[str] = Field(None, max_length=2000)
    title: Optional[str] = Field(None, max_length=2000)
    notes: Optional[str] = Field(None, max_length=5000)
    color: Optional[str] = None
    skip_existing: bool = True                 # create: skip a URL that is already bookmarked
    # order_categories: the tab's categories (ids) in the wanted order; each column keeps its categories
    categories: list[int] = Field([], max_length=MAX_BOOKMARKS)


class Changes(BaseModel):
    ops: list[Op] = Field(max_length=MAX_OPS)
    summary: str = Field("", max_length=500)
    dry_run: bool = False


def rows_state(con, ids) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for part in db.chunks(ids):
        for r in con.execute(f"SELECT * FROM bookmarks WHERE id IN ({db.marks(len(part))})", part):
            out[r["id"]] = {k: r[k] for k in SNAPSHOT_COLUMNS}
    for bid, tags in tags_for(con, list(out)).items():
        out[bid]["tags"] = tags
    for row in out.values():
        row.setdefault("tags", [])
    return out


class Runner:
    """Applies operations one by one, remembering each touched bookmark's state before its first change."""

    def __init__(self, c: Ctx):
        self.c, self.con = c, c.con
        self.before: dict[int, Optional[dict]] = {}   # None = created by this changeset
        self.created_categories: list[int] = []
        self.created_tabs: list[int] = []
        self.skipped: list[dict] = []
        self.categories_before: dict[int, dict] = {}   # category id -> {tab_id, col, position} before

    def remember(self, ids) -> None:
        fresh = [i for i in ids if i not in self.before]
        self.before.update(rows_state(self.con, fresh))

    def mine(self, ids) -> list[int]:
        found: set[int] = set()
        for part in db.chunks(sorted(set(ids))):
            found |= {r[0] for r in self.con.execute(
                f"SELECT id FROM bookmarks WHERE user_id=? AND id IN ({db.marks(len(part))})", [self.c.uid, *part])}
        missing = [i for i in dict.fromkeys(ids) if i not in found]
        if missing:
            self.skipped.append({"reason": "not_found", "ids": missing[:50], "count": len(missing)})
        return [i for i in dict.fromkeys(ids) if i in found]

    def target(self, op: Op, create: bool = True) -> Optional[int]:
        """Category id for move/create; None = the Catalog. Missing ones are created unless *create* is False."""
        if op.catalog:
            return None
        if op.category_id is not None:
            own(self.con, "categories", op.category_id, self.c.uid)
            return op.category_id
        tab_name, cat_name = clean_text(op.tab, 100), clean_text(op.category, 100)
        if not tab_name or not cat_name:
            raise err(422, "target_required")
        tab = self.con.execute("SELECT id FROM tabs WHERE user_id=? AND lower(name)=lower(?)",
                               (self.c.uid, tab_name)).fetchone()
        tab_id = tab[0] if tab else None
        if tab_id is None and not create:
            raise err(404, "not_found")
        if tab_id is None:
            tab_id = new_tab(self.con, self.c.uid, tab_name)
            self.created_tabs.append(tab_id)
        cat = self.con.execute("SELECT id FROM categories WHERE tab_id=? AND lower(name)=lower(?)",
                               (tab_id, cat_name)).fetchone()
        if cat:
            return cat[0]
        if not create:
            raise err(404, "not_found")
        cat_id = new_category(self.con, self.c.uid, tab_id, cat_name)
        self.created_categories.append(cat_id)
        return cat_id

    def run(self, op: Op) -> dict:
        con, uid = self.con, self.c.uid
        info: dict = {"op": op.op}
        if op.op in ("add_tags", "remove_tags", "set_tags", "delete", "move"):
            ids = self.mine(op.ids)
            info["matched"] = len(ids)
            self.remember(ids)
            tags = norm_tags(op.tags) if op.op != "remove_tags" else [norm_tag(t) for t in op.tags if norm_tag(t)]
            if op.op in ("add_tags", "set_tags", "remove_tags") and not tags and op.op != "set_tags":
                raise err(422, "bad_tag")
            if op.op == "add_tags":
                add_tags(con, ids, tags)
            elif op.op == "remove_tags":
                remove_tags(con, ids, tags)
            elif op.op == "set_tags":
                for i in ids:
                    set_tags(con, i, tags)
            elif op.op == "delete":
                for part in db.chunks(ids):
                    con.execute(f"DELETE FROM bookmarks WHERE id IN ({db.marks(len(part))})", part)
            else:
                category_id = self.target(op)
                moving = [i for i in ids if self.current_category(i) != category_id]
                move_bookmarks(self.c, moving, category_id)
            if op.op != "delete":
                db.reindex(con, ids)
        elif op.op == "rename_tag":
            old, new = norm_tag(op.old), norm_tag(op.new)
            if not old:
                raise err(422, "bad_tag")
            ids = [r[0] for r in con.execute(
                "SELECT bt.bookmark_id FROM bookmark_tags bt JOIN bookmarks b ON b.id=bt.bookmark_id"
                " WHERE b.user_id=? AND bt.tag=?", (uid, old))]
            info["matched"] = len(ids)
            self.remember(ids)
            if new and new != old:
                add_tags(con, ids, [new])
            if new != old:
                remove_tags(con, ids, [old])
            db.reindex(con, ids)
        elif op.op == "update":
            if op.id is None:
                raise err(422, "id_required")
            ids = self.mine([op.id])
            info["matched"] = len(ids)
            if ids:
                self.remember(ids)
                row = own(con, "bookmarks", op.id, uid)
                if op.url is not None:
                    url = clean_url(op.url)
                    con.execute("UPDATE bookmarks SET url=?, host=? WHERE id=?", (url, url_host(url), op.id))
                if op.title is not None:
                    con.execute("UPDATE bookmarks SET title=? WHERE id=?",
                                (clean_text(op.title, 500) or row["url"], op.id))
                if op.notes is not None:
                    con.execute("UPDATE bookmarks SET notes=? WHERE id=?", (op.notes.strip(), op.id))
                if op.color is not None:
                    con.execute("UPDATE bookmarks SET color=? WHERE id=?", (check_color(op.color), op.id))
                if op.tags:
                    set_tags(con, op.id, norm_tags(op.tags))
                con.execute("UPDATE bookmarks SET updated_at=? WHERE id=?", (db.now(), op.id))
                db.reindex(con, ids)
        elif op.op == "order_bookmarks":
            # the given bookmarks first, in that order; the category's other bookmarks keep their order after them
            category_id = self.target(op, create=False)
            if category_id is None:
                raise err(422, "category_required")
            current = [r[0] for r in con.execute(
                "SELECT id FROM bookmarks WHERE category_id=? ORDER BY position, id", (category_id,))]
            wanted = [i for i in dict.fromkeys(op.ids) if i in set(current)]
            if len(wanted) < len(set(op.ids)):
                self.skipped.append({"reason": "not_in_category", "count": len(set(op.ids)) - len(wanted)})
            order = wanted + [i for i in current if i not in set(wanted)]
            info["matched"] = len(wanted)
            self.remember(order)
            con.executemany("UPDATE bookmarks SET position=? WHERE id=?", [(n, b) for n, b in enumerate(order)])
        elif op.op == "order_categories":
            tab_name = clean_text(op.tab, 100)
            tab = con.execute("SELECT id FROM tabs WHERE user_id=? AND lower(name)=lower(?)", (uid, tab_name)).fetchone()
            if not tab:
                raise err(404, "not_found")
            cats = con.execute("SELECT id, tab_id, col, position FROM categories WHERE tab_id=? ORDER BY col, position, id",
                               (tab[0],)).fetchall()
            rank = {cid: n for n, cid in enumerate(dict.fromkeys(op.categories))}
            info["matched"] = sum(1 for k in cats if k["id"] in rank)
            for k in cats:
                self.categories_before.setdefault(k["id"], {"tab_id": k["tab_id"], "col": k["col"],
                                                            "position": k["position"]})
            for col in sorted({k["col"] for k in cats}):
                column = [k for k in cats if k["col"] == col]
                column.sort(key=lambda k: (rank.get(k["id"], len(rank)), k["position"], k["id"]))
                con.executemany("UPDATE categories SET position=? WHERE id=?",
                                [(n, k["id"]) for n, k in enumerate(column)])
        else:  # create
            url = clean_url(op.url or "")
            existing = con.execute("SELECT id FROM bookmarks WHERE user_id=? AND url=?", (uid, url)).fetchone()
            if existing and op.skip_existing:
                self.skipped.append({"reason": "exists", "url": url, "id": existing[0]})
                info["matched"] = 0
                return info
            if bookmark_count(con, uid) >= MAX_BOOKMARKS:
                raise err(422, "limit_reached")
            has_target = op.catalog or op.category_id is not None or op.tab or op.category
            category_id = self.target(op) if has_target else None
            bid = insert_bookmark(con, uid, url, clean_text(op.title or "", 500) or url, (op.notes or "").strip(),
                                  norm_tags(op.tags), category_id, next_position(con, category_id), db.now(),
                                  check_color(op.color or ""))
            db.reindex(con, [bid])
            self.before[bid] = None
            info.update(matched=1, id=bid)
        return info

    def current_category(self, bid: int) -> Optional[int]:
        return self.con.execute("SELECT category_id FROM bookmarks WHERE id=?", (bid,)).fetchone()[0]


def where_text(row: Optional[dict], where: dict) -> str:
    if row is None:
        return ""
    if row["category_id"] is None:
        return "Catalog"
    tab, cat = where.get(row["category_id"], ("?", "?"))
    return f"{tab} / {cat}"


def make_diff(before: dict[int, Optional[dict]], after: dict[int, dict], where_before: dict, where_after: dict):
    diff, counts = [], {"created": 0, "deleted": 0, "updated": 0, "unchanged": 0}
    for bid, old in before.items():
        new = after.get(bid)
        if old is None and new is None:
            continue
        if old is None:
            kind = "created"
        elif new is None:
            kind = "deleted"
        else:
            kind = "updated"
        ref = new or old
        entry: dict = {"id": bid, "change": kind, "title": ref["title"], "url": ref["url"]}
        if kind == "updated":
            changed = False
            # "title" and "url" stay the bookmark's current values; what changed goes into "fields"
            fields = {f: [old[f], new[f]] for f in ("title", "url", "notes", "color") if old[f] != new[f]}
            if fields:
                entry["fields"] = fields
                changed = True
            if sorted(old["tags"]) != sorted(new["tags"]):
                entry["tags"] = [old["tags"], new["tags"]]
                changed = True
            if old["category_id"] != new["category_id"]:
                entry["location"] = [where_text(old, where_before), where_text(new, where_after)]
                changed = True
            elif old["position"] != new["position"] and new["category_id"] is not None:
                entry["position"] = [old["position"] + 1, new["position"] + 1]
                changed = True
            if not changed:
                counts["unchanged"] += 1
                continue
        elif kind == "created":
            entry.update(tags=new["tags"], location=["", where_text(new, where_after)])
        else:
            entry.update(tags=old["tags"], location=[where_text(old, where_before), ""])
        counts[kind] += 1
        diff.append(entry)
    return diff, counts


def apply_changes(c: Ctx, body: Changes, key_name: str) -> dict:
    con = c.con
    where_before = locations(con, c.uid)
    runner = Runner(c)
    con.execute("BEGIN IMMEDIATE")
    try:
        results = [runner.run(op) for op in body.ops]
        after = rows_state(con, list(runner.before))
        diff, counts = make_diff(runner.before, after, where_before, locations(con, c.uid))
        moved_categories = category_diff(con, runner.categories_before, where_before)
        out = {"dry_run": body.dry_run, "ops": results, "counts": counts, "diff": diff, "skipped": runner.skipped,
               "created_tabs": runner.created_tabs, "created_categories": runner.created_categories,
               "categories": moved_categories}
        if body.dry_run or not diff and not runner.created_categories and not moved_categories:
            con.execute("ROLLBACK")
            out["changeset"] = None
            return out
        stored = {"counts": counts, "created_tabs": runner.created_tabs,
                  "created_categories": runner.created_categories,
                  "created_ids": [i for i, v in runner.before.items() if v is None],
                  "categories_before": {str(k): v for k, v in runner.categories_before.items()},
                  "categories_moved": len(moved_categories)}
        before = {str(i): v for i, v in runner.before.items() if v is not None}
        out["changeset"] = con.execute(
            "INSERT INTO changesets(user_id, key_name, summary, ops, result, before, created_at) VALUES(?,?,?,?,?,?,?)",
            (c.uid, key_name, clean_text(body.summary, 500),
             json.dumps([op.model_dump(exclude_defaults=True) for op in body.ops], ensure_ascii=False),
             json.dumps(stored), json.dumps(before, ensure_ascii=False), db.now())).lastrowid
        con.execute("DELETE FROM changesets WHERE user_id=? AND id NOT IN"
                    " (SELECT id FROM changesets WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                    (c.uid, c.uid, KEEP_CHANGESETS))
        con.execute("COMMIT")
        return out
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def category_diff(con, before: dict[int, dict], where: dict) -> list[dict]:
    out = []
    for cid, old in before.items():
        row = con.execute("SELECT col, position FROM categories WHERE id=?", (cid,)).fetchone()
        if row and (row["col"], row["position"]) != (old["col"], old["position"]):
            tab, name = where.get(cid, ("?", "?"))
            out.append({"id": cid, "tab": tab, "category": name, "column": row["col"] + 1,
                        "position": [old["position"] + 1, row["position"] + 1]})
    return sorted(out, key=lambda e: (e["tab"], e["column"], e["position"][1]))


def touched(cs) -> set[int]:
    result = json.loads(cs["result"])
    return {int(i) for i in json.loads(cs["before"])} | set(result.get("created_ids", []))


def undo_changeset(c: Ctx, cs_id: int, force: bool) -> dict:
    con = c.con
    cs = own(con, "changesets", cs_id, c.uid)
    if cs["undone_at"]:
        raise err(409, "already_undone")
    mine = touched(cs)
    later = [r for r in con.execute(
        "SELECT * FROM changesets WHERE user_id=? AND id>? AND undone_at IS NULL ORDER BY id", (c.uid, cs_id))
        if touched(r) & mine]
    if later and not force:
        # later changes would be overwritten too: the caller must ask for it explicitly
        raise err(409, "conflict")
    result, before = json.loads(cs["result"]), json.loads(cs["before"])
    categories = {r[0] for r in con.execute("SELECT id FROM categories WHERE user_id=?", (c.uid,))}
    restored, removed = 0, 0
    with db.tx(con):
        for part in db.chunks(result.get("created_ids", [])):
            removed += con.execute(f"DELETE FROM bookmarks WHERE user_id=? AND id IN ({db.marks(len(part))})",
                                   [c.uid, *part]).rowcount
        ids = []
        for key, row in before.items():
            bid = int(key)
            category_id = row["category_id"] if row["category_id"] in categories else None
            values = (category_id, row["title"], row["url"], row["host"], row["notes"], row["color"],
                      row["position"], row["created_at"], row["updated_at"])
            exists = con.execute("SELECT user_id FROM bookmarks WHERE id=?", (bid,)).fetchone()
            if exists and exists[0] == c.uid:
                con.execute("UPDATE bookmarks SET category_id=?, title=?, url=?, host=?, notes=?, color=?, position=?,"
                            " created_at=?, updated_at=? WHERE id=?", (*values, bid))
            else:
                if exists:  # the id was taken by someone else's new bookmark: restore under a new id
                    bid = con.execute("INSERT INTO bookmarks(user_id, category_id, title, url, host, notes, color,"
                                      " position, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (c.uid, *values)).lastrowid
                else:
                    con.execute("INSERT INTO bookmarks(id, user_id, category_id, title, url, host, notes, color,"
                                " position, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (bid, c.uid, *values))
            set_tags(con, bid, row["tags"])
            ids.append(bid)
            restored += 1
        db.reindex(con, ids)
        for cat_id in result.get("created_categories", []):
            if not con.execute("SELECT 1 FROM bookmarks WHERE category_id=?", (cat_id,)).fetchone():
                con.execute("DELETE FROM categories WHERE id=? AND user_id=?", (cat_id, c.uid))
        for tab_id in result.get("created_tabs", []):
            if not con.execute("SELECT 1 FROM categories WHERE tab_id=?", (tab_id,)).fetchone():
                con.execute("DELETE FROM tabs WHERE id=? AND user_id=?", (tab_id, c.uid))
        for cid, old in result.get("categories_before", {}).items():
            con.execute("UPDATE categories SET tab_id=?, col=?, position=? WHERE id=? AND user_id=?",
                        (old["tab_id"], old["col"], old["position"], int(cid), c.uid))
        con.execute("UPDATE changesets SET undone_at=? WHERE id=?", (db.now(), cs_id))
    return {"restored": restored, "removed": removed}


def changeset_list(c: Ctx, limit: int = 50) -> list[dict]:
    rows = c.con.execute("SELECT id, key_name, summary, result, created_at, undone_at FROM changesets"
                         " WHERE user_id=? ORDER BY id DESC LIMIT ?", (c.uid, limit)).fetchall()
    return [{"id": r["id"], "key": r["key_name"], "summary": r["summary"], "created_at": r["created_at"],
             "undone_at": r["undone_at"], "counts": json.loads(r["result"]).get("counts", {})} for r in rows]


@v1.post("/changes")
def post_changes(body: Changes, c: KeyCtx = Depends(key_ctx)):
    if not body.dry_run and not c.key["can_write"]:
        raise err(403, "read_only_key")
    return apply_changes(c, body, c.key["name"])


@v1.get("/changes")
def get_changes(limit: int = Query(50, ge=1, le=KEEP_CHANGESETS), c: KeyCtx = Depends(key_ctx)):
    return {"changes": changeset_list(c, limit)}


@v1.get("/changes/{cs_id}")
def get_change(cs_id: int, c: KeyCtx = Depends(key_ctx)):
    cs = own(c.con, "changesets", cs_id, c.uid)
    return {"id": cs["id"], "summary": cs["summary"], "created_at": cs["created_at"], "undone_at": cs["undone_at"],
            "ops": json.loads(cs["ops"]), "result": json.loads(cs["result"])}


@v1.post("/changes/{cs_id}/undo")
def post_undo(cs_id: int, force: bool = False, c: KeyCtx = Depends(writer)):
    return undo_changeset(c, cs_id, force)


# --- for the web UI: keys and the change history (session cookie) -----------------

class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    can_write: bool = False


@session_api.get("/keys")
def keys_list(c: Ctx = Depends(ctx)):
    rows = c.con.execute("SELECT id, name, prefix, can_write, created_at, last_used, last_ip FROM api_keys"
                         " WHERE user_id=? ORDER BY id", (c.uid,)).fetchall()
    return {"keys": [dict(r) | {"can_write": bool(r["can_write"])} for r in rows]}


@session_api.post("/keys")
def keys_create(body: KeyCreate, c: Ctx = Depends(ctx)):
    if c.con.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=?", (c.uid,)).fetchone()[0] >= MAX_KEYS:
        raise err(422, "limit_reached")
    key = KEY_PREFIX + security.new_token(32)
    kid = c.con.execute(
        "INSERT INTO api_keys(user_id, name, prefix, key_hash, can_write, created_at) VALUES(?,?,?,?,?,?)",
        (c.uid, clean_text(body.name, 100), key[:12], security.token_hash(key), int(body.can_write), db.now())).lastrowid
    return {"id": kid, "key": key}


@session_api.delete("/keys/{key_id}")
def keys_delete(key_id: int, c: Ctx = Depends(ctx)):
    c.con.execute("DELETE FROM api_keys WHERE id=? AND user_id=?", (key_id, c.uid))
    return {"ok": True}


@session_api.get("/changes")
def changes_list(c: Ctx = Depends(ctx)):
    return {"changes": changeset_list(c)}


@session_api.post("/changes/{cs_id}/undo")
def changes_undo(cs_id: int, force: bool = False, c: Ctx = Depends(ctx)):
    return undo_changeset(c, cs_id, force)
