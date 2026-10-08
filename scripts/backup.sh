#!/bin/bash
# A consistent copy of the Stash database (every account's bookmarks, tabs, tags and settings), compressed.
# Run it hourly (see "Backups" in the README); copies older than STASH_BACKUP_DAYS (30) days are deleted.
# SQLite's .backup is safe while Stash is running.
#
#   STASH_DATA          the data folder (default: data next to this script's folder)
#   STASH_BACKUP_DIR    where the copies go (default: $STASH_DATA/backups)
#   STASH_BACKUP_DAYS   how many days of copies to keep (default 30)
set -euo pipefail
cd "$(dirname "$0")/.."
data="${STASH_DATA:-data}"
dir="${STASH_BACKUP_DIR:-$data/backups}"
days="${STASH_BACKUP_DAYS:-30}"

umask 077
mkdir -p "$dir"
out="$dir/stash-$(date +%Y-%m-%d_%H%M).db.zst"
part="$dir/.backup-$$.db"
trap 'rm -f "$part" "$part.zst"' EXIT

sqlite3 "$data/stash.db" ".timeout 30000" ".backup '$part'"
check="$(sqlite3 "$part" 'PRAGMA integrity_check')"
[ "$check" = ok ] || { echo "backup: the copy failed its integrity check: $check" >&2; exit 1; }
counts="$(sqlite3 "$part" "SELECT (SELECT COUNT(*) FROM users) || ' accounts, ' || (SELECT COUNT(*) FROM bookmarks) || ' bookmarks'")"
zstd -q -19 "$part" -o "$part.zst"
mv "$part.zst" "$out"

find "$dir" -maxdepth 1 -name 'stash-*.db.zst' -mmin +$((days * 1440)) -delete
echo "backup: $out ($(du -h "$out" | cut -f1); $counts); $(find "$dir" -maxdepth 1 -name 'stash-*.db.zst' | wc -l) copies kept"
