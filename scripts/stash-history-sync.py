#!/usr/bin/env python3
"""Send your Firefox browsing history to Stash, so your assistant (MCP) can tell which bookmarks you really use.

Works on macOS and Linux with the Python 3 that comes with the system (3.8+), no extra packages.

  stash-history-sync.py --setup               ask for the Stash address and an API key, save them
  stash-history-sync.py                       send the history now
  stash-history-sync.py --dry-run             show what would be sent, send nothing
  stash-history-sync.py --schedule [HOURS]    send it every HOURS hours (default 6): launchd on macOS, cron line on Linux
  stash-history-sync.py --unschedule          stop the scheduled sync
  stash-history-sync.py --forget              delete this computer's history from Stash

What is sent: for each http(s) address visited in the last --days days (default 365): the address,
its title, visits in the last 30/90/365 days, all visits, and the first and last visit. Query
parameters that often carry secrets (token, code, session, key, password, …) are removed first, and
addresses matching --exclude patterns (or "exclude" in the config) are not sent at all. Each sync
replaces what this computer sent before. Get the API key from Stash → Settings → Browsing history →
“Create a key for this computer”.
"""
from __future__ import annotations

import argparse
import configparser
import getpass
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from fnmatch import fnmatch
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "stash" / "history-sync.json"
LABEL = "stash.history-sync"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
CHUNK = 2000
SECRET_PARAMS = re.compile(
    r"^(.*token.*|.*secret.*|.*passw.*|.*session.*|.*auth.*|code|key|apikey|api_key|sig|signature|state|nonce|otp|"
    r"ticket|sid|ssid|jwt|hash|reset.*|login_hint|email)$", re.I)
REAL_VISITS = (1, 2, 3)  # link, typed, bookmark (not embeds, redirects, downloads, frames or reloads)


# --- Firefox ----------------------------------------------------------------------

def firefox_roots() -> list[Path]:
    home = Path.home()
    return [home / "Library" / "Application Support" / "Firefox",           # macOS
            home / ".mozilla" / "firefox",                                    # Linux
            home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",  # Flatpak
            home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"]    # Snap


def find_profile(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not (path / "places.sqlite").exists():
            sys.exit(f"No places.sqlite in {path}")
        return path
    for root in firefox_roots():
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        cp = configparser.ConfigParser()
        cp.read(ini)
        candidates = []
        # the profile Firefox itself starts with is in [Install…] Default=
        for section in cp.sections():
            if section.startswith("Install") and cp.has_option(section, "Default"):
                candidates.append((cp.get(section, "Default"), True))
        for section in cp.sections():
            if section.startswith("Profile") and cp.has_option(section, "Path"):
                relative = cp.get(section, "IsRelative", fallback="1") == "1"
                if cp.get(section, "Default", fallback="0") == "1":
                    candidates.append((cp.get(section, "Path"), relative))
        for section in cp.sections():
            if section.startswith("Profile") and cp.has_option(section, "Path"):
                candidates.append((cp.get(section, "Path"), cp.get(section, "IsRelative", fallback="1") == "1"))
        for path, relative in candidates:
            p = root / path if relative else Path(path)
            if (p / "places.sqlite").exists():
                return p
    sys.exit("Firefox profile not found. Give it with --profile ~/Library/Application\\ Support/Firefox/Profiles/xxxx")


def clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password:  # never send credentials written into an address
        parts = parts._replace(netloc=parts.hostname + (f":{parts.port}" if parts.port else ""))
    if parts.query:
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                if not SECRET_PARAMS.match(k)]
        parts = parts._replace(query=urllib.parse.urlencode(kept))
    return urllib.parse.urlunsplit(parts._replace(fragment=""))


def read_history(profile: Path, days: int, exclude: list[str]) -> list[dict]:
    """Copy places.sqlite (Firefox keeps it locked) with its write-ahead log, and count the visits."""
    with tempfile.TemporaryDirectory(prefix="stash-history-") as tmp:
        os.chmod(tmp, 0o700)
        for name in ("places.sqlite", "places.sqlite-wal"):
            if (profile / name).exists():
                shutil.copy2(profile / name, Path(tmp) / name)
        con = sqlite3.connect(Path(tmp) / "places.sqlite")
        now = int(time.time() * 1_000_000)
        day = 86400 * 1_000_000
        marks = ",".join(str(t) for t in REAL_VISITS)
        rows = con.execute(f"""
            SELECT p.url, COALESCE(p.title, ''), p.visit_count,
                   SUM(v.visit_date > ?), SUM(v.visit_date > ?), SUM(v.visit_date > ?),
                   MIN(v.visit_date), MAX(v.visit_date)
            FROM moz_places p JOIN moz_historyvisits v ON v.place_id = p.id
            WHERE (p.url LIKE 'http://%' OR p.url LIKE 'https://%') AND p.hidden = 0
              AND v.visit_type IN ({marks}) AND p.last_visit_date > ?
            GROUP BY p.id""", (now - 30 * day, now - 90 * day, now - 365 * day, now - days * day)).fetchall()
        con.close()
    merged: dict[str, dict] = {}
    for url, title, total, d30, d90, d365, first, last in rows:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if any(fnmatch(host, pat) or fnmatch(url, pat) for pat in exclude):
            continue
        clean = clean_url(url)
        item = merged.get(clean)
        if item is None:
            merged[clean] = item = {"url": clean, "title": title[:300], "visits": 0, "visits_30d": 0,
                                    "visits_90d": 0, "visits_365d": 0, "first_visit": None, "last_visit": None}
        item["visits"] += max(total or 0, d365 or 0)
        item["visits_30d"] += d30 or 0
        item["visits_90d"] += d90 or 0
        item["visits_365d"] += d365 or 0
        first, last = first // 1_000_000, last // 1_000_000
        item["first_visit"] = min(item["first_visit"] or first, first)
        item["last_visit"] = max(item["last_visit"] or last, last)
        if title and not item["title"]:
            item["title"] = title[:300]
    return sorted(merged.values(), key=lambda i: (-i["visits_90d"], -i["visits"]))


# --- Stash ------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG, 0o600)


def call(cfg: dict, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/api/v1" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json",
                                          "User-Agent": "stash-history-sync/1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except ValueError:
            detail = e.reason
        sys.exit(f"Stash answered {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach Stash at {cfg['url']}: {e.reason}")


def setup() -> None:
    cfg = load_config()
    url = input(f"Stash address [{cfg.get('url', 'https://')}]: ").strip() or cfg.get("url", "")
    if not url.startswith("https://") and not re.match(r"http://(localhost|127\.|192\.168\.|10\.)", url):
        sys.exit("The address must start with https://")
    key = getpass.getpass("API key (Stash → Settings → Browsing history → Create a key for this computer; hidden): ").strip() or cfg.get("key", "")
    source = input(f"Name of this computer in Stash [{cfg.get('source', default_source())}]: ").strip() \
        or cfg.get("source", default_source())
    cfg.update(url=url.rstrip("/"), key=key, source=source)
    me = call(cfg, "GET", "/me")
    if not me.get("can_write"):
        sys.exit("That key can only read. Create one with “Allow changes”.")
    cfg.setdefault("exclude", [])
    save_config(cfg)
    print(f"Saved to {CONFIG} (readable only by you). Signed in as {me['user']}.")
    print("Add patterns to \"exclude\" there to keep sites out, e.g. \"*.bank.example\" or \"*/private/*\".")


def default_source() -> str:
    name = socket.gethostname().split(".")[0] or platform.node() or "computer"
    return re.sub(r"[^\w .@-]", "-", name)[:60]


def sync(args, cfg: dict) -> None:
    profile = find_profile(args.profile or cfg.get("profile"))
    items = read_history(profile, args.days, list(cfg.get("exclude", [])) + list(args.exclude or []))
    print(f"{len(items)} addresses from {profile.name} (last {args.days} days); "
          f"{sum(1 for i in items if i['visits_30d'])} visited in the last 30 days.")
    if args.dry_run:
        for i in items[:25]:
            print(f"  {i['visits_90d']:>4} in 90 d {i['visits']:>6} all  {i['url'][:100]}")
        print("Dry run: nothing was sent.")
        return
    if not cfg.get("url") or not cfg.get("key"):
        sys.exit("Run --setup first.")
    source = cfg.get("source") or default_source()
    chunks = [items[i:i + CHUNK] for i in range(0, len(items), CHUNK)] or [[]]
    for n, chunk in enumerate(chunks):
        call(cfg, "POST", f"/history/{urllib.parse.quote(source)}",
             {"items": chunk, "browser": "firefox", "reset": n == 0, "done": n == len(chunks) - 1})
    print(f"Sent to {cfg['url']} as “{source}”. {time.strftime('%Y-%m-%d %H:%M')}")


# --- scheduling -------------------------------------------------------------------

def schedule(hours: int) -> None:
    script = Path(__file__).resolve()
    python = sys.executable
    if platform.system() == "Darwin":
        log = Path.home() / "Library" / "Logs" / "stash-history-sync.log"
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>{script}</string></array>
  <key>StartInterval</key><integer>{hours * 3600}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
        uid = os.getuid()
        subprocess.call(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], stderr=subprocess.DEVNULL)
        if subprocess.call(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)]) != 0:
            subprocess.call(["launchctl", "load", "-w", str(PLIST)])
        print(f"Scheduled every {hours} h (and now). Log: {log}")
        print("Moving the script later breaks the schedule; run --schedule again after moving it.")
    else:
        print("Add this line with `crontab -e`:")
        print(f"0 */{hours} * * * {python} {script} >> ~/.cache/stash-history-sync.log 2>&1")


def unschedule() -> None:
    if platform.system() == "Darwin" and PLIST.exists():
        subprocess.call(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], stderr=subprocess.DEVNULL)
        PLIST.unlink()
        print("The scheduled sync was removed.")
    else:
        print("Nothing scheduled here (on Linux, remove the line from `crontab -e`).")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--setup", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--schedule", nargs="?", const=6, type=int, metavar="HOURS")
    p.add_argument("--unschedule", action="store_true")
    p.add_argument("--forget", action="store_true", help="delete this computer's history from Stash")
    p.add_argument("--profile", help="the Firefox profile folder (default: the one Firefox starts with)")
    p.add_argument("--days", type=int, default=365, help="send addresses visited in the last DAYS days")
    p.add_argument("--exclude", action="append", help="host or address pattern to leave out (repeatable)")
    args = p.parse_args()
    if args.setup:
        return setup()
    if args.schedule:
        return schedule(args.schedule)
    if args.unschedule:
        return unschedule()
    cfg = load_config()
    if args.forget:
        if not cfg.get("url"):
            sys.exit("Run --setup first.")
        deleted = call(cfg, "DELETE", f"/history/{urllib.parse.quote(cfg.get('source') or default_source())}")
        print(f"Deleted {deleted.get('deleted', 0)} addresses of this computer from Stash.")
        return
    sync(args, cfg)


if __name__ == "__main__":
    main()
