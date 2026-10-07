"""Background upkeep: every so often Stash looks after titles and site icons by itself.

A slow loop works through a small batch every few minutes, so the load stays low. A bookmark whose title is
empty or just its address is given the page's real title, and site icons are fetched again. A title the user has
written something into is left alone, and a page that the server cannot reach keeps whatever it has. The loop also
empties the trash of what has been there longer than 30 days.

Titles are changed directly, so they do not fill the undo history. A user can switch this off in Settings.
"""
from __future__ import annotations

import asyncio
import json
import re

from . import db, net

LOOP_SECONDS = 300           # one slow cycle every five minutes
META_RECHECK = 30 * 86400    # re-read a title / icon this often
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
        meta = await refresh_meta_batch(con, client, uids)
        return {"users": len(uids), "meta": meta}
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
