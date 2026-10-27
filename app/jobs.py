"""Scheduled tasks, run by timers on the server (see "Scheduled tasks" in the README):

  backup        scripts/backup.sh, hourly: the 30-day version history of everything
  media-sync    every 6 hours: fetch every connected account's playlists and subscriptions (import only; pushing
                changes stays a thing the user starts), in small batches and within the services' limits
  duplicates    daily: tag the later copies of the same address "duplicate", never the oldest one

    .venv/bin/python -m app.cli job media-sync
    .venv/bin/python -m app.cli job duplicates
    .venv/bin/python -m app.cli jobs            # when each task last ran, and whether it is overdue

Each run leaves a record in the config table (job:<name>); the web UI warns administrators when a task has not
run within its expected interval, which is how a missing or broken timer shows up.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

from . import db

# name -> (hours after which the task counts as overdue, what it is), for the warning in the UI
JOBS = {
    "backup": (3, "hourly database backup (30 days of versions)"),
    "media-sync": (14, "fetching playlists and subscriptions every 6 hours"),
    "duplicates": (28, "daily duplicate tagging"),
}
PLAYLIST_BATCH = 40          # playlists whose songs are read per account and service in one run
PAUSE_BETWEEN = 5            # seconds between accounts and services, to spread the requests


def record(con, name: str, ok: bool, summary: str) -> None:
    t = db.now()
    previous = json.loads(db.get_config(con, f"job:{name}", "{}") or "{}")
    data = {"last_run": t, "last_ok": t if ok else previous.get("last_ok"), "ok": ok, "summary": summary[:2000]}
    db.set_config(con, f"job:{name}", json.dumps(data, ensure_ascii=False))


def newest_backup() -> int | None:
    folder = Path(os.environ.get("STASH_BACKUP_DIR") or db.DATA / "backups")
    try:
        times = [p.stat().st_mtime for p in folder.glob("stash-*.db.zst")]
    except OSError:
        return None
    return int(max(times)) if times else None


def status(con) -> list[dict]:
    """Every task with when it last ran well and whether it is overdue."""
    out, t = [], time.time()
    for name, (hours, what) in JOBS.items():
        if name == "backup":
            last = newest_backup()
            data = {"last_run": last, "last_ok": last, "ok": last is not None, "summary": ""}
        else:
            data = json.loads(db.get_config(con, f"job:{name}", "{}") or "{}")
        last_ok = data.get("last_ok")
        out.append({"name": name, "what": what, "every_hours": hours, "last_run": data.get("last_run"),
                    "last_ok": last_ok, "ok": bool(data.get("ok")), "summary": data.get("summary", ""),
                    "overdue": not last_ok or t - last_ok > hours * 3600})
    return out


def _users(con, sql: str):
    from .main import Ctx

    return [Ctx(con, r) for r in con.execute(sql).fetchall()]


def media_sync() -> dict:
    """Import only: playlists and subscriptions of every account with a music service connected. A service that
    has paused the app is skipped until the pause ends; one account's failure does not stop the others."""
    from . import playlists, subscriptions

    con = db.connect()
    lines, failures = [], 0
    try:
        accounts = con.execute("SELECT a.user_id, a.service FROM playlist_accounts a JOIN users u ON u.id=a.user_id"
                               " WHERE a.access_token<>'' ORDER BY a.user_id, a.service").fetchall()
        for n, acc in enumerate(accounts):
            if n:
                time.sleep(PAUSE_BETWEEN)
            c = _users(con, f"SELECT * FROM users WHERE id={int(acc['user_id'])}")[0]
            who = f"{c.user['username']}/{acc['service']}"
            for label, run in (("playlists", lambda: playlists.pull(c, acc["service"], budget=PLAYLIST_BATCH)),
                               ("subscriptions", lambda: subscriptions.pull(c, acc["service"]))):
                try:
                    r = run()
                    if label == "playlists":
                        lines.append(f"{who} playlists: {r['fetched']}/{r['total']} ({r.get('unchanged', 0)} unchanged,"
                                     f" {len(r.get('deferred', []))} for a later run, {len(r['failed'])} failed)")
                    else:
                        lines.append(f"{who} subscriptions: {r['total']} ({r['new']} new, {r['gone']} gone)")
                except playlists.ServiceError as e:
                    code = playlists.problem_code(c, e)
                    lines.append(f"{who} {label}: {code}")
                    if not code.startswith("rate_limited"):   # a pause is expected, not a failure of the task
                        failures += 1
                    if e.status == 429:
                        break   # the same app: do not try its subscriptions during the pause either
                except Exception as e:  # noqa: BLE001  (an HTTPException such as not_connected, or a bug)
                    failures += 1
                    lines.append(f"{who} {label}: {getattr(e, 'detail', e)}")
        summary = "; ".join(lines) or "no music service connected"
        record(con, "media-sync", failures == 0, summary)
        return {"ok": failures == 0, "summary": summary}
    except Exception:
        record(con, "media-sync", False, traceback.format_exc()[-1500:])
        raise
    finally:
        con.close()


def duplicates() -> dict:
    """Every account: the later copies of an address get the tag "duplicate", the oldest never does. http and
    https (and a leading www. and a trailing slash) count as the same address."""
    from .main import mark_duplicates

    con = db.connect()
    try:
        lines = []
        for c in _users(con, "SELECT * FROM users ORDER BY id"):
            r = mark_duplicates(con, c.uid, "duplicate", strict=False)
            lines.append(f"{c.user['username']}: {r['duplicates']} duplicates in {r['groups']} groups"
                         f" (+{r['tagged']} −{r['untagged']})")
        summary = "; ".join(lines)
        record(con, "duplicates", True, summary)
        return {"ok": True, "summary": summary}
    except Exception:
        record(con, "duplicates", False, traceback.format_exc()[-1500:])
        raise
    finally:
        con.close()


RUN = {"media-sync": media_sync, "duplicates": duplicates}
