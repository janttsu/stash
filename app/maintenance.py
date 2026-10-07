"""Background upkeep: every so often Stash checks bookmarks by itself, so the user never has to ask.

Three jobs run on a slow loop, a small batch at a time so the load stays low:

- **dead links** — a page that answers 404/410 or whose domain no longer resolves is checked a few times, and
  once it has failed `DEAD_THRESHOLD` times in a row it gets the `dead-link` tag. The tag is removed again the
  moment the page answers normally. Pages that only fail behind a login, or time out, are never tagged: only a
  clear "gone" counts, so the mark stays trustworthy.
- **duplicates** — when two bookmarks share the same address, every copy after the first keeps the `duplicate`
  tag. The mark is kept in step with the bookmarks: delete one copy and the tag clears on its own.
- **titles and icons** — a bookmark whose title is empty or just its address is given the page's real title,
  and site icons are fetched again. A title the user has written something into is left alone, and a page that
  the server cannot reach from where it runs keeps whatever it has.

Everything here edits bookmarks directly (like the manual "find duplicates" and "dead links" tools), so it
does not fill the undo history. A user can switch the whole thing off in Settings.
"""
from __future__ import annotations

import asyncio
import json
import re

from . import db, net

DEAD_TAG = "dead-link"
DUPLICATE_TAG = "duplicate"

LOOP_SECONDS = 300           # one slow cycle every five minutes
DEAD_RECHECK = 7 * 86400     # re-check a link this often
META_RECHECK = 30 * 86400    # re-read a title / icon this often
DEAD_THRESHOLD = 3           # consecutive failures before the dead-link tag goes on
DEAD_BATCH = 20              # links checked per cycle
META_BATCH = 15              # titles / icons refreshed per cycle

# titles that a blocked, broken or placeholder page returns: never worth storing
_JUNK_TITLE = re.compile(
    r"^\W*(\d{3}\s*)?(error|forbidden|not found|page not found|access denied|unauthorized|bad gateway|"
    r"service unavailable|too many requests|down for maintenance|under maintenance|document moved|"
    r"object moved|moved permanently|redirecting|untitled|loading)\b"
    r"|^(just a moment|attention required|are you (a )?(robot|human)|one more step|security check|"
    r"request rejected|checking your browser|ddos-guard|captcha|please wait)", re.I)


def active_user_ids(con) -> list[int]:
    """Users who have not turned the upkeep off (it is on by default)."""
    uids = []
    for r in con.execute("SELECT id, settings FROM users"):
        try:
            on = json.loads(r["settings"]).get("auto_maintain", True)
        except (ValueError, TypeError):
            on = True
        if on:
            uids.append(r["id"])
    return uids


def looks_unnamed(title: str, url: str, host: str) -> bool:
    """A title that says nothing more than the address: safe to replace with the real page title."""
    t = title.strip().casefold().rstrip("/")
    if not t:
        return True
    u = url.casefold().rstrip("/")
    return t in (u, u.split("://", 1)[-1], host.casefold(), host.casefold().removeprefix("www.")) \
        or t.startswith(("http://", "https://", "www."))


def good_title(title: str) -> bool:
    title = title.strip()
    return bool(title) and not _JUNK_TITLE.match(title)


# --- duplicates ------------------------------------------------------------------

def reconcile_duplicates(con, uid: int) -> int:
    """Keep the `duplicate` tag on every bookmark whose address another, older one already has."""
    with db.tx(con):
        from .main import add_tags, ids_with_tag, remove_tags  # late: avoids an import cycle at startup

        groups: dict[str, list[int]] = {}
        for r in con.execute("SELECT id, url FROM bookmarks WHERE user_id=? ORDER BY created_at, id", (uid,)):
            groups.setdefault(r["url"], []).append(r["id"])
        should = {i for ids in groups.values() if len(ids) > 1 for i in ids[1:]}
        has = set(ids_with_tag(con, uid, DUPLICATE_TAG))
        add, drop = should - has, has - should
        if add:
            add_tags(con, list(add), [DUPLICATE_TAG])
        if drop:
            remove_tags(con, list(drop), [DUPLICATE_TAG])
        if add or drop:
            db.reindex(con, add | drop)
    return len(should)


# --- dead links ------------------------------------------------------------------

def due_dead(con, uids: list[int], limit: int) -> list[tuple[int, int, str, int, int]]:
    if not uids:
        return []
    cutoff = db.now() - DEAD_RECHECK
    marks = db.marks(len(uids))
    rows = con.execute(
        f"SELECT id, user_id, url, dead, dead_streak FROM bookmarks"
        f" WHERE user_id IN ({marks}) AND host<>'' AND (checked_at IS NULL OR checked_at<?)"
        " ORDER BY checked_at IS NOT NULL, checked_at LIMIT ?",
        [*uids, cutoff, limit]).fetchall()
    return [(r["id"], r["user_id"], r["url"], r["dead"], r["dead_streak"]) for r in rows]


async def check_dead_batch(con, client, uids: list[int], limit: int = DEAD_BATCH) -> dict:
    """Check the links that are due and keep the dead-link tag and the stored dead state in step."""
    from .main import add_tags, remove_tags  # late: avoids an import cycle at startup

    counts = {"checked": 0, "tagged": 0, "cleared": 0}
    for bid, uid, url, was_dead, streak in due_dead(con, uids, limit):
        if not url.lower().startswith(("http://", "https://")):
            con.execute("UPDATE bookmarks SET checked_at=? WHERE id=?", (db.now(), bid))
            continue
        dead = await net.is_dead(client, url)
        counts["checked"] += 1
        streak = streak + 1 if dead else 0
        now = db.now()
        with db.tx(con):
            # the bookmark may have been deleted or its address changed while the check ran
            row = con.execute("SELECT url, dead FROM bookmarks WHERE id=?", (bid,)).fetchone()
            if not row or row["url"] != url:
                continue
            if dead and streak >= DEAD_THRESHOLD and not row["dead"]:
                add_tags(con, [bid], [DEAD_TAG])
                con.execute("UPDATE bookmarks SET dead=1 WHERE id=?", (bid,))
                db.reindex(con, [bid])
                counts["tagged"] += 1
            elif not dead and row["dead"]:
                remove_tags(con, [bid], [DEAD_TAG])
                con.execute("UPDATE bookmarks SET dead=0 WHERE id=?", (bid,))
                db.reindex(con, [bid])
                counts["cleared"] += 1
            con.execute("UPDATE bookmarks SET dead_streak=?, checked_at=? WHERE id=?", (streak, now, bid))
    return counts


# --- titles and icons ------------------------------------------------------------

def due_meta(con, uids: list[int], limit: int) -> list[tuple[int, str, str, str]]:
    if not uids:
        return []
    cutoff = db.now() - META_RECHECK
    marks = db.marks(len(uids))
    rows = con.execute(
        f"SELECT id, url, title, host FROM bookmarks WHERE user_id IN ({marks})"
        " AND host<>'' AND (meta_at IS NULL OR meta_at<?)"
        " ORDER BY meta_at IS NOT NULL, meta_at LIMIT ?",
        [*uids, cutoff, limit]).fetchall()
    return [(r["id"], r["url"], r["title"], r["host"]) for r in rows]


async def refresh_meta_batch(con, client, uids: list[int], limit: int = META_BATCH) -> dict:
    """Give unnamed bookmarks their page's real title, and fetch their site icons again."""
    from .main import refresh_favicon  # late: avoids an import cycle at startup

    counts = {"checked": 0, "titled": 0, "icons_new": 0}
    icons: dict[str, str] = {}
    for bid, url, title, host in due_meta(con, uids, limit):
        if not url.lower().startswith(("http://", "https://")):
            con.execute("UPDATE bookmarks SET meta_at=? WHERE id=?", (db.now(), bid))
            continue
        counts["checked"] += 1
        if looks_unnamed(title, url, host):
            try:
                found = await net.page_title(client, url)
            except net.FetchError:
                found = ""
            if good_title(found):
                with db.tx(con):
                    row = con.execute("SELECT url, title FROM bookmarks WHERE id=?", (bid,)).fetchone()
                    # only if nothing changed meanwhile and the title is still unnamed
                    if row and row["url"] == url and looks_unnamed(row["title"], url, host) \
                            and found.strip()[:500] != row["title"]:
                        con.execute("UPDATE bookmarks SET title=? WHERE id=?", (found.strip()[:500], bid))
                        db.reindex(con, [bid])
                        counts["titled"] += 1
        if host not in icons:
            icons[host] = await refresh_favicon(client, host)
            if icons[host] == "new":
                counts["icons_new"] += 1
        con.execute("UPDATE bookmarks SET meta_at=? WHERE id=?", (db.now(), bid))
    if counts["icons_new"]:
        db.set_config(con, "favicon_rev", str(db.now()))
    return counts


# --- the loop --------------------------------------------------------------------

async def cycle(client) -> dict:
    con = db.connect()
    try:
        from .main import purge_trash

        purge_trash(con)
        uids = active_user_ids(con)
        for uid in uids:
            reconcile_duplicates(con, uid)
        dead = await check_dead_batch(con, client, uids)
        meta = await refresh_meta_batch(con, client, uids)
        return {"users": len(uids), "dead": dead, "meta": meta}
    finally:
        con.close()


async def run(client, *, stop: asyncio.Event | None = None) -> None:
    """Loop until cancelled. Every failure is swallowed so the loop keeps going."""
    import logging

    log = logging.getLogger("stash.maintenance")
    await asyncio.sleep(30)  # let the app settle after start-up
    while not (stop and stop.is_set()):
        try:
            await cycle(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  (a stray error must not kill the upkeep)
            log.exception("maintenance cycle failed")
        try:
            await asyncio.wait_for(stop.wait(), LOOP_SECONDS) if stop else await asyncio.sleep(LOOP_SECONDS)
        except asyncio.TimeoutError:
            pass
