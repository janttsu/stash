# Handover

The state of Stash for whoever continues the work: a person or an assistant in a new session. Keep this file
current: update it in the same commit as any change that affects how the project is run, built or extended.
Last updated 2026-10-26.

## What this is

A self-hosted bookmark manager (FastAPI + SQLite + plain ES modules, no build step) with an MCP server for AI
assistants. The top bar has six sections: **Dashboard**, **My Bookmarks**, **Open tabs** (a browser's open
tabs sent here to be sorted out later), **Playlists** (Spotify and YouTube playlists copied in, edited as pending
changes, pushed back), **Subscriptions** (YouTube channels, Spotify followed artists and playlists, with tags,
notes and pending subscribe/unsubscribe) and **AI** (`static/js/ai.js`: connecting an assistant with keys and rules, the versioned
**Handovers** assistants save over MCP, and the undoable changes made with API keys). Settings keeps the rest.
README.md describes every feature for users; this file is about the work.

## Where it runs

- Production: the owner's own server (its name is not in this repository), Caddy with HTTPS in front of a
  systemd **user** unit `stash.service` (uvicorn on 127.0.0.1:8003) that runs the `main` checkout in `~/stash`
  directly. `systemctl --user restart stash`
  after code changes; `journalctl --user -u stash -f` for logs. Changes under `static/` need no restart.
- Repository: `git@github.com:janttsu/stash.git`, branch `main`. Features are developed in worktrees under
  `.claude/worktrees/<name>` on branches `worktree-<name>` and fast-forwarded into `main`.
- Database: `data/stash.db` (WAL). `scripts/backup.sh` (unit `stash-backup`, hourly timer) writes compressed
  copies to `data/backups/`; run `systemctl --user start stash-backup` before a deploy that changes the schema.
  These copies are also the users' **version history** (`app/versions.py`, Settings → Version history): a user
  restores their own rows by area; a restore first runs backup.sh. A new table with user data must be added to
  `versions.AREAS` (and to `_counts`), or it will not be restorable.
- Scheduled tasks (`app/jobs.py`, units in `scripts/systemd/`, installed in `~/.config/systemd/user/`, linger on):
  `stash-backup` hourly, `stash-media-sync` every 6 h (import only, 40 changed playlists per account and service
  per run, a service's long 429 pause is respected), `stash-duplicates` daily at 04:40. Each run writes
  `config` key `job:<name>`; `/api/me` gives administrators `jobs_overdue`, shown as a banner. `app.cli jobs`
  lists them on the server.
- E-mail (password reset links) is sent directly to the recipient's MX, DKIM-signed. Production shares the
  key and DNS records of another app on the same server through the environment of `stash.service`
  (`STASH_MAIL_FROM`, `STASH_MAIL_HELO`, `STASH_DKIM_KEY`, `STASH_DKIM_SELECTOR`); the repository has no real
  domain names, host names, account ids or project numbers in it, and its history was rewritten on 2026-10-27 to
  take out the ones that had slipped in. Keep it that way: fixtures use example domains and made-up numbers. `python -m app.cli mail-dns` prints the records a fresh setup would need.

## How to work on it

```sh
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python tests/smoke.py; .venv/bin/python tests/api_v1.py; .venv/bin/python tests/trash.py
.venv/bin/python tests/account.py; .venv/bin/python tests/history.py; .venv/bin/python tests/maintenance.py
.venv/bin/python -m pytest -q tests/mcp          # MCP tools, open tabs, playlists (fake services), handovers
node tools/check-i18n.mjs                        # every UI string needs Finnish and Swedish
node --check static/js/<file>.js                 # no bundler, so at least a syntax check
```

- `node tests/ui.mjs` (headless Chromium) times out on the production server itself: Chromium takes ~15 s to
  start there and the test allows 90 s for the whole run. It works on a normal computer. Do not read a timeout
  there as a regression.
- Code, comments, commit messages, API and MCP text are English. UI strings are English keys translated in
  `static/js/i18n.js` (fi) and `static/js/i18n-sv.js` (sv); the extension's texts in `extension/_locales/`.
- Commit messages are plain descriptions without tool or AI attribution lines.
- Schema changes go into `db.SCHEMA` as `CREATE TABLE IF NOT EXISTS` plus an `ALTER TABLE` guard in
  `db.init()` for columns added later; `init()` runs at start, so production migrates itself on restart.
  Never a migration that deletes.
- New web routes: a module with `session_api = APIRouter(prefix="/api/<thing>")` imported and included at the
  end of `app/main.py` (see `handover.py`, `tabs.py`, `playlists.py`). `/api/tabs` is taken by the Dashboard's
  tabs, which is why open tabs live at `/api/open-tabs`.
- New MCP tools: a method on `Session` (`app/mcpserver/session.py`) returning text, a `LocalAPI` method
  (`app/mcpserver/local.py`) that opens the connection and calls the module function (with `self._writer(c)`
  when the key must be allowed to change things), and the `@mcp.tool()` in `app/mcpserver/server.py`. Keys
  are checked on every call; `KeyGate` refuses requests without one before they reach the MCP machinery.
- New pages: a module exporting `render<Thing>(container, params)`, a route in `static/js/app.js`, styles in
  `static/css/app.css`. Modals: `openModal(title, body, buttons)` returns a promise; a button with
  `kind: 'primary'` and an `action` closes on a non-false return.
- MCP tests share one session per API key, so a test that makes sets (S1, S2) should use its own key
  (`new_key(stash)`), or it shifts the numbering for the other test files.

## State of the parts (2026-10-27)

- **Accounts, e-mail, password reset**: done, tests in `tests/account.py`.
- **Open tabs** (`app/tabs.py`, `static/js/tabs.js`, extension menu "Send all open tabs to Stash" which opens
  `/add#tabs=…`): done, tests in `tests/mcp/test_tabs.py`. Saving from MCP makes bookmarks through the normal
  change pipeline, so it is undoable; saving from the page inserts bookmarks directly like the Add page does.
- **Playlists** (`app/playlists.py`, `static/js/playlists.js`): done and tested against fake Spotify and
  YouTube (`tests/mcp/test_playlists.py`). YouTube was connected for real on 2026-10-27 for the owner's
  account: the OAuth round trip through Caddy works. What the real setup taught (now in the page's guide and
  README): the consent screen must exist before a client can be made; only *Test users* worked, publishing
  the app did not; a Desktop-app client is the wrong kind; the YouTube Data API must be enabled in the client's
  own project, else the connection succeeds and fetching fails; that case is now recognised
  (`problem_code()` → `youtube_api_disabled:<project>`) and the page shows a notice with the direct link. A
  fetch reports progress through `GET /api/playlists/accounts/<service>/progress` (in-memory, one process)
  and returns a summary (`total`, `fetched`, `items`, `skipped_with_local_changes`, `failed`). `sync_all()`
  (`POST …/sync-all`, the page's *Synchronise*, MCP `playlists_sync(push_pending=true)`) pushes every dirty
  playlist first (progress phase `push`) and adds `pushed` and `push_failed`. **Google Takeout import**
  (`import_takeout`, `POST /api/playlists/takeout`, 2026-10-27): Watch later is not in YouTube's API, so
  Takeout CSVs become `playlists.local=1` rows (external_id `stash:takeout:<name>`): edits apply at once,
  push/discard refuse, account fetches skip them, a single fetch re-reads titles (`refresh_local`). Not yet
  tried with the owner's real Takeout file: check the header names of a Finnish export. Also since 2026-10-27:
  429 handling with Retry-After (`_Client.send`, `MIN_INTERVAL` pacing for Spotify), YouTube titles from
  videos.list with `playlist_items.unavailable` keeping the last real title. **Spotify's February 2026
  Development Mode changes** (developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
  broke the first real Spotify fetch (every `/playlists/{id}/tracks` → 403): now `/items` with `item`,
  `spotify_total()` for the renamed size field, per-track `/tracks/{id}` (no `/tracks?ids=`), `POST
  /me/playlists`, follow/unfollow via `PUT/DELETE /me/library?uris=`. Others' playlists give no contents:
  stored metadata-only (`metadata_only` in the fetch summary). The test fake answers 403 on the removed
  paths. Still to check: a real Spotify push and create, artist follow via /me/library (undocumented),
  YouTube quota use on a push, and the 7-day end of a *Testing* connection.
  Tokens are refreshed when expired; a failed refresh asks the user to connect again. Items with no id
  (Spotify local files) are skipped. **Deleting a playlist** (2026-10-27): `playlists.deleting=1`
  (`mark_delete`, `POST /lists/{id}/delete` and `/keep`, MCP `playlist_delete`) greys the list out; `push()` of
  such a playlist calls `_delete_remote` (YouTube `DELETE /playlists?id=`, Spotify `DELETE /me/library?uris=`)
  and drops the row; `sync_all` includes marked playlists and reports them in `deleted`. A Stash-only playlist
  is forgotten at once. Test: `test_delete_marked_playlists_at_push_or_sync`. **Publishing a Stash-only
  playlist to YouTube** (2026-10-27): `publish()` (`POST /lists/{id}/publish`, MCP `playlist_publish`) creates
  the playlist on YouTube and turns the row into a synced one with every item `added`; `adopt(local, into)`
  (no UI; for a script) instead turns an existing YouTube playlist into the Stash-only one (rename + diff),
  which saves quota when they overlap. YouTube writes cost 50 units each (`YOUTUBE_WRITE_COST`), so `push()`
  of a YouTube list refuses more than `YOUTUBE_PUSH_MAX` writes unless given a `budget`; with one it runs
  `_push_youtube_batch` (rename, removals, additions appended in order, each recorded at once, no reordering,
  fetch back when complete; a 4xx for one video drops it and reports it, a 5xx ends the batch with the video
  still pending: the first real run met a passing 503 from YouTube). `push_batch(c, budget)` is what the daily `youtube-push` job runs
  (`jobs.youtube_push`, `PUSH_BATCH`=160, `scripts/systemd/stash-youtube-push.*` at 10:30): dirty playlists
  first, then the deletions of marked playlists once nothing is pending, then (`jobs.youtube_push`) the pending
  subscribes/unsubscribes when the rest of the budget covers them all. `/c/<name>` and `/user/<name>`
  addresses are resolved as a handle first, then as a legacy username (`subscriptions._resolve`). A
  *quotaExceeded* 403 from YouTube is
  turned into the rate-limit pause until the next reset (`youtube_quota_reset`, midnight Pacific time).
- **Subscriptions** (`app/subscriptions.py`, `static/js/subscriptions.js`, 2026-10-27): uses Playlists'
  connections, HTTP client (`_api`, retries) and progress record (key `(uid, "<service>:subscriptions")`).
  Tested against fakes (`tests/mcp/test_subscriptions.py`), not yet against the real services. Spotify's
  scope gained `user-follow-read user-follow-modify`; older connections get `reconnect_needed:spotify`.
  YouTube has no API for playlists saved from others, so those are `state='local'` entries, never pushed.
  To check on first real use: the subscriptions list order/paging on a large account, and the quota of
  the activity check (1 unit per channel). The page groups the list by tag by default (`grouped()` in
  `subscriptions.js`, one `<details>` per tag, open state and the chosen view remembered in localStorage,
  2026-10-27): tags are the categories, one topic tag per channel is the intended use. Since 2026-10-27 `media-sync` also runs the activity check in the
  background (`jobs._stale_activity`: channels not checked for `ACTIVITY_EVERY`, at most `ACTIVITY_BATCH`=200
  per account per run, oldest first; `tests/mcp/test_subscriptions.py::test_scheduled_activity_check`).
- **Handovers** (`app/handover.py`, `static/js/handover.js` as a card on the AI page): done, tests in
  `tests/mcp/test_handover.py`; checked live on production with a throwaway account on 2026-10-26.
- **Live redraw of every page** (2026-10-27): `users.rev` is bumped by triggers (`db.REV_TRIGGERS`) and open
  pages watch it through `/api/events`; `app.js` `scheduleRedraw()` re-routes any page other than the dashboard
  and bookmarks. Until 2026-10-27 only bookmarks, tags, categories, tabs and trash had triggers, so the
  Playlists, Subscriptions, Open tabs and AI pages did not follow changes made elsewhere; now `playlists`,
  `playlist_items`, `subscriptions`, `open_tab_lists`, `open_tabs`, `handovers` and `handover_versions` bump
  it too (`tests/smoke.py`). A fetch of a big playlist bumps it per row, which is at most one redraw per
  `EVENT_POLL`; the busy check in `app.js` still holds a redraw while a dialog is open or a field is focused.
- **AI section** (`static/js/ai.js`): the assistant card and change history moved here from Settings
  (`settings.js` exports them as `assistantCard()` and `changeHistoryCard()`); Settings → Assistants and API
  only points here. `#/handovers` still routes to the AI page.
- **Dashboard "bookmarks fade with disuse"** was added and then reverted on request; the `used_at` column it
  added may still exist in older databases and is unused.
- The browser extension zip is built by the server from `extension/` on each download; after changing the
  extension users must download and reinstall it, and `"version"` in `extension/manifest.json` goes up.
  Release Firefox only installs it signed: `/extension.xpi` serves `data/stash-extension.xpi` when the owner has
  signed one on AMO (README, *Browser extension*), otherwise an unsigned xpi. No signed file exists yet.

## Next steps that were discussed or seem likely

- Playlists and Subscriptions have now run against the owner's real YouTube and Spotify accounts (fetch,
  Takeout-style Stash-only lists, the activity check, a single unsubscribe); a real push of playlist
  changes, a playlist create and a playlist deletion on the services have not been exercised yet (the
  owner's Stash-only lists are to be published to YouTube on request: create each on the service, add the
  videos, then delete the old lists that are marked for deletion). Spotify's long pause ends on 2026-10-28.
- A YouTube watch-history import (Google Takeout's history file) was discussed: it would tell which
  channels are watched most, to tidy playlists and subscriptions by use.
- Tags, notes or a "why" on open tabs could be richer; the assistant currently gets one note field.
- `tests/ui.mjs` could be run from a laptop against the server before releases, since it cannot run there.
