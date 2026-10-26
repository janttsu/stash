# Handover

The state of Stash for whoever continues the work: a person or an assistant in a new session. Keep this file
current: update it in the same commit as any change that affects how the project is run, built or extended.
Last updated 2026-10-26.

## What this is

A self-hosted bookmark manager (FastAPI + SQLite + plain ES modules, no build step) with an MCP server for AI
assistants. Beside bookmarks it now holds three more things, each its own section in the top bar or account
menu: **Open tabs** (a browser's open tabs sent here to be sorted out later), **Playlists** (Spotify and YouTube
playlists copied in, edited as pending changes, pushed back) and **Handovers** (versioned project handover texts
saved by assistants over MCP). README.md describes every feature for users; this file is about the work.

## Where it runs

- Production: `stash.example.org`, served by Caddy in front of a systemd **user** unit `stash.service`
  (uvicorn on 127.0.0.1:8003) that runs the `main` checkout in `~/stash` directly. `systemctl --user restart stash`
  after code changes; `journalctl --user -u stash -f` for logs. Changes under `static/` need no restart.
- Repository: `git@github.com:janttsu/stash.git`, branch `main`. Features are developed in worktrees under
  `.claude/worktrees/<name>` on branches `worktree-<name>` and fast-forwarded into `main`.
- Database: `data/stash.db` (WAL). `scripts/backup.sh` (unit `stash-backup`, hourly timer) writes compressed
  copies to `data/backups/`; run `systemctl --user start stash-backup` before a deploy that changes the schema.
- E-mail (password reset links) is sent directly to the recipient's MX, DKIM-signed. Production shares the
  key and DNS records of another app on the same server through the environment of `stash.service`
  (`STASH_MAIL_FROM`, `STASH_MAIL_HELO`, `STASH_DKIM_KEY`, `STASH_DKIM_SELECTOR`); the repository has no domain
  names in it. `python -m app.cli mail-dns` prints the records a fresh setup would need.

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

## State of the parts (2026-10-26)

- **Accounts, e-mail, password reset**: done, tests in `tests/account.py`.
- **Open tabs** (`app/tabs.py`, `static/js/tabs.js`, extension menu "Send all open tabs to Stash" which opens
  `/add#tabs=…`): done, tests in `tests/mcp/test_tabs.py`. Saving from MCP makes bookmarks through the normal
  change pipeline, so it is undoable; saving from the page inserts bookmarks directly like the Add page does.
- **Playlists** (`app/playlists.py`, `static/js/playlists.js`): done and tested against fake Spotify and
  YouTube (`tests/mcp/test_playlists.py`), but not yet exercised against the real services. First real use
  should check: the OAuth callback round trip through Caddy, Spotify's `/tracks` endpoints on a playlist of
  more than 100 items, YouTube quota use on a push, and that a Google app in *Testing* really needs a new
  connection after seven days. Tokens are refreshed when expired; a failed refresh asks the user to connect
  again. Items with no id (Spotify local files) are skipped.
- **Handovers** (`app/handover.py`, `static/js/handover.js`): done, tests in `tests/mcp/test_handover.py`.
- **Dashboard "bookmarks fade with disuse"** was added and then reverted on request; the `used_at` column it
  added may still exist in older databases and is unused.
- The browser extension zip is built by the server from `extension/` on each download; after changing the
  extension users must download and reinstall it.

## Next steps that were discussed or seem likely

- Try Playlists against real Spotify and YouTube accounts and fix what the fakes did not predict.
- Tags, notes or a "why" on open tabs could be richer; the assistant currently gets one note field.
- `tests/ui.mjs` could be run from a laptop against the server before releases, since it cannot run there.
