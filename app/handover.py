"""Project handovers: a written record of a project that an AI assistant saves through MCP, so another session
(or another assistant) can pick the project up later. Every save is a new version; nothing is ever overwritten
or deleted. This is separate from bookmarks: no changeset, no trash and no undo apply here.

The web UI shows the projects and versions at /#/handovers; `/api/handovers` serves that page with the session
cookie. The MCP tools (handover_save, handover_get, handover_list) go through LocalAPI with an API key.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends

from . import db
from .main import Ctx, ctx, err

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 200_000
MAX_SHORT = 200
MAX_LIST = 500
# control characters other than newline and tab (also the C1 range)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

session_api = APIRouter(prefix="/api/handovers")


def clean(value, limit: int) -> str:
    """Text as data: control characters out, length limited. Newlines and tabs stay."""
    text = _CONTROL.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()[:limit]


def project_name(value) -> str:
    name = str(value or "").strip().lower()
    if not PROJECT_RE.match(name):
        raise err(422, "bad_project: a project name is 1–64 lowercase letters, digits and dashes")
    return name


def _project(c: Ctx, name: str):
    row = c.con.execute("SELECT * FROM handovers WHERE user_id=? AND name=?", (c.uid, name)).fetchone()
    if not row:
        raise err(404, "no_such_project")
    return row


def _version_dict(project: str, r) -> dict:
    return {"project": project, "version": r["version"], "title": r["title"], "body": r["body"], "ai": r["ai"],
            "machine": r["machine"], "session": r["session"], "git_commit": r["git_commit"],
            "created_at": r["created_at"]}


def save(c: Ctx, project, body, ai="", machine="", session="", title="", expected_version=None,
         git_commit="") -> dict:
    """Store the next version of a project's handover. With expected_version, refuse unless it is the latest:
    two assistants then never overwrite each other without noticing."""
    name = project_name(project)
    if len(str(body or "")) > MAX_BODY:
        raise err(422, f"body_too_long: at most {MAX_BODY} characters")
    body = clean(body, MAX_BODY)
    if not body:
        raise err(422, "empty_body")
    ai, machine, session = (clean(v, MAX_SHORT).replace("\n", " ") for v in (ai, machine, session))
    title = clean(title, MAX_SHORT).replace("\n", " ")
    git_commit = clean(git_commit, 64).replace("\n", " ")
    if expected_version is not None:
        try:
            expected_version = int(expected_version)
        except (TypeError, ValueError) as e:
            raise err(422, "bad_expected_version") from e
    t = db.now()
    with db.tx(c.con):
        row = c.con.execute("SELECT * FROM handovers WHERE user_id=? AND name=?", (c.uid, name)).fetchone()
        current = row["latest_version"] if row else 0
        if expected_version is not None and expected_version != current:
            raise err(409, f"version_conflict: the latest version of '{name}' is {current}, not {expected_version};"
                           " read it with handover_get before saving")
        if row:
            pid = row["id"]
        else:
            pid = c.con.execute("INSERT INTO handovers(user_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                                (c.uid, name, t, t)).lastrowid
        version = current + 1
        c.con.execute(
            "INSERT INTO handover_versions(handover_id, version, title, body, ai, machine, session, git_commit, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (pid, version, title, body, ai, machine, session, git_commit, t))
        c.con.execute("UPDATE handovers SET latest_version=?, updated_at=? WHERE id=?", (version, t, pid))
    return {"project": name, "version": version, "created_at": t}


def get(c: Ctx, project, version=None) -> dict:
    name = project_name(project)
    row = _project(c, name)
    if version is None:
        v = row["latest_version"]
    else:
        try:
            v = int(version)
        except (TypeError, ValueError) as e:
            raise err(422, "bad_version") from e
    r = c.con.execute("SELECT * FROM handover_versions WHERE handover_id=? AND version=?", (row["id"], v)).fetchone()
    if not r:
        raise err(404, f"no_such_version: '{name}' has versions 1–{row['latest_version']}")
    return {**_version_dict(name, r), "latest_version": row["latest_version"]}


def versions(c: Ctx, project) -> list[dict]:
    """Every version of a project without the bodies, newest first."""
    name = project_name(project)
    row = _project(c, name)
    rows = c.con.execute(
        "SELECT version, title, ai, machine, session, git_commit, created_at, length(body) AS size"
        " FROM handover_versions WHERE handover_id=? ORDER BY version DESC", (row["id"],)).fetchall()
    return [dict(r) for r in rows]


def list_projects(c: Ctx) -> list[dict]:
    """Each project with its latest version and who wrote it, most recently updated first."""
    rows = c.con.execute(
        "SELECT h.name, h.latest_version, h.created_at, h.updated_at, v.title, v.ai, v.machine, v.session"
        " FROM handovers h LEFT JOIN handover_versions v ON v.handover_id=h.id AND v.version=h.latest_version"
        " WHERE h.user_id=? ORDER BY h.updated_at DESC, h.name LIMIT ?", (c.uid, MAX_LIST)).fetchall()
    return [dict(r) for r in rows]


# --- the web UI's view, with the session cookie ------------------------------------------------------

@session_api.get("")
def web_list(c: Ctx = Depends(ctx)):
    return {"projects": list_projects(c)}


@session_api.get("/{project}")
def web_versions(project: str, c: Ctx = Depends(ctx)):
    return {"project": project_name(project), "versions": versions(c, project)}


@session_api.get("/{project}/{version}")
def web_get(project: str, version: int, c: Ctx = Depends(ctx)):
    return get(c, project, version)
