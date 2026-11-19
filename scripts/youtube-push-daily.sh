#!/bin/bash
# The timer is enabled, so a reboot arms today's 10:30 run again. If a push already
# succeeded today, leave the quota for tomorrow instead of sending a second batch.
set -euo pipefail
cd "$HOME/stash"
skip=$(.venv/bin/python - << 'PY'
import json
import sqlite3
from datetime import datetime

con = sqlite3.connect("data/stash.db")
row = con.execute("SELECT value FROM config WHERE key='job:youtube-push'").fetchone()
if not row:
    print("run")
else:
    data = json.loads(row[0])
    last = data.get("last_ok") or 0
    same_day = datetime.fromtimestamp(last).date() == datetime.now().date()
    print("skip" if data.get("ok") and same_day else "run")
PY
)
if [[ "$skip" == "skip" ]]; then
    echo "youtube-push: already succeeded today, not sending a second batch"
    exit 0
fi
exec .venv/bin/python -m app.cli job youtube-push
