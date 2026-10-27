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
    created_at  INTEGER NOT NULL,
    rev         INTEGER NOT NULL DEFAULT 0,
    email       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    remember   INTEGER NOT NULL DEFAULT 1
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
    updated_at  INTEGER NOT NULL,
    dead        INTEGER NOT NULL DEFAULT 0,
    dead_streak INTEGER NOT NULL DEFAULT 0,
    checked_at  INTEGER,
    meta_at     INTEGER
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
-- browsing history sent by a device (stash-history-sync); one row per address and device,
-- visit counts are as of synced_at. Never exported, deleted with the account.
CREATE TABLE IF NOT EXISTS history (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    url_key     TEXT NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    visits      INTEGER NOT NULL DEFAULT 0,
    visits_30d  INTEGER NOT NULL DEFAULT 0,
    visits_90d  INTEGER NOT NULL DEFAULT 0,
    visits_365d INTEGER NOT NULL DEFAULT 0,
    first_visit INTEGER,
    last_visit  INTEGER,
    PRIMARY KEY (user_id, source, url)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_key ON history(user_id, url_key);
-- deleted bookmarks, kept for TRASH_DAYS so they can be restored; filled by a trigger, so every way of
-- deleting (one bookmark, many, a whole category or tab, an API key) ends up here
CREATE TABLE IF NOT EXISTS trash (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bookmark_id   INTEGER NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    host          TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    color         TEXT NOT NULL DEFAULT '',
    category_id   INTEGER,
    tab_name      TEXT,
    category_name TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    created_at    INTEGER NOT NULL,
    deleted_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trash_user ON trash(user_id, deleted_at);
-- names of categories being deleted, so bookmarks deleted along with them still know where they were
CREATE TABLE IF NOT EXISTS trash_place (
    category_id   INTEGER PRIMARY KEY,
    tab_name      TEXT,
    category_name TEXT
);
CREATE TABLE IF NOT EXISTS history_sources (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source    TEXT NOT NULL,
    browser   TEXT NOT NULL DEFAULT '',
    items     INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER,
    PRIMARY KEY (user_id, source)
);
-- project handovers written by AI assistants through MCP (app/handover.py): one row per project, and an
-- append-only version history. Separate from bookmarks: no changeset, no trash, no undo.
CREATE TABLE IF NOT EXISTS handovers (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    latest_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, name)
);
-- open browser tabs sent to Stash to be tidied later (app/tabs.py): a list per sending, and its tabs with a
-- state: open (still to look at), done (finished), saved (made a bookmark) or dropped (not worth keeping)
CREATE TABLE IF NOT EXISTS open_tab_lists (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS open_tabs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    list_id     INTEGER NOT NULL REFERENCES open_tab_lists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    url         TEXT NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'open',
    note        TEXT NOT NULL DEFAULT '',
    bookmark_id INTEGER,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS open_tabs_list ON open_tabs(list_id, position);
CREATE INDEX IF NOT EXISTS open_tabs_user ON open_tabs(user_id, state);
-- playlists from Spotify and YouTube (app/playlists.py): the connected account per service, every playlist
-- with its items as they are on the service, and local edits (added, removed, moved) waiting to be pushed
CREATE TABLE IF NOT EXISTS playlist_accounts (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service       TEXT NOT NULL,
    client_id     TEXT NOT NULL DEFAULT '',
    client_secret TEXT NOT NULL DEFAULT '',
    access_token  TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    expires_at    INTEGER NOT NULL DEFAULT 0,
    oauth_state   TEXT NOT NULL DEFAULT '',
    external_id   TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    synced_at     INTEGER,
    created_at    INTEGER NOT NULL,
    UNIQUE (user_id, service)
);
CREATE TABLE IF NOT EXISTS playlists (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service      TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    public       INTEGER NOT NULL DEFAULT 0,
    editable     INTEGER NOT NULL DEFAULT 1,
    owner        TEXT NOT NULL DEFAULT '',
    remote_count INTEGER NOT NULL DEFAULT 0,
    snapshot     TEXT NOT NULL DEFAULT '',
    synced_at    INTEGER,
    dirty        INTEGER NOT NULL DEFAULT 0,
    gone         INTEGER NOT NULL DEFAULT 0,
    local        INTEGER NOT NULL DEFAULT 0,   -- only in Stash (imported from Google Takeout): never fetched or pushed
    deleting     INTEGER NOT NULL DEFAULT 0,   -- marked to be deleted from the service at the next push or synchronise
    UNIQUE (user_id, service, external_id)
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id              INTEGER PRIMARY KEY,
    playlist_id     INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL DEFAULT 0,
    remote_position INTEGER,
    item_id         TEXT NOT NULL,
    remote_id       TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    artist          TEXT NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    url             TEXT NOT NULL DEFAULT '',
    added_at        INTEGER,
    state           TEXT NOT NULL DEFAULT 'synced',
    unavailable     INTEGER NOT NULL DEFAULT 0   -- deleted or private on the service; title is the last one seen
);
CREATE INDEX IF NOT EXISTS playlist_items_pl ON playlist_items(playlist_id, position);
-- subscriptions (app/subscriptions.py): YouTube channels, Spotify artists and followed playlists, and YouTube
-- playlists kept only in Stash (YouTube's API cannot list or save them). state: synced, added (to subscribe
-- on the next push), removed (to unsubscribe), local (only in Stash). tags and note are Stash's own.
CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    remote_id    TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    owner        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    item_count   INTEGER,
    subscribed_at INTEGER,
    last_activity INTEGER,
    activity_checked INTEGER,
    state        TEXT NOT NULL DEFAULT 'synced',
    gone         INTEGER NOT NULL DEFAULT 0,
    tags         TEXT NOT NULL DEFAULT '[]',
    note         TEXT NOT NULL DEFAULT '',
    synced_at    INTEGER,
    UNIQUE (user_id, service, kind, external_id)
);
CREATE INDEX IF NOT EXISTS subscriptions_user ON subscriptions(user_id, service, kind);
CREATE TABLE IF NOT EXISTS handover_versions (
    id          INTEGER PRIMARY KEY,
    handover_id INTEGER NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    ai          TEXT NOT NULL DEFAULT '',
    machine     TEXT NOT NULL DEFAULT '',
    session     TEXT NOT NULL DEFAULT '',
    git_commit  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE (handover_id, version)
);
"""


# users.rev goes up whenever something the user sees changes, whoever changed it (web UI, API key, background
# upkeep, server commands). Open pages watch it through /api/events and redraw themselves.
_BUMP = "UPDATE users SET rev=rev+1 WHERE id={who};"
REV_TRIGGERS = "".join(
    f"CREATE TRIGGER IF NOT EXISTS rev_{name} AFTER {event} ON {table} BEGIN {_BUMP.format(who=who)} END;\n"
    for name, event, table, who in (
        ("bm_ins", "INSERT", "bookmarks", "NEW.user_id"),
        ("bm_del", "DELETE", "bookmarks", "OLD.user_id"),
        # not search, the check times or the dead state: those change in the background without anything to show
        ("bm_upd", "UPDATE OF title, url, notes, color, category_id, position", "bookmarks", "NEW.user_id"),
        ("tag_ins", "INSERT", "bookmark_tags", "(SELECT user_id FROM bookmarks WHERE id=NEW.bookmark_id)"),
        ("tag_del", "DELETE", "bookmark_tags", "(SELECT user_id FROM bookmarks WHERE id=OLD.bookmark_id)"),
        ("tab_ins", "INSERT", "tabs", "NEW.user_id"),
        ("tab_upd", "UPDATE", "tabs", "NEW.user_id"),
        ("tab_del", "DELETE", "tabs", "OLD.user_id"),
        ("cat_ins", "INSERT", "categories", "NEW.user_id"),
        ("cat_upd", "UPDATE", "categories", "NEW.user_id"),
        ("cat_del", "DELETE", "categories", "OLD.user_id"),
        ("trash_del", "DELETE", "trash", "OLD.user_id"),
        # the newer pages: playlists, subscriptions, open tabs and handovers, whether changed from the page itself,
        # by an assistant over MCP, by a fetch from the service or by a scheduled job
        ("pl_ins", "INSERT", "playlists", "NEW.user_id"),
        ("pl_upd", "UPDATE OF name, description, public, remote_count, synced_at, dirty, gone, local", "playlists", "NEW.user_id"),
        ("pl_del", "DELETE", "playlists", "OLD.user_id"),
        ("pli_ins", "INSERT", "playlist_items", "(SELECT user_id FROM playlists WHERE id=NEW.playlist_id)"),
        ("pli_upd", "UPDATE", "playlist_items", "(SELECT user_id FROM playlists WHERE id=NEW.playlist_id)"),
        ("pli_del", "DELETE", "playlist_items", "(SELECT user_id FROM playlists WHERE id=OLD.playlist_id)"),
        ("sub_ins", "INSERT", "subscriptions", "NEW.user_id"),
        ("sub_upd", "UPDATE", "subscriptions", "NEW.user_id"),
        ("sub_del", "DELETE", "subscriptions", "OLD.user_id"),
        ("otl_ins", "INSERT", "open_tab_lists", "NEW.user_id"),
        ("otl_upd", "UPDATE", "open_tab_lists", "NEW.user_id"),
        ("otl_del", "DELETE", "open_tab_lists", "OLD.user_id"),
        ("ot_ins", "INSERT", "open_tabs", "NEW.user_id"),
        ("ot_upd", "UPDATE", "open_tabs", "NEW.user_id"),
        ("ot_del", "DELETE", "open_tabs", "OLD.user_id"),
        ("ho_ins", "INSERT", "handovers", "NEW.user_id"),
        ("ho_del", "DELETE", "handovers", "OLD.user_id"),
        ("hov_ins", "INSERT", "handover_versions", "(SELECT user_id FROM handovers WHERE id=NEW.handover_id)"),
    ))

TRASH_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trash_tab BEFORE DELETE ON tabs BEGIN
    INSERT OR REPLACE INTO trash_place SELECT id, OLD.name, name FROM categories WHERE tab_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trash_cat BEFORE DELETE ON categories BEGIN
    INSERT OR REPLACE INTO trash_place VALUES (OLD.id,
        COALESCE((SELECT name FROM tabs WHERE id=OLD.tab_id),
                 (SELECT tab_name FROM trash_place WHERE category_id=OLD.id)), OLD.name);
END;
-- not when the whole account goes: then its user row is already gone
CREATE TRIGGER IF NOT EXISTS trash_bm BEFORE DELETE ON bookmarks
WHEN EXISTS (SELECT 1 FROM users WHERE id=OLD.user_id) BEGIN
    INSERT INTO trash(user_id, bookmark_id, title, url, host, notes, color, category_id, tab_name, category_name,
                      tags, created_at, deleted_at)
    VALUES (OLD.user_id, OLD.id, OLD.title, OLD.url, OLD.host, OLD.notes, OLD.color, OLD.category_id,
        COALESCE((SELECT t.name FROM categories k JOIN tabs t ON t.id=k.tab_id WHERE k.id=OLD.category_id),
                 (SELECT tab_name FROM trash_place WHERE category_id=OLD.category_id)),
        COALESCE((SELECT name FROM categories WHERE id=OLD.category_id),
                 (SELECT category_name FROM trash_place WHERE category_id=OLD.category_id)),
        (SELECT json_group_array(tag) FROM bookmark_tags WHERE bookmark_id=OLD.id),
        OLD.created_at, CAST(strftime('%s', 'now') AS INTEGER));
END;
-- the same address saved again (or restored): it is no longer missing
CREATE TRIGGER IF NOT EXISTS trash_back AFTER INSERT ON bookmarks BEGIN
    DELETE FROM trash WHERE user_id=NEW.user_id AND url=NEW.url;
END;
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
        # columns added after the first release: add them to databases that predate them
        have = {r[1] for r in con.execute("PRAGMA table_info(bookmarks)")}
        for name, decl in (("dead", "INTEGER NOT NULL DEFAULT 0"), ("dead_streak", "INTEGER NOT NULL DEFAULT 0"),
                           ("checked_at", "INTEGER"), ("meta_at", "INTEGER")):
            if name not in have:
                con.execute(f"ALTER TABLE bookmarks ADD COLUMN {name} {decl}")
        if "rev" not in {r[1] for r in con.execute("PRAGMA table_info(users)")}:
            con.execute("ALTER TABLE users ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
        con.executescript(REV_TRIGGERS)
        con.executescript(TRASH_TRIGGERS)
        if "remember" not in {r[1] for r in con.execute("PRAGMA table_info(sessions)")}:
            con.execute("ALTER TABLE sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 1")
        if "email" not in {r[1] for r in con.execute("PRAGMA table_info(users)")}:
            con.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "unavailable" not in {r[1] for r in con.execute("PRAGMA table_info(playlist_items)")}:
            con.execute("ALTER TABLE playlist_items ADD COLUMN unavailable INTEGER NOT NULL DEFAULT 0")
        have = {r[1] for r in con.execute("PRAGMA table_info(playlists)")}
        if "local" not in have:
            con.execute("ALTER TABLE playlists ADD COLUMN local INTEGER NOT NULL DEFAULT 0")
        if "deleting" not in have:
            con.execute("ALTER TABLE playlists ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0")
        # one address, one account (case does not matter); accounts without an address are not compared
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users(email COLLATE NOCASE) WHERE email <> ''")
        # these indexes reference the columns added just above, so they come after the migration
        con.execute("CREATE INDEX IF NOT EXISTS bookmarks_checked ON bookmarks(checked_at)")
        con.execute("CREATE INDEX IF NOT EXISTS bookmarks_meta ON bookmarks(meta_at)")
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
