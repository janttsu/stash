"""SQLite storage: schema, connections and small query helpers."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("STASH_DATA", BASE / "data"))
DB_PATH = DATA / "stash.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash     TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    totp_secret TEXT,
    totp_on     INTEGER NOT NULL DEFAULT 0,
    totp_last   INTEGER NOT NULL DEFAULT 0,
    settings    TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at    INTEGER
);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tabs (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    color          TEXT NOT NULL DEFAULT '',
    position       INTEGER NOT NULL DEFAULT 0,
    columns        INTEGER NOT NULL DEFAULT 3,
    share_token    TEXT UNIQUE,
    share_title    TEXT NOT NULL DEFAULT '',
    share_subtitle TEXT NOT NULL DEFAULT '',
    share_pw       TEXT
);
CREATE INDEX IF NOT EXISTS tabs_user ON tabs(user_id, position);
CREATE TABLE IF NOT EXISTS categories (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tab_id    INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    color     TEXT NOT NULL DEFAULT '',
    col       INTEGER NOT NULL DEFAULT 0,
    position  INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS categories_tab ON categories(tab_id, col, position);
-- category_id NULL = the bookmark lives in the Catalog
CREATE TABLE IF NOT EXISTS bookmarks (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,
    search      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_user ON bookmarks(user_id, created_at);
CREATE INDEX IF NOT EXISTS bookmarks_cat ON bookmarks(category_id, position);
CREATE INDEX IF NOT EXISTS bookmarks_url ON bookmarks(user_id, url);
CREATE INDEX IF NOT EXISTS bookmarks_host ON bookmarks(host);
CREATE TABLE IF NOT EXISTS bookmark_tags (
    bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (bookmark_id, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bookmark_tags_tag ON bookmark_tags(tag);
-- keys for the /api/v1 programming interface; only a hash of the key is stored
CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    prefix     TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    can_write  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_used  INTEGER,
    last_ip    TEXT NOT NULL DEFAULT ''
);
-- every change made through /api/v1/changes, with what the touched rows looked like before (for undo)
CREATE TABLE IF NOT EXISTS changesets (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_name   TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    ops        TEXT NOT NULL,
    result     TEXT NOT NULL,
    before     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    undone_at  INTEGER
);
CREATE INDEX IF NOT EXISTS changesets_user ON changesets(user_id, id);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None: autocommit, transactions are opened explicitly with tx()
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    con = connect()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
    finally:
        con.close()
    os.chmod(DB_PATH, 0o600)


@contextmanager
def tx(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def now() -> int:
    return int(time.time())


def chunks(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def marks(n: int) -> str:
    return ",".join("?" * n)


def get_config(con, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def reindex(con, ids) -> None:
    """Rebuild the casefolded search text (title, url, notes, tags) of the given bookmarks."""
    for part in chunks(ids):
        rows = con.execute(
            "SELECT b.id, b.title, b.url, b.notes,"
            " (SELECT group_concat(tag, ' ') FROM bookmark_tags WHERE bookmark_id=b.id) AS tags"
            f" FROM bookmarks b WHERE b.id IN ({marks(len(part))})",
            part,
        ).fetchall()
        con.executemany(
            "UPDATE bookmarks SET search=? WHERE id=?",
            [("\n".join((r["title"], r["url"], r["notes"], r["tags"] or "")).casefold(), r["id"]) for r in rows],
        )
