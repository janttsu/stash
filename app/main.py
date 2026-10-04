"""Stash – a self-hosted bookmark manager (dashboard of tabs/categories + tagged catalog)."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, importer, net, security
from .security import limiter

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"
FAVICONS = db.DATA / "favicons"
ORIGIN = os.environ.get("STASH_ORIGIN", "http://localhost:8003")
COOKIE = "stash_session"
SECURE_COOKIE = os.environ.get("STASH_INSECURE_COOKIE") != "1"
SESSION_TTL = 90 * 86400
# STASH_REGISTRATION fixes who can create an account: invite (the default: an invitation link is needed), open
# (anyone) or closed. When it is unset, the administrator chooses in Settings → Users and registration and the
# choice is stored in the database.
REGISTRATION_MODES = ("open", "invite", "closed")
REGISTRATION_ENV = os.environ.get("STASH_REGISTRATION", "").strip().lower() or None
if REGISTRATION_ENV is not None and REGISTRATION_ENV not in REGISTRATION_MODES:
    raise SystemExit(f"STASH_REGISTRATION must be one of {', '.join(REGISTRATION_MODES)}, not {REGISTRATION_ENV!r}")

MAX_BOOKMARKS = 50_000
MAX_TABS = 200
MAX_CATEGORIES = 3000
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_TAGS = 30
COLORS = {"", "red", "orange", "yellow", "green", "teal", "blue", "indigo", "purple", "pink", "gray"}
SCHEMES = {"http", "https", "ftp", "mailto", "tel"}
DEFAULT_SETTINGS = {
    "theme": "auto", "lang": "", "tab_size": "m", "new_tab": True, "tooltips": True,
    "favicons": True, "auto_tag_catalog": False, "tag_order": "count",
}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    FAVICONS.mkdir(exist_ok=True)
    con = db.connect()
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
    con.close()
    app.state.http = net.make_client()
    yield
    await app.state.http.aclose()


app = FastAPI(title="Stash", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # A custom header cannot be sent cross-site without a CORS preflight (which we never grant),
    # so requiring it on every state-changing API call is the CSRF defence.
    # /api/v1 takes only API keys in the Authorization header, never the cookie, so it needs no CSRF header.
    if path.startswith("/api/") and not path.startswith("/api/v1/") \
            and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-stash") != "1":
        return JSONResponse({"detail": "csrf"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif not path.startswith("/favicon/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- helpers -----------------------------------------------------------------

def err(status: int, code: str) -> HTTPException:
    return HTTPException(status, code)


def get_db():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


class Ctx:
    def __init__(self, con, user):
        self.con = con
        self.user = user
        self.uid: int = user["id"]
        self.settings = {**DEFAULT_SETTINGS, **json.loads(user["settings"])}


def ctx(request: Request, con=Depends(get_db)) -> Ctx:
    token = request.cookies.get(COOKIE)
    if not token:
        raise err(401, "not_authenticated")
    th, t = security.token_hash(token), db.now()
    row = con.execute(
        "SELECT u.*, s.last_seen FROM sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.token_hash=? AND s.expires_at>?", (th, t)).fetchone()
    if not row:
        raise err(401, "not_authenticated")
    if t - row["last_seen"] > 3600:
        con.execute("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?", (t, t + SESSION_TTL, th))
    return Ctx(con, row)


def admin(c: Ctx = Depends(ctx)) -> Ctx:
    if not c.user["is_admin"]:
        raise err(403, "forbidden")
    return c


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def user_json(c: Ctx) -> dict:
    return {"id": c.uid, "username": c.user["username"], "is_admin": bool(c.user["is_admin"]),
            "totp_on": bool(c.user["totp_on"]), "settings": c.settings}


def start_session(con, response: Response, user_id: int) -> None:
    token, t = security.new_token(), db.now()
    con.execute("INSERT INTO sessions(token_hash, user_id, created_at, last_seen, expires_at) VALUES(?,?,?,?,?)",
                (security.token_hash(token), user_id, t, t, t + SESSION_TTL))
    response.set_cookie(COOKIE, token, max_age=400 * 86400, httponly=True, secure=SECURE_COOKIE,
                        samesite="lax", path="/")


def clean_text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def clean_url(url: str, guess: bool = True) -> str:
    url = url.strip()
    if not url or len(url) > 2000 or any(ch in url for ch in "\r\n\t"):
        raise err(422, "bad_url")
    if guess:
        if re.fullmatch(r"[^@\s:/]+@[^@\s/]+\.[^@\s/]+", url):
            url = "mailto:" + url
        elif re.fullmatch(r"\+?[\d\s().-]{5,}", url):
            url = "tel:" + re.sub(r"[^\d+]", "", url)
        elif re.match(r"^[\w.-]+:\d+(/|$)", url):
            url = "http://" + url
        elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url
    if url.split(":", 1)[0].lower() not in SCHEMES:
        raise err(422, "bad_url")
    return url


def url_host(url: str) -> str:
    try:
        parts = urlsplit(url)
        return (parts.hostname or "").lower() if parts.scheme in ("http", "https") else ""
    except ValueError:
        return ""


def norm_tag(tag: str) -> str:
    return " ".join(tag.replace(",", " ").split()).lower()[:50]


def norm_tags(tags) -> list[str]:
    out: list[str] = []
    for tag in tags:
        tag = norm_tag(tag)
        if tag and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


def check_color(color: str) -> str:
    if color not in COLORS:
        raise err(422, "bad_color")
    return color


def own(con, table: str, row_id: int, uid: int):
    row = con.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (row_id, uid)).fetchone()
    if not row:
        raise err(404, "not_found")
    return row


def tags_for(con, ids) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for part in db.chunks(ids):
        for r in con.execute(
                f"SELECT bookmark_id, tag FROM bookmark_tags WHERE bookmark_id IN ({db.marks(len(part))}) ORDER BY tag",
                part):
            out.setdefault(r["bookmark_id"], []).append(r["tag"])
    return out


def bookmark_json(row, tags) -> dict:
    return {"id": row["id"], "category_id": row["category_id"], "title": row["title"], "url": row["url"],
            "notes": row["notes"], "color": row["color"], "position": row["position"],
            "created_at": row["created_at"], "tags": tags}


def set_tags(con, bookmark_id: int, tags: list[str]) -> None:
    con.execute("DELETE FROM bookmark_tags WHERE bookmark_id=?", (bookmark_id,))
    con.executemany("INSERT OR IGNORE INTO bookmark_tags(bookmark_id, tag) VALUES(?,?)",
                    [(bookmark_id, t) for t in tags])


def add_tags(con, ids, tags) -> None:
    con.executemany("INSERT OR IGNORE INTO bookmark_tags(bookmark_id, tag) VALUES(?,?)",
                    [(i, t) for i in ids for t in tags])


def remove_tags(con, ids, tags) -> None:
    con.executemany("DELETE FROM bookmark_tags WHERE bookmark_id=? AND tag=?", [(i, t) for i in ids for t in tags])


def ids_with_tag(con, uid: int, tag: str) -> list[int]:
    return [r[0] for r in con.execute(
        "SELECT bt.bookmark_id FROM bookmark_tags bt JOIN bookmarks b ON b.id=bt.bookmark_id"
        " WHERE b.user_id=? AND bt.tag=?", (uid, tag))]


def next_position(con, category_id: Optional[int]) -> int:
    if category_id is None:
        return 0
    return con.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM bookmarks WHERE category_id=?",
                       (category_id,)).fetchone()[0]


def move_bookmarks(c: Ctx, ids: list[int], category_id: Optional[int]) -> None:
    """Move bookmarks to a category (appended in the given order) or to the Catalog (None)."""
    con = c.con
    if category_id is None:
        if c.settings["auto_tag_catalog"]:
            # Tab and category names become tags, so the bookmark stays findable in the Catalog
            for part in db.chunks(ids):
                rows = con.execute(
                    "SELECT b.id, cat.name AS cat, t.name AS tab FROM bookmarks b"
                    " JOIN categories cat ON cat.id=b.category_id JOIN tabs t ON t.id=cat.tab_id"
                    f" WHERE b.id IN ({db.marks(len(part))})", part).fetchall()
                for r in rows:
                    add_tags(con, [r["id"]], norm_tags([r["tab"], r["cat"]]))
        for part in db.chunks(ids):
            con.execute(f"UPDATE bookmarks SET category_id=NULL, position=0, updated_at=?"
                        f" WHERE id IN ({db.marks(len(part))})", [db.now(), *part])
        db.reindex(con, ids)
        return
    pos = next_position(con, category_id)
    con.executemany("UPDATE bookmarks SET category_id=?, position=?, updated_at=? WHERE id=?",
                    [(category_id, pos + i, db.now(), b) for i, b in enumerate(ids)])


def new_category(con, uid: int, tab_id: int, name: str, col: Optional[int] = None) -> int:
    if con.execute("SELECT COUNT(*) FROM categories WHERE user_id=?", (uid,)).fetchone()[0] >= MAX_CATEGORIES:
        raise err(422, "limit_reached")
    columns = con.execute("SELECT columns FROM tabs WHERE id=?", (tab_id,)).fetchone()[0]
    counts = {i: 0 for i in range(columns)}
    for r in con.execute("SELECT MIN(col, ?) AS c, COUNT(*) AS n FROM categories WHERE tab_id=? GROUP BY 1",
                         (columns - 1, tab_id)):
        counts[r["c"]] = r["n"]
    if col is None or not 0 <= col < columns:
        col = min(counts, key=lambda i: (counts[i], i))
    pos = con.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM categories WHERE tab_id=? AND col=?",
                      (tab_id, col)).fetchone()[0]
    return con.execute("INSERT INTO categories(user_id, tab_id, name, col, position) VALUES(?,?,?,?,?)",
                       (uid, tab_id, name, col, pos)).lastrowid


def new_tab(con, uid: int, name: str) -> int:
    if con.execute("SELECT COUNT(*) FROM tabs WHERE user_id=?", (uid,)).fetchone()[0] >= MAX_TABS:
        raise err(422, "limit_reached")
    pos = con.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tabs WHERE user_id=?", (uid,)).fetchone()[0]
    return con.execute("INSERT INTO tabs(user_id, name, position) VALUES(?,?,?)", (uid, name, pos)).lastrowid


def bookmark_count(con, uid: int) -> int:
    return con.execute("SELECT COUNT(*) FROM bookmarks WHERE user_id=?", (uid,)).fetchone()[0]


def like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Filter(BaseModel):
    scope: Literal["all", "dashboard", "catalog"] = "all"
    tags: list[str] = []
    q: str = ""
    mode: Literal["exact", "all", "any"] = "exact"
    untagged: bool = False


def filter_sql(uid: int, f: Filter) -> tuple[str, list]:
    where, params = ["b.user_id=?"], [uid]
    if f.scope == "catalog":
        where.append("b.category_id IS NULL")
    elif f.scope == "dashboard":
        where.append("b.category_id IS NOT NULL")
    for tag in f.tags[:20]:
        where.append("EXISTS(SELECT 1 FROM bookmark_tags x WHERE x.bookmark_id=b.id AND x.tag=?)")
        params.append(tag)
    if f.untagged:
        where.append("NOT EXISTS(SELECT 1 FROM bookmark_tags x WHERE x.bookmark_id=b.id)")
    q = " ".join(f.q.split()).casefold()
    if q:
        terms = [q] if f.mode == "exact" else q.split()[:20]
        joiner = " OR " if f.mode == "any" else " AND "
        where.append("(" + joiner.join(["b.search LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
        params += [f"%{like_escape(t)}%" for t in terms]
    return " AND ".join(where), params


def query_filter(scope: Literal["all", "dashboard", "catalog"] = "all", tags: str = "", q: str = "",
                 mode: Literal["exact", "all", "any"] = "exact", untagged: bool = False) -> Filter:
    return Filter(scope=scope, tags=[t for t in tags.split(",") if t], q=q[:300], mode=mode, untagged=untagged)


# --- pages -------------------------------------------------------------------

@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def page_index():
    return FileResponse(STATIC / "index.html")


@app.api_route("/add", methods=["GET", "HEAD"], include_in_schema=False)
def page_add():
    return FileResponse(STATIC / "add.html")


@app.api_route("/share/{token}", methods=["GET", "HEAD"], include_in_schema=False)
def page_share(token: str):
    return FileResponse(STATIC / "share.html")


@app.get("/favicon.ico", include_in_schema=False)
def site_icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/extension.zip", include_in_schema=False)
def extension_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        root = BASE / "extension"
        for f in sorted(root.rglob("*")):  # includes _locales/<lang>/messages.json
            if f.is_file() and f.name != "config.js":
                z.write(f, f"stash-extension/{f.relative_to(root).as_posix()}")
        z.writestr("stash-extension/config.js", f"const STASH_ORIGIN = {json.dumps(ORIGIN)};\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="stash-extension.zip"'})


# --- auth --------------------------------------------------------------------

FIRST_TAB = {"en": ("Home", "Favorites"), "fi": ("Koti", "Suosikit"), "sv": ("Hem", "Favoriter")}


class Register(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=200)
    invite: str = ""
    lang: str = "en"


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)
    totp: str = ""


def registration_mode(con) -> str:
    """Who can create an account: the STASH_REGISTRATION variable wins, otherwise the administrator's setting."""
    return REGISTRATION_ENV or db.get_config(con, "registration", "invite")


@app.get("/api/public")
def public_config(con=Depends(get_db)):
    has_users = con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    return {"registration": registration_mode(con), "first_user": not has_users}


@app.post("/api/register")
def register(body: Register, request: Request, response: Response, con=Depends(get_db)):
    ip_key = f"reg:{client_ip(request)}"
    if limiter.blocked(ip_key, 5, 3600):
        raise err(429, "too_many_attempts")
    username = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise err(422, "bad_username")
    pw_hash = security.hash_password(body.password)
    with db.tx(con):
        # The first account becomes the administrator. It goes through the same gate as everyone else
        # (by default an invitation from `python -m app.cli invite`), so a stranger cannot claim a fresh install.
        first = con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        mode = registration_mode(con)
        if mode == "closed":
            raise err(403, "registration_closed")
        if mode == "invite":
            if not con.execute("SELECT 1 FROM invites WHERE code=? AND used_by IS NULL", (body.invite,)).fetchone():
                raise err(403, "bad_invite")
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise err(409, "username_taken")
        uid = con.execute("INSERT INTO users(username, pw_hash, is_admin, created_at) VALUES(?,?,?,?)",
                          (username, pw_hash, int(first), db.now())).lastrowid
        if mode == "invite":
            con.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?", (uid, db.now(), body.invite))
        tab_name, cat_name = FIRST_TAB.get(body.lang, FIRST_TAB["en"])
        tab_id = new_tab(con, uid, tab_name)
        new_category(con, uid, tab_id, cat_name)
        start_session(con, response, uid)
    limiter.hit(ip_key)
    return {"ok": True}


@app.post("/api/login")
def login(body: Login, request: Request, response: Response, con=Depends(get_db)):
    keys = (f"login:{client_ip(request)}", f"login-user:{body.username.strip().lower()}")
    if limiter.blocked(keys[0], 20, 900) or limiter.blocked(keys[1], 10, 900):
        raise err(429, "too_many_attempts")
    user = con.execute("SELECT * FROM users WHERE username=?", (body.username.strip(),)).fetchone()
    # verify against a dummy hash for unknown users so both paths take the same time
    ok = security.verify_password(body.password, user["pw_hash"] if user else DUMMY_HASH)
    if not user or not ok:
        for key in keys:
            limiter.hit(key)
        raise err(401, "invalid_credentials")
    if user["totp_on"]:
        if not body.totp:
            raise err(401, "totp_required")
        counter = security.verify_totp(user["totp_secret"], body.totp, user["totp_last"])
        if counter is None:
            for key in keys:
                limiter.hit(key)
            raise err(401, "bad_totp")
        con.execute("UPDATE users SET totp_last=? WHERE id=?", (counter, user["id"]))
    start_session(con, response, user["id"])
    return {"ok": True}


DUMMY_HASH = security.hash_password(security.new_token())


@app.post("/api/logout")
def logout(request: Request, response: Response, con=Depends(get_db)):
    token = request.cookies.get(COOKIE)
    if token:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (security.token_hash(token),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(c: Ctx = Depends(ctx)):
    return user_json(c)


class SettingsPatch(BaseModel):
    theme: Optional[Literal["auto", "light", "dark"]] = None
    lang: Optional[Literal["", "fi", "en", "sv"]] = None
    tab_size: Optional[Literal["s", "m", "l"]] = None
    new_tab: Optional[bool] = None
    tooltips: Optional[bool] = None
    favicons: Optional[bool] = None
    auto_tag_catalog: Optional[bool] = None
    tag_order: Optional[Literal["count", "alpha"]] = None


@app.patch("/api/settings")
def patch_settings(body: SettingsPatch, c: Ctx = Depends(ctx)):
    c.settings.update(body.model_dump(exclude_none=True))
    c.con.execute("UPDATE users SET settings=? WHERE id=?", (json.dumps(c.settings), c.uid))
    return c.settings


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=8, max_length=200)


def require_password(c: Ctx, password: str) -> None:
    key = f"pw:{c.uid}"
    if limiter.blocked(key, 10, 900):
        raise err(429, "too_many_attempts")
    if not security.verify_password(password, c.user["pw_hash"]):
        limiter.hit(key)
        raise err(403, "wrong_password")


@app.post("/api/account/password")
def change_password(body: PasswordChange, request: Request, c: Ctx = Depends(ctx)):
    require_password(c, body.current)
    keep = security.token_hash(request.cookies.get(COOKIE, ""))
    with db.tx(c.con):
        c.con.execute("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(body.new), c.uid))
        c.con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (c.uid, keep))
    return {"ok": True}


@app.post("/api/account/logout-all")
def logout_all(request: Request, c: Ctx = Depends(ctx)):
    keep = security.token_hash(request.cookies.get(COOKIE, ""))
    c.con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (c.uid, keep))
    return {"ok": True}


class PasswordOnly(BaseModel):
    password: str = Field(max_length=200)


class Code(BaseModel):
    code: str = Field(max_length=20)


@app.post("/api/account/totp/setup")
def totp_setup(c: Ctx = Depends(ctx)):
    import segno
    if c.user["totp_on"]:
        raise err(409, "totp_already_on")
    secret = security.new_totp_secret()
    c.con.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret, c.uid))
    uri = security.totp_uri(secret, c.user["username"])
    return {"secret": secret, "uri": uri, "qr": segno.make(uri, error="m").svg_data_uri(scale=5, border=2)}


@app.post("/api/account/totp/enable")
def totp_enable(body: Code, c: Ctx = Depends(ctx)):
    if c.user["totp_on"] or not c.user["totp_secret"]:
        raise err(409, "totp_not_pending")
    counter = security.verify_totp(c.user["totp_secret"], body.code)
    if counter is None:
        raise err(422, "bad_totp")
    c.con.execute("UPDATE users SET totp_on=1, totp_last=? WHERE id=?", (counter, c.uid))
    return {"ok": True}


@app.post("/api/account/totp/disable")
def totp_disable(body: PasswordOnly, c: Ctx = Depends(ctx)):
    require_password(c, body.password)
    c.con.execute("UPDATE users SET totp_on=0, totp_secret=NULL, totp_last=0 WHERE id=?", (c.uid,))
    return {"ok": True}


@app.post("/api/account/delete")
def delete_account(body: PasswordOnly, response: Response, c: Ctx = Depends(ctx)):
    require_password(c, body.password)
    if c.user["is_admin"] and c.con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0] == 1 \
            and c.con.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 1:
        raise err(409, "last_admin")
    c.con.execute("DELETE FROM users WHERE id=?", (c.uid,))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# --- admin -------------------------------------------------------------------

@app.get("/api/admin")
def admin_overview(c: Ctx = Depends(admin)):
    users = [dict(r) for r in c.con.execute(
        "SELECT u.id, u.username, u.is_admin, u.totp_on, u.created_at,"
        " (SELECT COUNT(*) FROM bookmarks b WHERE b.user_id=u.id) AS bookmarks,"
        " (SELECT MAX(last_seen) FROM sessions s WHERE s.user_id=u.id) AS last_seen"
        " FROM users u ORDER BY u.id")]
    invites = [dict(r) for r in c.con.execute(
        "SELECT i.code, i.created_at, i.used_at, u.username AS used_by FROM invites i"
        " LEFT JOIN users u ON u.id=i.used_by ORDER BY i.created_at DESC LIMIT 200")]
    return {"registration": registration_mode(c.con), "registration_fixed": REGISTRATION_ENV is not None,
            "users": users, "invites": invites}


class AdminConfig(BaseModel):
    registration: Literal["open", "invite", "closed"]


@app.put("/api/admin/config")
def admin_config(body: AdminConfig, c: Ctx = Depends(admin)):
    if REGISTRATION_ENV is not None:
        raise err(409, "registration_fixed")
    db.set_config(c.con, "registration", body.registration)
    return {"ok": True}


@app.post("/api/admin/invites")
def admin_invite(c: Ctx = Depends(admin)):
    code = security.new_token(12)
    c.con.execute("INSERT INTO invites(code, created_by, created_at) VALUES(?,?,?)", (code, c.uid, db.now()))
    return {"code": code}


@app.delete("/api/admin/invites/{code}")
def admin_invite_delete(code: str, c: Ctx = Depends(admin)):
    c.con.execute("DELETE FROM invites WHERE code=? AND used_by IS NULL", (code,))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_user_delete(user_id: int, c: Ctx = Depends(admin)):
    if user_id == c.uid:
        raise err(409, "cannot_delete_self")
    c.con.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_user_reset(user_id: int, c: Ctx = Depends(admin)):
    password = security.new_token(9)
    with db.tx(c.con):
        cur = c.con.execute("UPDATE users SET pw_hash=?, totp_on=0, totp_secret=NULL WHERE id=?",
                            (security.hash_password(password), user_id))
        if not cur.rowcount:
            raise err(404, "not_found")
        c.con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"password": password}


# --- dashboard: tabs & categories ---------------------------------------------

def tab_json(r) -> dict:
    share = None
    if r["share_token"]:
        share = {"token": r["share_token"], "title": r["share_title"], "subtitle": r["share_subtitle"],
                 "has_password": bool(r["share_pw"])}
    return {"id": r["id"], "name": r["name"], "color": r["color"], "position": r["position"],
            "columns": r["columns"], "share": share}


@app.get("/api/dashboard")
def dashboard(c: Ctx = Depends(ctx)):
    con = c.con
    tabs = [tab_json(r) for r in con.execute("SELECT * FROM tabs WHERE user_id=? ORDER BY position, id", (c.uid,))]
    cats = [dict(r) for r in con.execute(
        "SELECT id, tab_id, name, color, col, position, collapsed FROM categories WHERE user_id=?"
        " ORDER BY col, position, id", (c.uid,))]
    rows = con.execute("SELECT * FROM bookmarks WHERE user_id=? AND category_id IS NOT NULL"
                       " ORDER BY position, id", (c.uid,)).fetchall()
    tags: dict[int, list[str]] = {}
    for r in con.execute("SELECT bt.bookmark_id, bt.tag FROM bookmark_tags bt JOIN bookmarks b ON b.id=bt.bookmark_id"
                         " WHERE b.user_id=? AND b.category_id IS NOT NULL ORDER BY bt.tag", (c.uid,)):
        tags.setdefault(r["bookmark_id"], []).append(r["tag"])
    return {"tabs": tabs, "categories": cats, "bookmarks": [bookmark_json(r, tags.get(r["id"], [])) for r in rows]}


class TabCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TabPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    color: Optional[str] = None
    columns: Optional[int] = Field(None, ge=1, le=5)


@app.post("/api/tabs")
def create_tab(body: TabCreate, c: Ctx = Depends(ctx)):
    name = clean_text(body.name, 100) or "Tab"
    with db.tx(c.con):
        return {"id": new_tab(c.con, c.uid, name)}


@app.patch("/api/tabs/{tab_id}")
def patch_tab(tab_id: int, body: TabPatch, c: Ctx = Depends(ctx)):
    own(c.con, "tabs", tab_id, c.uid)
    if body.name is not None:
        c.con.execute("UPDATE tabs SET name=? WHERE id=?", (clean_text(body.name, 100) or "Tab", tab_id))
    if body.color is not None:
        c.con.execute("UPDATE tabs SET color=? WHERE id=?", (check_color(body.color), tab_id))
    if body.columns is not None:
        c.con.execute("UPDATE tabs SET columns=? WHERE id=?", (body.columns, tab_id))
    return {"ok": True}


def catalog_or_delete(c: Ctx, category_ids: list[int], bookmarks: str) -> None:
    if bookmarks == "catalog" and category_ids:
        ids = [r[0] for r in c.con.execute(
            f"SELECT id FROM bookmarks WHERE category_id IN ({db.marks(len(category_ids))})", category_ids)]
        move_bookmarks(c, ids, None)


@app.delete("/api/tabs/{tab_id}")
def delete_tab(tab_id: int, bookmarks: Literal["delete", "catalog"] = "delete", c: Ctx = Depends(ctx)):
    own(c.con, "tabs", tab_id, c.uid)
    with db.tx(c.con):
        cats = [r[0] for r in c.con.execute("SELECT id FROM categories WHERE tab_id=?", (tab_id,))]
        catalog_or_delete(c, cats, bookmarks)
        c.con.execute("DELETE FROM tabs WHERE id=?", (tab_id,))
    return {"ok": True}


class Ids(BaseModel):
    ids: list[int] = Field(max_length=5000)


@app.post("/api/tabs/order")
def order_tabs(body: Ids, c: Ctx = Depends(ctx)):
    with db.tx(c.con):
        c.con.executemany("UPDATE tabs SET position=? WHERE id=? AND user_id=?",
                          [(i, tab_id, c.uid) for i, tab_id in enumerate(body.ids)])
    return {"ok": True}


class Layout(BaseModel):
    columns: list[list[int]] = Field(max_length=5)


@app.post("/api/tabs/{tab_id}/layout")
def tab_layout(tab_id: int, body: Layout, c: Ctx = Depends(ctx)):
    own(c.con, "tabs", tab_id, c.uid)
    with db.tx(c.con):
        c.con.executemany("UPDATE categories SET tab_id=?, col=?, position=? WHERE id=? AND user_id=?",
                          [(tab_id, col, pos, cat_id, c.uid)
                           for col, ids in enumerate(body.columns) for pos, cat_id in enumerate(ids)])
    return {"ok": True}


@app.post("/api/tabs/{tab_id}/sort")
def sort_categories(tab_id: int, c: Ctx = Depends(ctx)):
    """Alphabetical order, read row by row across the columns."""
    tab = own(c.con, "tabs", tab_id, c.uid)
    with db.tx(c.con):
        cats = c.con.execute("SELECT id, name FROM categories WHERE tab_id=?", (tab_id,)).fetchall()
        cats.sort(key=lambda r: r["name"].casefold())
        c.con.executemany("UPDATE categories SET col=?, position=? WHERE id=?",
                          [(i % tab["columns"], i // tab["columns"], r["id"]) for i, r in enumerate(cats)])
    return {"ok": True}


class Collapse(BaseModel):
    collapsed: bool


@app.post("/api/tabs/{tab_id}/collapse")
def collapse_all(tab_id: int, body: Collapse, c: Ctx = Depends(ctx)):
    c.con.execute("UPDATE categories SET collapsed=? WHERE tab_id=? AND user_id=?",
                  (int(body.collapsed), tab_id, c.uid))
    return {"ok": True}


class Share(BaseModel):
    title: str = Field("", max_length=200)
    subtitle: str = Field("", max_length=500)
    password: Optional[str] = Field(None, max_length=200)  # None keeps the current one, "" removes it


@app.put("/api/tabs/{tab_id}/share")
def share_tab(tab_id: int, body: Share, c: Ctx = Depends(ctx)):
    tab = own(c.con, "tabs", tab_id, c.uid)
    token = tab["share_token"] or security.new_token(12)
    pw = tab["share_pw"]
    if body.password is not None:
        pw = security.hash_password(body.password) if body.password else None
    c.con.execute("UPDATE tabs SET share_token=?, share_title=?, share_subtitle=?, share_pw=? WHERE id=?",
                  (token, clean_text(body.title, 200), clean_text(body.subtitle, 500), pw, tab_id))
    return {"token": token}


@app.delete("/api/tabs/{tab_id}/share")
def unshare_tab(tab_id: int, c: Ctx = Depends(ctx)):
    c.con.execute("UPDATE tabs SET share_token=NULL, share_pw=NULL WHERE id=? AND user_id=?", (tab_id, c.uid))
    return {"ok": True}


class SharePassword(BaseModel):
    password: str = Field("", max_length=200)


@app.post("/api/share/{token}")
def shared_tab(token: str, body: SharePassword, request: Request, con=Depends(get_db)):
    tab = con.execute("SELECT * FROM tabs WHERE share_token=?", (token,)).fetchone()
    if not tab:
        raise err(404, "not_found")
    if tab["share_pw"]:
        key = f"share:{client_ip(request)}:{tab['id']}"
        if limiter.blocked(key, 10, 900):
            raise err(429, "too_many_attempts")
        if not body.password:
            raise err(401, "password_required")
        if not security.verify_password(body.password, tab["share_pw"]):
            limiter.hit(key)
            raise err(401, "wrong_password")
    cats = [dict(r) for r in con.execute(
        "SELECT id, name, color, col, position FROM categories WHERE tab_id=? ORDER BY col, position, id",
        (tab["id"],))]
    bookmarks = [dict(r) for r in con.execute(
        "SELECT b.category_id, b.title, b.url, b.color FROM bookmarks b JOIN categories c ON c.id=b.category_id"
        " WHERE c.tab_id=? ORDER BY b.position, b.id", (tab["id"],))]
    return {"title": tab["share_title"] or tab["name"], "subtitle": tab["share_subtitle"],
            "columns": tab["columns"], "categories": cats, "bookmarks": bookmarks}


class CategoryCreate(BaseModel):
    tab_id: int
    name: str = Field(min_length=1, max_length=100)
    col: Optional[int] = None


class CategoryPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    color: Optional[str] = None
    collapsed: Optional[bool] = None
    tab_id: Optional[int] = None


@app.post("/api/categories")
def create_category(body: CategoryCreate, c: Ctx = Depends(ctx)):
    own(c.con, "tabs", body.tab_id, c.uid)
    with db.tx(c.con):
        return {"id": new_category(c.con, c.uid, body.tab_id, clean_text(body.name, 100) or "?", body.col)}


@app.patch("/api/categories/{cat_id}")
def patch_category(cat_id: int, body: CategoryPatch, c: Ctx = Depends(ctx)):
    cat = own(c.con, "categories", cat_id, c.uid)
    con = c.con
    with db.tx(con):
        if body.name is not None:
            con.execute("UPDATE categories SET name=? WHERE id=?", (clean_text(body.name, 100) or "?", cat_id))
        if body.color is not None:
            con.execute("UPDATE categories SET color=? WHERE id=?", (check_color(body.color), cat_id))
        if body.collapsed is not None:
            con.execute("UPDATE categories SET collapsed=? WHERE id=?", (int(body.collapsed), cat_id))
        if body.tab_id is not None and body.tab_id != cat["tab_id"]:
            own(con, "tabs", body.tab_id, c.uid)
            pos = con.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM categories WHERE tab_id=? AND col=0",
                              (body.tab_id,)).fetchone()[0]
            con.execute("UPDATE categories SET tab_id=?, col=0, position=? WHERE id=?", (body.tab_id, pos, cat_id))
    return {"ok": True}


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, bookmarks: Literal["delete", "catalog"] = "delete", c: Ctx = Depends(ctx)):
    own(c.con, "categories", cat_id, c.uid)
    with db.tx(c.con):
        catalog_or_delete(c, [cat_id], bookmarks)
        c.con.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    return {"ok": True}


@app.post("/api/categories/{cat_id}/order")
def order_bookmarks(cat_id: int, body: Ids, c: Ctx = Depends(ctx)):
    """Set the full bookmark order of a category; bookmarks dragged in from elsewhere are moved here."""
    own(c.con, "categories", cat_id, c.uid)
    with db.tx(c.con):
        c.con.executemany("UPDATE bookmarks SET category_id=?, position=? WHERE id=? AND user_id=?",
                          [(cat_id, i, b, c.uid) for i, b in enumerate(body.ids)])
    return {"ok": True}


@app.post("/api/categories/{cat_id}/sort")
def sort_bookmarks(cat_id: int, c: Ctx = Depends(ctx)):
    own(c.con, "categories", cat_id, c.uid)
    with db.tx(c.con):
        rows = c.con.execute("SELECT id, title FROM bookmarks WHERE category_id=?", (cat_id,)).fetchall()
        rows.sort(key=lambda r: r["title"].casefold())
        c.con.executemany("UPDATE bookmarks SET position=? WHERE id=?", [(i, r["id"]) for i, r in enumerate(rows)])
    return {"ok": True}


# --- bookmarks ---------------------------------------------------------------

class BookmarkCreate(BaseModel):
    url: str
    title: str = Field("", max_length=2000)
    notes: str = Field("", max_length=5000)
    tags: list[str] = Field([], max_length=100)
    category_id: Optional[int] = None
    color: str = ""


class BookmarkPatch(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = Field(None, max_length=2000)
    notes: Optional[str] = Field(None, max_length=5000)
    tags: Optional[list[str]] = Field(None, max_length=100)
    category_id: Optional[int] = None
    color: Optional[str] = None


def insert_bookmark(con, uid: int, url: str, title: str, notes: str, tags: list[str],
                    category_id: Optional[int], position: int, created_at: int, color: str = "") -> int:
    bid = con.execute(
        "INSERT INTO bookmarks(user_id, category_id, title, url, host, notes, color, position, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uid, category_id, title, url, url_host(url), notes, color, position, created_at, db.now())).lastrowid
    if tags:
        add_tags(con, [bid], tags)
    return bid


@app.post("/api/bookmarks")
def create_bookmark(body: BookmarkCreate, c: Ctx = Depends(ctx)):
    con = c.con
    url = clean_url(body.url)
    if body.category_id is not None:
        own(con, "categories", body.category_id, c.uid)
    if bookmark_count(con, c.uid) >= MAX_BOOKMARKS:
        raise err(422, "limit_reached")
    with db.tx(con):
        bid = insert_bookmark(con, c.uid, url, clean_text(body.title, 500) or url, body.notes.strip(),
                              norm_tags(body.tags), body.category_id, next_position(con, body.category_id),
                              db.now(), check_color(body.color))
        db.reindex(con, [bid])
    return {"id": bid}


@app.patch("/api/bookmarks/{bookmark_id}")
def patch_bookmark(bookmark_id: int, body: BookmarkPatch, c: Ctx = Depends(ctx)):
    con = c.con
    row = own(con, "bookmarks", bookmark_id, c.uid)
    with db.tx(con):
        if body.url is not None:
            url = clean_url(body.url)
            con.execute("UPDATE bookmarks SET url=?, host=? WHERE id=?", (url, url_host(url), bookmark_id))
        if body.title is not None:
            title = clean_text(body.title, 500) or (body.url and clean_url(body.url)) or row["url"]
            con.execute("UPDATE bookmarks SET title=? WHERE id=?", (title, bookmark_id))
        if body.notes is not None:
            con.execute("UPDATE bookmarks SET notes=? WHERE id=?", (body.notes.strip(), bookmark_id))
        if body.color is not None:
            con.execute("UPDATE bookmarks SET color=? WHERE id=?", (check_color(body.color), bookmark_id))
        if body.tags is not None:
            set_tags(con, bookmark_id, norm_tags(body.tags))
        if "category_id" in body.model_fields_set and body.category_id != row["category_id"]:
            if body.category_id is not None:
                own(con, "categories", body.category_id, c.uid)
            move_bookmarks(c, [bookmark_id], body.category_id)
        con.execute("UPDATE bookmarks SET updated_at=? WHERE id=?", (db.now(), bookmark_id))
        db.reindex(con, [bookmark_id])
    return {"ok": True}


@app.delete("/api/bookmarks/{bookmark_id}")
def delete_bookmark(bookmark_id: int, c: Ctx = Depends(ctx)):
    c.con.execute("DELETE FROM bookmarks WHERE id=? AND user_id=?", (bookmark_id, c.uid))
    return {"ok": True}


SORTS = {
    "newest": "b.created_at DESC, b.id DESC",
    "oldest": "b.created_at, b.id",
    "title": "b.title COLLATE NOCASE, b.id",
    "title_desc": "b.title COLLATE NOCASE DESC, b.id DESC",
    "domain": "b.host, b.url, b.id",
}


@app.get("/api/bookmarks")
def list_bookmarks(f: Filter = Depends(query_filter), sort: str = "newest", offset: int = Query(0, ge=0),
                   limit: int = Query(100, ge=1, le=500), c: Ctx = Depends(ctx)):
    where, params = filter_sql(c.uid, f)
    total = c.con.execute(f"SELECT COUNT(*) FROM bookmarks b WHERE {where}", params).fetchone()[0]
    rows = c.con.execute(
        "SELECT b.*, cat.name AS category_name, t.id AS tab_id, t.name AS tab_name FROM bookmarks b"
        " LEFT JOIN categories cat ON cat.id=b.category_id LEFT JOIN tabs t ON t.id=cat.tab_id"
        f" WHERE {where} ORDER BY {SORTS.get(sort, SORTS['newest'])} LIMIT ? OFFSET ?",
        [*params, limit, offset]).fetchall()
    tags = tags_for(c.con, [r["id"] for r in rows])
    items = []
    for r in rows:
        item = bookmark_json(r, tags.get(r["id"], []))
        item.update(category_name=r["category_name"], tab_id=r["tab_id"], tab_name=r["tab_name"])
        items.append(item)
    return {"total": total, "items": items}


class UrlBody(BaseModel):
    url: str = Field(max_length=2000)


@app.post("/api/bookmarks/lookup")
def lookup_bookmark(body: UrlBody, c: Ctx = Depends(ctx)):
    """Already-bookmarked detection for the Add dialog."""
    url = body.url
    variants = {url.strip()}
    try:
        variants.add(clean_url(url))
    except HTTPException:
        pass
    for v in list(variants):
        variants.add(v.rstrip("/") if v.endswith("/") else v + "/")
    row = c.con.execute(
        f"SELECT * FROM bookmarks WHERE user_id=? AND url IN ({db.marks(len(variants))}) ORDER BY id LIMIT 1",
        [c.uid, *variants]).fetchone()
    if not row:
        return {"bookmark": None}
    return {"bookmark": bookmark_json(row, tags_for(c.con, [row["id"]]).get(row["id"], []))}


@app.get("/api/tags")
def list_tags(f: Filter = Depends(query_filter), c: Ctx = Depends(ctx)):
    where, params = filter_sql(c.uid, f)
    order = "bt.tag" if c.settings["tag_order"] == "alpha" else "n DESC, bt.tag"
    rows = c.con.execute(
        "SELECT bt.tag, COUNT(*) AS n FROM bookmark_tags bt JOIN bookmarks b ON b.id=bt.bookmark_id"
        f" WHERE {where} GROUP BY bt.tag ORDER BY {order}", params).fetchall()
    return {"tags": [[r["tag"], r["n"]] for r in rows]}


class Bulk(BaseModel):
    action: Literal["delete", "move", "add_tags", "remove_tags", "replace_tag"]
    ids: Optional[list[int]] = Field(None, max_length=MAX_BOOKMARKS)
    filter: Optional[Filter] = None  # alternative to ids: everything matching
    category_id: Optional[int] = None
    tags: list[str] = Field([], max_length=100)
    old: str = ""
    new: str = ""


@app.post("/api/bookmarks/bulk")
def bulk(body: Bulk, c: Ctx = Depends(ctx)):
    con = c.con
    with db.tx(con):
        if body.filter is not None:
            where, params = filter_sql(c.uid, body.filter)
            ids = [r[0] for r in con.execute(f"SELECT b.id FROM bookmarks b WHERE {where}", params)]
        else:
            ids = []
            for part in db.chunks(body.ids or []):
                found = {r[0] for r in con.execute(
                    f"SELECT id FROM bookmarks WHERE user_id=? AND id IN ({db.marks(len(part))})", [c.uid, *part])}
                ids += [i for i in part if i in found]
        if body.action == "delete":
            for part in db.chunks(ids):
                con.execute(f"DELETE FROM bookmarks WHERE id IN ({db.marks(len(part))})", part)
        elif body.action == "move":
            if body.category_id is not None:
                own(con, "categories", body.category_id, c.uid)
            move_bookmarks(c, ids, body.category_id)
        else:
            if body.action == "add_tags":
                add_tags(con, ids, norm_tags(body.tags))
            elif body.action == "remove_tags":
                remove_tags(con, ids, norm_tags(body.tags))
            else:
                old, new = norm_tag(body.old), norm_tag(body.new)
                if not old or not new:
                    raise err(422, "bad_tag")
                ids = [r[0] for part in db.chunks(ids) for r in con.execute(
                    f"SELECT bookmark_id FROM bookmark_tags WHERE tag=? AND bookmark_id IN ({db.marks(len(part))})",
                    [old, *part])]
                add_tags(con, ids, [new])
                if new != old:
                    remove_tags(con, ids, [old])
            db.reindex(con, ids)
    return {"count": len(ids)}


class TagRename(BaseModel):
    old: str
    new: str = ""  # empty = delete the tag everywhere


@app.post("/api/tags/rename")
def rename_tag(body: TagRename, c: Ctx = Depends(ctx)):
    old, new = norm_tag(body.old), norm_tag(body.new)
    if not old:
        raise err(422, "bad_tag")
    with db.tx(c.con):
        ids = ids_with_tag(c.con, c.uid, old)
        if new and new != old:
            add_tags(c.con, ids, [new])
        if new != old:
            remove_tags(c.con, ids, [old])
        db.reindex(c.con, ids)
    return {"count": len(ids)}


# --- tools: duplicates, dead links, page titles, favicons ---------------------

class Duplicates(BaseModel):
    strict: bool = True
    tag: str = "duplicate"
    mark: Literal["duplicates", "both"] = "duplicates"


def loose_url(url: str) -> str:
    """http/https and a leading www. are ignored when matching non-strictly."""
    m = re.match(r"^https?://(www\.)?", url, re.I)
    if not m:
        return url
    host, sep, rest = url[m.end():].partition("/")
    return (host.lower() + sep + rest).rstrip("/")


@app.post("/api/tools/duplicates")
def find_duplicates(body: Duplicates, c: Ctx = Depends(ctx)):
    tag = norm_tag(body.tag) or "duplicate"
    con = c.con
    with db.tx(con):
        stale = ids_with_tag(con, c.uid, tag)
        remove_tags(con, stale, [tag])
        groups: dict[str, list[int]] = {}
        for r in con.execute("SELECT id, url FROM bookmarks WHERE user_id=? ORDER BY created_at, id", (c.uid,)):
            groups.setdefault(r["url"] if body.strict else loose_url(r["url"]), []).append(r["id"])
        dupes = [ids for ids in groups.values() if len(ids) > 1]
        tagged = [i for ids in dupes for i in (ids if body.mark == "both" else ids[1:])]
        add_tags(con, tagged, [tag])
        db.reindex(con, set(stale) | set(tagged))
    return {"groups": len(dupes), "duplicates": sum(len(ids) - 1 for ids in dupes), "tag": tag}


JOBS: dict[int, dict] = {}
_tasks: set[asyncio.Task] = set()


class DeadLinks(BaseModel):
    tag: str = "dead-link"


async def deadlink_job(client, uid: int, tag: str, rows: list) -> None:
    job = JOBS[uid]
    sem = asyncio.Semaphore(8)

    async def check(bookmark_id: int, url: str) -> None:
        async with sem:
            dead = await net.is_dead(client, url)
        job["checked"] += 1
        if dead:
            job["dead"] += 1
            con = db.connect()
            try:
                with db.tx(con):
                    # the bookmark may have been deleted while the job ran
                    if con.execute("SELECT 1 FROM bookmarks WHERE id=?", (bookmark_id,)).fetchone():
                        add_tags(con, [bookmark_id], [tag])
                        db.reindex(con, [bookmark_id])
            finally:
                con.close()

    try:
        await asyncio.gather(*(check(r["id"], r["url"]) for r in rows))
        job["status"] = "done"
    except Exception:
        job["status"] = "failed"
        raise
    finally:
        job["finished"] = db.now()


@app.post("/api/tools/deadlinks")
async def start_deadlinks(body: DeadLinks, request: Request, c: Ctx = Depends(ctx)):
    tag = norm_tag(body.tag) or "dead-link"
    if JOBS.get(c.uid, {}).get("status") == "running":
        raise err(409, "already_running")
    if sum(j["status"] == "running" for j in JOBS.values()) >= 3:
        raise err(429, "busy")

    def prepare():
        with db.tx(c.con):
            stale = ids_with_tag(c.con, c.uid, tag)
            remove_tags(c.con, stale, [tag])
            db.reindex(c.con, stale)
        return c.con.execute("SELECT id, url FROM bookmarks WHERE user_id=? AND host<>''", (c.uid,)).fetchall()

    rows = await run_in_threadpool(prepare)
    JOBS[c.uid] = {"status": "running", "total": len(rows), "checked": 0, "dead": 0, "tag": tag,
                   "started": db.now(), "finished": None}
    task = asyncio.create_task(deadlink_job(request.app.state.http, c.uid, tag, rows))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return JOBS[c.uid]


@app.get("/api/tools/deadlinks")
def deadlinks_status(c: Ctx = Depends(ctx)):
    return JOBS.get(c.uid) or {"status": "idle"}


@app.post("/api/fetch-title")
async def fetch_title(body: UrlBody, request: Request, c: Ctx = Depends(ctx)):
    key = f"title:{c.uid}"
    if limiter.blocked(key, 60, 60):
        raise err(429, "too_many_attempts")
    limiter.hit(key)
    try:
        return {"title": await net.page_title(request.app.state.http, clean_url(body.url))}
    except (net.FetchError, HTTPException):
        return {"title": ""}


_favicon_sem = asyncio.Semaphore(6)
_favicon_pending: dict[str, asyncio.Future] = {}


def _favicon_known(host: str) -> bool:
    con = db.connect()
    try:
        return con.execute("SELECT 1 FROM bookmarks WHERE host=? LIMIT 1", (host,)).fetchone() is not None
    finally:
        con.close()


@app.get("/favicon/{host}", include_in_schema=False)
async def favicon(host: str, request: Request):
    """Site icons are fetched once by the server and cached, so browsers never contact third parties."""
    host = host.lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host:
        raise err(404, "not_found")
    path = FAVICONS / host
    miss = Response(status_code=404, headers={"Cache-Control": "public, max-age=86400"})
    fresh = path.exists() and (path.stat().st_size > 0 or time.time() - path.stat().st_mtime < 7 * 86400)
    if not fresh:
        # only for hosts someone has bookmarked: this must not become an open proxy
        if not await run_in_threadpool(_favicon_known, host):
            raise err(404, "not_found")
        pending = _favicon_pending.get(host)
        if pending is None:
            pending = _favicon_pending[host] = asyncio.get_running_loop().create_future()
            try:
                async with _favicon_sem:
                    try:
                        data = await asyncio.wait_for(net.favicon(request.app.state.http, host), 20)
                    except Exception:
                        data = None
                path.write_bytes(data or b"")
            finally:
                pending.set_result(None)
                del _favicon_pending[host]
        else:
            await pending
    data = path.read_bytes() if path.exists() else b""
    media_type = net.sniff_image(data)
    if not media_type:
        return miss
    return Response(data, media_type=media_type, headers={
        "Cache-Control": "public, max-age=604800", "Content-Security-Policy": "default-src 'none'; sandbox"})


# --- import / export ---------------------------------------------------------

def do_import(c: Ctx, text: str, mode: str, tab_name: str, folder_tags: bool, skip_dupes: bool) -> dict:
    con = c.con
    items = importer.parse(text)
    stats = {"found": len(items), "imported": 0, "skipped": 0, "duplicates": 0}
    room = MAX_BOOKMARKS - bookmark_count(con, c.uid)
    seen = {r[0] for r in con.execute("SELECT url FROM bookmarks WHERE user_id=?", (c.uid,))} if skip_dupes else set()
    new_ids: list[int] = []
    with db.tx(con):
        tabs = {r["name"].casefold(): r["id"] for r in
                con.execute("SELECT id, name FROM tabs WHERE user_id=? ORDER BY id DESC", (c.uid,))}
        cats = {(r["tab_id"], r["name"].casefold()): r["id"] for r in
                con.execute("SELECT id, tab_id, name FROM categories WHERE user_id=? ORDER BY id DESC", (c.uid,))}
        positions: dict[int, int] = {}

        def category(tab: str, name: str) -> int:
            tab, name = clean_text(tab, 100) or "Imported", clean_text(name, 100) or "Imported"
            tab_id = tabs.get(tab.casefold())
            if tab_id is None:
                tab_id = tabs[tab.casefold()] = new_tab(con, c.uid, tab)
            cat_id = cats.get((tab_id, name.casefold()))
            if cat_id is None:
                cat_id = cats[(tab_id, name.casefold())] = new_category(con, c.uid, tab_id, name)
            return cat_id

        for item in items:
            try:
                url = clean_url(item["url"], guess=False)
            except HTTPException:
                stats["skipped"] += 1  # javascript:, place:, chrome:// and the like
                continue
            if skip_dupes:
                if url in seen:
                    stats["duplicates"] += 1
                    continue
                seen.add(url)
            if stats["imported"] >= room:
                stats["skipped"] += 1
                continue
            tags, path = list(item["tags"]), item["path"]
            if item["catalog"] or mode == "catalog" or (mode == "structure" and not path):
                cat_id = None
                if folder_tags and not item["catalog"]:
                    tags += path
            elif mode == "tab":
                cat_id = category(tab_name, " / ".join(path) or tab_name)
            else:
                cat_id = category(path[0], " / ".join(path[1:]) or path[0])
            position = 0
            if cat_id is not None:
                if cat_id not in positions:
                    positions[cat_id] = next_position(con, cat_id)
                position = positions[cat_id]
                positions[cat_id] += 1
            new_ids.append(insert_bookmark(
                con, c.uid, url, clean_text(item["title"], 500) or url, item["notes"][:5000], norm_tags(tags),
                cat_id, position, item["add_date"] or db.now()))
            stats["imported"] += 1
        db.reindex(con, new_ids)
    return stats


@app.post("/api/import")
async def import_bookmarks(request: Request, mode: Literal["catalog", "tab", "structure"] = "catalog",
                           tab_name: str = Query("Imported", max_length=100), folder_tags: bool = True,
                           skip_duplicates: bool = True, c: Ctx = Depends(ctx)):
    if int(request.headers.get("content-length") or 0) > MAX_IMPORT_BYTES:
        raise err(413, "too_large")
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise err(413, "too_large")
    text = raw.decode("utf-8", "replace")
    return await run_in_threadpool(do_import, c, text, mode, tab_name, folder_tags, skip_duplicates)


@app.get("/api/export")
def export_bookmarks(c: Ctx = Depends(ctx)):
    con = c.con
    rows = con.execute("SELECT * FROM bookmarks WHERE user_id=? ORDER BY position, id", (c.uid,)).fetchall()
    tags = tags_for(con, [r["id"] for r in rows])
    by_cat: dict[Optional[int], list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category_id"], []).append(bookmark_json(r, tags.get(r["id"], [])))
    tabs = []
    for t in con.execute("SELECT id, name FROM tabs WHERE user_id=? ORDER BY position, id", (c.uid,)).fetchall():
        cats = con.execute("SELECT id, name FROM categories WHERE tab_id=? ORDER BY col, position, id",
                           (t["id"],)).fetchall()
        tabs.append({"name": t["name"],
                     "categories": [{"name": k["name"], "bookmarks": by_cat.get(k["id"], [])} for k in cats]})
    body = importer.export(tabs, by_cat.get(None, []))
    name = time.strftime("stash-bookmarks-%Y-%m-%d.html")
    return Response(body, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


from . import api_v1  # noqa: E402  (uses the helpers above)

from . import history  # noqa: E402

app.include_router(history.v1)    # before api_v1: /api/v1/history/... must not be taken for anything else
app.include_router(history.session_api)
app.include_router(api_v1.v1)
app.include_router(api_v1.session_api)

CLIENT_DIST = BASE / "client" / "dist"
HISTORY_SCRIPT = BASE / "scripts" / "stash-history-sync.py"


@app.api_route("/dl/{name}", methods=["GET", "HEAD"], include_in_schema=False)
def client_download(name: str):
    """The stashai terminal client as a wheel (`pipx install https://…/dl/<wheel>`) and the history sync script."""
    if name == HISTORY_SCRIPT.name:
        return FileResponse(HISTORY_SCRIPT, media_type="text/x-python; charset=utf-8")
    path = CLIENT_DIST / name
    if not re.fullmatch(r"stashai-[\w.]+-py3-none-any\.whl", name) or not path.is_file():
        raise err(404, "not_found")
    return FileResponse(path, media_type="application/zip")


@app.get("/api/client")
def client_info():
    wheels = sorted(CLIENT_DIST.glob("stashai-*-py3-none-any.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        return {"wheel": None, "version": None}
    return {"wheel": f"{ORIGIN}/dl/{wheels[-1].name}", "version": wheels[-1].name.split("-")[1]}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
