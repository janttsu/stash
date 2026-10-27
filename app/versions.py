"""Version history for each user: their own data as it was in any of the hourly backups of the last 30 days,
and restoring parts of it.

The backups (scripts/backup.sh) are copies of the whole database. A user never gets the file: Stash opens a
copy, counts and copies only that user's rows, and only from the areas they choose:

  bookmarks       tabs, categories, bookmarks and their tags (the Dashboard and the Catalog)
  playlists       the copies of Spotify and YouTube playlists (not the service connections)
  subscriptions   channels, artists and followed playlists with their tags and notes
  open_tabs       the lists of open tabs sent to Stash
  settings        the account's settings, among them the rules for the assistant

Restoring an area replaces that area's current rows with the old ones. Just before, the current state is saved
as a new backup, so a restore can itself be undone by restoring that version. Bookmarks that exist now but not
in the restored version go to the trash (30 days) rather than vanish. Project handovers are never overwritten,
so they are not part of this: every version of every handover is kept anyway.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import db
from .main import Ctx, ctx, err

AREAS = {
    # area: [(table, parent foreign key column, parent table)], parents first
    "bookmarks": [("tabs", None, None), ("categories", "tab_id", "tabs"), ("bookmarks", "category_id", "categories"),
                  ("bookmark_tags", "bookmark_id", "bookmarks")],
    "playlists": [("playlists", None, None), ("playlist_items", "playlist_id", "playlists")],
    "subscriptions": [("subscriptions", None, None)],
    "open_tabs": [("open_tab_lists", None, None), ("open_tabs", "list_id", "open_tab_lists")],
    "settings": [],
}
NAME = re.compile(r"^stash-(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})\.db\.zst$")
KEEP_OPEN = 1800        # seconds a decompressed copy is kept for further looks before it is deleted

session_api = APIRouter(prefix="/api/versions")


def backup_dir() -> Path:
    return Path(os.environ.get("STASH_BACKUP_DIR") or db.DATA / "backups")


def _time_of(name: str) -> int:
    m = NAME.match(name)
    return int(time.mktime(time.strptime("".join(m.groups()), "%Y%m%d%H%M"))) if m else 0


def versions() -> list[dict]:
    """The versions to choose from: every backup of the last 48 hours, then the newest of each older day."""
    files = sorted((p for p in backup_dir().glob("stash-*.db.zst") if NAME.match(p.name)), key=lambda p: p.name, reverse=True)
    out, days, recent = [], set(), time.time() - 48 * 3600
    for p in files:
        t = _time_of(p.name)
        day = p.name[6:16]
        if t < recent:
            if day in days:
                continue
            days.add(day)
        out.append({"name": p.name, "time": t, "size": p.stat().st_size})
    return out


def _open(name: str) -> Path:
    """The backup decompressed into data/tmp/versions (reused for a while), after checking the name."""
    if not NAME.match(name) or not (backup_dir() / name).is_file():
        raise err(404, "no_such_version")
    folder = db.DATA / "tmp" / "versions"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    now = time.time()
    for old in folder.glob("*.db"):
        if now - old.stat().st_mtime > KEEP_OPEN:
            old.unlink(missing_ok=True)
    path = folder / name.removesuffix(".zst")
    if not path.exists():
        part = path.with_suffix(".part")
        subprocess.run(["zstd", "-q", "-d", "-f", str(backup_dir() / name), "-o", str(part)], check=True, timeout=120)
        os.chmod(part, 0o600)
        part.rename(path)
    os.utime(path)
    return path


def _counts(con, schema: str, uid: int) -> dict:
    def n(sql: str) -> int:
        try:
            return con.execute(sql.replace("S.", f"{schema}."), (uid,)).fetchone()[0]
        except Exception:  # noqa: BLE001  (an older backup without that table)
            return 0
    return {
        "bookmarks": n("SELECT COUNT(*) FROM S.bookmarks WHERE user_id=?"),
        "tabs": n("SELECT COUNT(*) FROM S.tabs WHERE user_id=?"),
        "tags": n("SELECT COUNT(DISTINCT t.tag) FROM S.bookmark_tags t JOIN S.bookmarks b ON b.id=t.bookmark_id WHERE b.user_id=?"),
        "playlists": n("SELECT COUNT(*) FROM S.playlists WHERE user_id=?"),
        "playlist_items": n("SELECT COUNT(*) FROM S.playlist_items i JOIN S.playlists p ON p.id=i.playlist_id WHERE p.user_id=?"),
        "subscriptions": n("SELECT COUNT(*) FROM S.subscriptions WHERE user_id=?"),
        "open_tabs": n("SELECT COUNT(*) FROM S.open_tabs WHERE user_id=?"),
    }


def preview(c: Ctx, name: str) -> dict:
    path = _open(name)
    con = c.con
    con.execute("ATTACH DATABASE ? AS snap", (str(path),))
    try:
        then = _counts(con, "snap", c.uid)
    finally:
        con.execute("DETACH DATABASE snap")
    return {"name": name, "time": _time_of(name), "now": _counts(con, "main", c.uid), "then": then}


def _columns(con, schema: str, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def _safety_backup() -> str:
    script = db.BASE / "scripts" / "backup.sh"
    env = {**os.environ, "STASH_DATA": str(db.DATA)}
    r = subprocess.run([str(script)], cwd=db.BASE, env=env, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise err(500, "backup_failed: the current state could not be saved first, so nothing was restored")
    m = re.search(r"(stash-[\d-]+_\d{4}\.db\.zst)", r.stdout)
    return m.group(1) if m else ""


def restore(c: Ctx, name: str, areas: list[str]) -> dict:
    areas = [a for a in AREAS if a in set(areas)]
    if not areas:
        raise err(422, "no_areas")
    path = _open(name)
    saved = _safety_backup()
    con, uid = c.con, c.uid
    con.execute("ATTACH DATABASE ? AS snap", (str(path),))
    restored: dict[str, int] = {}
    try:
        snap_tables = {r[0] for r in con.execute("SELECT name FROM snap.sqlite_master WHERE type='table'")}
        with db.tx(con):
            for area in areas:
                if area == "settings":
                    row = con.execute("SELECT settings FROM snap.users WHERE id=?", (uid,)).fetchone()
                    if row:
                        con.execute("UPDATE users SET settings=? WHERE id=?", (row[0], uid))
                        restored["settings"] = 1
                    continue
                restored[area] = _restore_area(con, uid, area, snap_tables)
            if "bookmarks" in areas:
                db.reindex(con, [r[0] for r in con.execute("SELECT id FROM bookmarks WHERE user_id=?", (uid,))])
    finally:
        con.execute("DETACH DATABASE snap")
    return {"restored": restored, "from": name, "saved_before": saved}


def _restore_area(con, uid: int, area: str, snap_tables: set[str]) -> int:
    tables = AREAS[area]
    top = tables[0][0]
    # current rows out: deleting the top table cascades to its children (bookmarks in the Catalog have no
    # category, so they are deleted on their own; the trash trigger keeps them for 30 days)
    if area == "bookmarks":
        con.execute("DELETE FROM bookmarks WHERE user_id=? AND category_id IS NULL", (uid,))
    con.execute(f"DELETE FROM {top} WHERE user_id=?", (uid,))
    maps: dict[str, dict[int, int]] = {}
    count = 0
    for table, fk, parent in tables:
        if table not in snap_tables:
            continue
        cols = [col for col in _columns(con, "snap", table) if col in set(_columns(con, "main", table)) and col != "id"]
        has_id = "id" in _columns(con, "snap", table)
        if fk:
            rows = con.execute(
                f"SELECT {'s.id AS __id, ' if has_id else ''}{', '.join('s.' + x for x in cols)} FROM snap.{table} s"
                f" JOIN snap.{parent} p ON p.id=s.{fk} WHERE p.user_id=?"
                + (" UNION ALL SELECT " + ("s.id AS __id, " if has_id else "") + ", ".join("s." + x for x in cols)
                   + f" FROM snap.{table} s WHERE s.{fk} IS NULL AND s.user_id=?" if table == "bookmarks" else ""),
                (uid, uid) if table == "bookmarks" else (uid,)).fetchall()
        else:
            rows = con.execute(f"SELECT {'id AS __id, ' if has_id else ''}{', '.join(cols)} FROM snap.{table} WHERE user_id=?",
                               (uid,)).fetchall()
        mapping: dict[int, int] = {}
        for r in rows:
            values = {x: r[x] for x in cols}
            if fk and values.get(fk) is not None:
                values[fk] = maps[parent].get(values[fk])
                if values[fk] is None:
                    continue
            if table == "tabs" and values.get("share_token") and con.execute(
                    "SELECT 1 FROM tabs WHERE share_token=?", (values["share_token"],)).fetchone():
                values["share_token"] = None   # taken by someone else meanwhile: the share link is not restored
            cur = con.execute(f"INSERT INTO {table}({', '.join(values)}) VALUES({db.marks(len(values))})", list(values.values()))
            if has_id:
                mapping[r["__id"]] = cur.lastrowid
            count += 1
        maps[table] = mapping
    return count


# --- the web UI, with the session cookie ---------------------------------------------------------------------

class Restore(BaseModel):
    areas: list[str] = Field(min_length=1, max_length=len(AREAS))


@session_api.get("")
def web_versions(c: Ctx = Depends(ctx)):
    return {"versions": versions(), "areas": list(AREAS)}


@session_api.get("/{name}")
def web_preview(name: str, c: Ctx = Depends(ctx)):
    return preview(c, name)


@session_api.post("/{name}/restore")
def web_restore(name: str, body: Restore, c: Ctx = Depends(ctx)):
    return restore(c, name, body.areas)
