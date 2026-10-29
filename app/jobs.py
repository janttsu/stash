"""Scheduled tasks, run by timers on the server (see "Scheduled tasks" in the README):

  backup        scripts/backup.sh, hourly: the 30-day version history of everything
  media-sync    every 6 hours: fetch every connected account's playlists and subscriptions (import only; pushing
                changes stays a thing the user starts), in small batches and within the services' limits, and
                look up when YouTube channels last published, each channel about once a day
  duplicates    daily: tag the later copies of the same address "duplicate", never the oldest one
  youtube-push  daily, after YouTube's quota reset: the pending changes of YouTube playlists (a published
                Stash-only playlist's videos, removals, renames), then the deletions of playlists marked
                for it, after the pending subscribes and unsubscribes that go first (up to SUBSCRIPTION_SHARE);
                at most PUSH_BATCH writes a run since each costs 50 of the day's 10 000 units

    .venv/bin/python -m app.cli job media-sync
    .venv/bin/python -m app.cli job duplicates
    .venv/bin/python -m app.cli job youtube-push
    .venv/bin/python -m app.cli job spotify-push   # the same for Spotify (follows and unfollows first): SPOTIFY_BATCH calls, one a second
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
    "youtube-push": (28, "daily batch of playlist changes to YouTube, within its quota"),
    "spotify-push": (28, "daily batch of playlist changes to Spotify, a few calls a day"),
}
PUSH_BATCH = 160             # writes to YouTube per youtube-push run: 8 000 of the day's 10 000 quota units
SUBSCRIPTION_SHARE = 40      # of those, at most this many may go to pending subscribes and unsubscribes, sent first
SPOTIFY_BATCH = 60           # calls to Spotify per spotify-push run (a hundred tracks each at most)
SPOTIFY_PACE = 1.0           # seconds between those calls
PLAYLIST_BATCH = 40          # playlists whose songs are read per account and service in one run
ACTIVITY_BATCH = 200         # YouTube channels whose newest upload is looked up per account in one run (1 quota unit each)
ACTIVITY_EVERY = 24 * 3600   # a channel is looked up again after this
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
    from . import main  # noqa: F401  (first: in a process of its own, main must load playlists and subscriptions)
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
            steps = [("playlists", lambda: playlists.pull(c, acc["service"], budget=PLAYLIST_BATCH)),
                     ("subscriptions", lambda: subscriptions.pull(c, acc["service"]))]
            if acc["service"] == "youtube":
                steps.append(("activity", lambda: _stale_activity(c)))
            for label, run in steps:
                try:
                    r = run()
                    if label == "playlists":
                        lines.append(f"{who} playlists: {r['fetched']}/{r['total']} ({r.get('unchanged', 0)} unchanged,"
                                     f" {len(r.get('deferred', []))} for a later run, {len(r['failed'])} failed)")
                    elif label == "subscriptions":
                        lines.append(f"{who} subscriptions: {r['total']} ({r['new']} new, {r['gone']} gone)")
                    else:
                        lines.append(f"{who} activity: {r['checked']}/{r['total']} channels ({len(r['failed'])} failed)")
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


def _stale_activity(c) -> dict:
    """The newest-upload date of the YouTube channels not looked at for a day, the longest-waiting first, at most
    ACTIVITY_BATCH of them: with four runs a day every channel of an account of up to 800 is seen daily."""
    from . import subscriptions

    ids = [r[0] for r in c.con.execute(
        "SELECT id FROM subscriptions WHERE user_id=? AND service='youtube' AND kind='channel' AND state<>'local'"
        " AND (activity_checked IS NULL OR activity_checked<?) ORDER BY activity_checked LIMIT ?",
        (c.uid, db.now() - ACTIVITY_EVERY, ACTIVITY_BATCH)).fetchall()]
    if not ids:
        return {"total": 0, "checked": 0, "failed": []}
    return subscriptions.check_activity(c, "youtube", ids)


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


def _subscriptions_first(c, service: str, budget: int) -> tuple[str, int]:
    """A push task's pending subscribes and unsubscribes go before its playlists, when there are at most
    SUBSCRIPTION_SHARE of them (they are few and the user waits for them). Returns the summary's opening and
    what is left of the budget for the playlists."""
    from . import subscriptions

    waiting = c.con.execute("SELECT COUNT(*) FROM subscriptions WHERE user_id=? AND service=?"
                            " AND state IN ('added', 'removed')", (c.uid, service)).fetchone()[0]
    if not waiting:
        return "", budget
    if waiting > SUBSCRIPTION_SHARE:
        return f"{waiting} subscription changes wait (more than {SUBSCRIPTION_SHARE}: synchronise them from the page); ", budget
    s = subscriptions.sync(c, service, push=True)
    line = f"{len(s['pushed'])} subscription changes sent" + (
        f", {len(s['push_failed'])} refused" if s["push_failed"] else "") + "; "
    return line, budget - len(s["pushed"]) - len(s["push_failed"])


def youtube_push() -> dict:
    """Every account with YouTube connected: up to PUSH_BATCH pending writes (playlists.push_batch). A quota
    that is spent, or a pause YouTube asks for, ends the run without counting as a failure: the next run goes
    on from where this one stopped, since every write is recorded as soon as it is done."""
    from . import main  # noqa: F401
    from . import playlists

    con = db.connect()
    lines, failures = [], 0
    try:
        accounts = con.execute("SELECT user_id FROM playlist_accounts WHERE service='youtube' AND access_token<>''"
                               " ORDER BY user_id").fetchall()
        for n, acc in enumerate(accounts):
            if n:
                time.sleep(PAUSE_BETWEEN)
            c = _users(con, f"SELECT * FROM users WHERE id={int(acc['user_id'])}")[0]
            who = f"{c.user['username']}/youtube"
            try:
                line, budget = _subscriptions_first(c, "youtube", PUSH_BATCH)
                r = playlists.push_batch(c, budget)
                lines.append(f"{who}: {line}{r['writes']} writes, {len(r['completed'])} playlists complete, {r['remaining']} items"
                             f" still pending, {len(r['deleted'])} playlists deleted"
                             + (f", refused by YouTube: {len(r['failed'])}" if r["failed"] else ""))
            except playlists.ServiceError as e:
                code = playlists.problem_code(c, e)
                lines.append(f"{who}: {code}")
                if not code.startswith("rate_limited"):
                    failures += 1
            except Exception as e:  # noqa: BLE001
                failures += 1
                lines.append(f"{who}: {getattr(e, 'detail', e)}")
        summary = "; ".join(lines) or "YouTube not connected"
        record(con, "youtube-push", failures == 0, summary)
        return {"ok": failures == 0, "summary": summary}
    except Exception:
        record(con, "youtube-push", False, traceback.format_exc()[-1500:])
        raise
    finally:
        con.close()


def spotify_push() -> dict:
    """Every account with Spotify connected: the pending follows and unfollows first (as in youtube_push),
    then pending playlist changes (playlists.push_batch) up to SPOTIFY_BATCH calls in all, one call a second. Spotify has no daily quota but pauses an app that calls too
    much, so the batch stays small and a pause ends the run without counting as a failure."""
    from . import main  # noqa: F401
    from . import playlists

    con = db.connect()
    lines, failures = [], 0
    try:
        accounts = con.execute("SELECT user_id FROM playlist_accounts WHERE service='spotify' AND access_token<>''"
                               " ORDER BY user_id").fetchall()
        playlists.MIN_INTERVAL["spotify"] = max(playlists.MIN_INTERVAL.get("spotify", 0), SPOTIFY_PACE)
        for n, acc in enumerate(accounts):
            if n:
                time.sleep(PAUSE_BETWEEN)
            c = _users(con, f"SELECT * FROM users WHERE id={int(acc['user_id'])}")[0]
            who = f"{c.user['username']}/spotify"
            try:
                line, budget = _subscriptions_first(c, "spotify", SPOTIFY_BATCH)
                r = playlists.push_batch(c, budget, "spotify")
                lines.append(f"{who}: {line}{r['writes']} calls, {len(r['completed'])} playlists complete, {r['remaining']} items"
                             f" still pending, {len(r['deleted'])} playlists deleted")
            except playlists.ServiceError as e:
                code = playlists.problem_code(c, e)
                lines.append(f"{who}: {code}")
                if not code.startswith("rate_limited"):
                    failures += 1
            except Exception as e:  # noqa: BLE001
                failures += 1
                lines.append(f"{who}: {getattr(e, 'detail', e)}")
        summary = "; ".join(lines) or "Spotify not connected"
        record(con, "spotify-push", failures == 0, summary)
        return {"ok": failures == 0, "summary": summary}
    except Exception:
        record(con, "spotify-push", False, traceback.format_exc()[-1500:])
        raise
    finally:
        con.close()


RUN = {"media-sync": media_sync, "duplicates": duplicates, "youtube-push": youtube_push, "spotify-push": spotify_push}
