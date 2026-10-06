# Stash

A self-hosted bookmark manager you can use in the browser, from other computers through an API, and in
plain language from AI assistants such as Qwen Code or Claude Code through its MCP server.

![The Dashboard: tabs, coloured categories in columns, bookmarks with site icons](docs/screenshots/dashboard.png)

![My Bookmarks: every bookmark with its tags, notes and place, the tag list on the left](docs/screenshots/bookmarks.png)

<sub>Screenshots of a demo account (`node tools/screenshots.mjs` makes them again). A dark theme follows the
device setting.</sub>

**Everything stays in your hands.** Stash runs on your own server, and your bookmarks, browsing history and
change history live in your own database. You choose which language model, if any, gets to see them: point
your MCP client at a model on your own computer and no cloud service sees what you save, what you read or
what you ask. That is the big advantage of hosting it yourself: you get real use out of your browsing history
without handing it to anyone.

## What it consists of

1. **The server application** (`app/`, `static/`): a bookmark manager for the browser.
   - **Dashboard**: tabs with categories in columns and bookmarks inside them, arranged by dragging.
   - **Catalog**: a tagged store with fast search.
   - Import and export as a browser bookmarks file, sharing a tab by link, finding duplicates and dead links,
     a bookmarklet, a browser extension, two-factor authentication and invitation-based registration.
   - **Always current.** An open page redraws itself when the bookmarks change anywhere: in another tab or
     device, through an API key or MCP client, or in the background upkeep. It waits while you are typing, dragging
     or have a dialog open.
   - **Keeps itself tidy.** In the background Stash slowly re-checks bookmarks on its own, so you rarely have to
     run the duplicate or dead-link tools by hand (see *Background upkeep* below). Each account can switch this
     off in Settings.
   - The web UI and the browser extension speak **English, Finnish and Swedish** (Settings → Language, or the
     browser's language).
   - **API** (`/api/v1`) with API keys: all bookmarks at once, search, and changes with exact previews.
     Every change can be undone.
2. **stashai** (`client/`): an **MCP server** (`stashai mcp`) for Qwen Code, Claude Code, Gemini CLI and
   other MCP clients, plus terminal helpers. You give the client instructions in plain language ("move
   everything about company X to tag Y", "check whether these links still work"); its model studies the
   bookmarks with stashai's tools, can read web pages and your browsing history, and previews every change as
   an exact dry run. Nothing changes until you agree to the preview, and everything can be undone.
   See [`client/README.md`](client/README.md) for installing it and connecting a client.
3. **Browsing history sync** (`scripts/stash-history-sync.py`): a small script (macOS and Linux, plain
   Python 3) that sends Firefox's visit counts to Stash every few hours. Then the assistant knows which bookmarks you
   really use: it can bring the most used ones to the Dashboard, order bookmarks and categories by use, find
   often visited pages that are not bookmarked yet, and suggest cleaning up the ones unused for years. The
   history is stored only on your own server, is never part of the bookmark export and can be deleted per
   device in Settings. Before sending, query parameters that often carry secrets (token, session, code, …) are
   removed, and whole sites can be left out.

## Layout

| Path | Contents |
| --- | --- |
| `app/main.py` | FastAPI app: accounts, tabs, categories, bookmarks, search, sharing, import/export |
| `app/db.py` | SQLite schema and connections (`data/stash.db`, WAL) |
| `app/net.py` | Outgoing requests (dead links, titles, favicon cache), to public addresses only |
| `app/security.py` | Passwords (scrypt), sessions, TOTP, rate limiting |
| `app/api_v1.py` | `/api/v1` for API keys, plus managing keys and the change history |
| `app/history.py` | Browsing history sent by devices: upload, visit counts for bookmarks, search |
| `app/importer.py` | Reading and writing browser bookmark files (Netscape HTML) |
| `client/` | stashai: the MCP server and terminal helpers (its own Python package) |
| `scripts/stash-history-sync.py` | Sends Firefox history to Stash (served at `/dl/stash-history-sync.py`) |
| `static/` | The web UI: native ES modules, no build step |
| `extension/` | Browser extension (downloadable from the app at `/extension.zip`) |
| `data/` | Database and favicon cache: not in version control, back this up |

## Running it

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
STASH_ORIGIN=https://stash.example.com .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8003 --timeout-graceful-shutdown 5
```

Put a reverse proxy with HTTPS in front of it (for example Caddy or nginx). Open pages keep a server-sent events
stream (`/api/events`) open, so give uvicorn `--timeout-graceful-shutdown`: otherwise a restart waits for those
streams to end (they close and reconnect every five minutes). As a systemd user service:

```sh
systemctl --user status stash          # the service
systemctl --user restart stash         # after code changes (changes in static/ need no restart)
journalctl --user -u stash -f          # logs

.venv/bin/python -m app.cli invite                  # one-time invitation link (also needed for the first account)
.venv/bin/python -m app.cli users                   # accounts
.venv/bin/python -m app.cli reset-password NAME     # new random password, turns off 2FA
.venv/bin/python -m app.cli registration invite     # open | invite | closed
```

The first account is the administrator. By default you can only register with an invitation link; the
administrator creates invitations and changes the mode in Settings → Users and registration. To decide this in
the server configuration instead (for example in the systemd unit), set `STASH_REGISTRATION`:

| `STASH_REGISTRATION` | Who can create an account |
| --- | --- |
| `invite` | only people with an invitation link (the default when the variable is unset) |
| `open` | anyone, with just a username and a password: no email address or confirmation is needed |
| `closed` | nobody |

While the variable is set, the setting in Settings → Users and registration is shown but cannot be changed.

Environment variables: `STASH_ORIGIN` (public address), `STASH_DATA` (data directory, default `./data`),
`STASH_REGISTRATION` (see above), `STASH_MAINTENANCE` (`off` turns the background upkeep off; see below).

## Using Stash from an AI assistant (MCP)

stashai turns Stash into an MCP server, so Qwen Code, Claude Code, Gemini CLI or any other MCP client can
search, tidy and fix your bookmarks in plain language. On the computer where the client runs:

```sh
pipx install --force https://stash.example.com/dl/stashai-<version>-py3-none-any.whl   # Settings → API keys shows the exact command
stashai login https://stash.example.com      # paste an API key with "Allow changes"
stashai doctor                                # checks the connection and prints the client settings
```

Then register the server with your client. Qwen Code and Gemini CLI read it from `~/.qwen/settings.json` or
`~/.gemini/settings.json`:

```json
{ "mcpServers": { "stash": { "command": "stashai", "args": ["mcp"] } } }
```

Claude Code: `claude mcp add stash -- stashai mcp`. Now ask, for example "which bookmarks about Python have no
tags? suggest tags for them". The model finds bookmarks as named sets, every change is first shown as an exact
dry-run preview, and only `apply_changes` with that preview's id changes anything; it is refused if the
bookmarks changed after the preview. Every applied change can be undone. Keep `apply_changes` behind the
client's confirmation prompt. The full tool list and options are in [`client/README.md`](client/README.md).

## The API and stashai

Other computers and programs use Stash at `/api/v1` with an API key (`Authorization: Bearer stash_…`).
Keys are created in Settings → API keys; only a hash of the key is stored. A key can only read unless it is
allowed to make changes. `/api/v1` never accepts the session cookie, so it needs no CSRF header.

| Call | What it does |
| --- | --- |
| `GET /api/v1/me` | user, key name and rights, number of bookmarks |
| `GET /api/v1/snapshot` | all bookmarks at once + Dashboard structure + tags |
| `GET /api/v1/bookmarks?q=&tags=&mode=&host=&untagged=&scope=&ids=&limit=&offset=` | search (up to 1000 at a time) |
| `GET /api/v1/tags`, `GET /api/v1/structure` | tags with counts, tabs and categories |
| `POST /api/v1/changes` | `{"ops": [...], "summary": "...", "dry_run": true}`: changes in one transaction |
| `GET /api/v1/changes`, `POST /api/v1/changes/{id}/undo[?force=true]` | change history and undo |
| `POST /api/v1/favicons/refresh` | `{"ids": [...]}`: fetch the site icons of these bookmarks again, ignoring the cache (a key that can change; at most 60 sites per call) |
| `POST /api/v1/history/{device}` | `{"items": [...], "reset": true, "done": true}`: a device's browsing history (replaces its earlier one) |
| `GET /api/v1/history?q=&host=&min_visits=&period=&bookmarked=` | visited addresses, most visited first, with the bookmarks they match |
| `GET /api/v1/history/usage`, `GET /api/v1/history/sources`, `DELETE /api/v1/history/{device}` | visit counts of bookmarks, devices, deleting |

Operations: `add_tags`, `remove_tags`, `set_tags` (`ids`, `tags`), `rename_tag` (`old`, `new`; an empty `new`
removes the tag), `delete` (`ids`), `move` (`ids` + `category_id`, or `tab` + `category` which are created when
missing, or `catalog: true`), `update` (`id` + `title`/`url`/`notes`/`color`/`tags`), `create` (`url`, `title`,
`tags`, a place as for `move`), `order_bookmarks` (a category's bookmarks in the given order) and
`order_categories` (a tab's categories in order, each within its column). `dry_run` runs the operations and
rolls them back, so the preview (`diff`: each bookmark's tags, place, position and fields before and after) is
exactly what would happen. A real run stores a changeset (the latest 200) with the earlier state of every
touched bookmark and category; undoing it restores them, brings deleted bookmarks back and removes added ones.
If a later change touched the same bookmarks, undo needs `force=true`. Changes can also be seen and undone in
Settings → Changes made with API keys.

Site icons are a cache shared by all accounts. A refresh fetches only sites the caller has bookmarked, never
replaces a working icon with a failure, and changes the icon version the web UI loads icons with, so browsers
show a new icon at once instead of keeping their week-long cached copy.

## Background upkeep

Stash keeps bookmarks in order on its own, so you seldom need the duplicate or dead-link tools by hand. A slow
loop inside the server works through a small batch every few minutes:

- **Dead links.** A page that answers 404/410, or whose domain no longer resolves, is re-checked over several
  rounds; once it has failed a few times in a row it gets the `dead-link` tag, and the tag is removed again as
  soon as the page answers normally. A link that only times out, or is behind a login, is never tagged, so the
  mark stays trustworthy. The dead state is stored per bookmark.
- **Duplicates.** When two bookmarks share the same address, every copy after the oldest keeps the `duplicate`
  tag. The tag is kept in step with the bookmarks and clears by itself once the extra copies are gone.
- **Titles and icons.** A bookmark whose title is empty or just its own address is given the page's real title,
  and site icons are fetched again. A title you have written is left untouched, and a page the server cannot
  reach from where it runs keeps whatever title it already has.

These edits are made directly (like the manual tools), so they do not fill the undo history. Each account can
turn the whole thing off in Settings → Bookmarks, and `STASH_MAINTENANCE=off` disables it for the whole server.

Browsing history is matched to bookmarks loosely (http/https, `www.` and a trailing slash do not matter). Each
device sends visit counts per address (last 30, 90 and 365 days, and all), and a new sync replaces that
device's earlier rows.

`client/` is **stashai** (see [`client/README.md`](client/README.md)). The package is built with
`client/.venv/bin/pip wheel --no-deps -w client/dist client/`, and Stash serves it at
`/dl/stashai-<version>-py3-none-any.whl` (Settings shows the current `pipx install` command).

Sending the history from the Mac (or Linux computer) where Firefox is used:

```sh
curl -o ~/stash-history-sync.py https://stash.example.com/dl/stash-history-sync.py
python3 ~/stash-history-sync.py --setup       # Stash address, an API key that can change, a name for this computer
python3 ~/stash-history-sync.py --dry-run     # shows what would be sent
python3 ~/stash-history-sync.py --schedule    # sends every 6 hours (launchd / cron)
```

## Tests

```sh
.venv/bin/python tests/smoke.py    # the web API end to end, throwaway database
.venv/bin/python tests/api_v1.py   # API keys, /api/v1, previews and undo
.venv/bin/python tests/history.py  # the sync script against a fake Firefox profile, the history API, ordering
client/.venv/bin/python -m pytest -q client/tests   # stashai: search, sets, previews, web, MCP tools
node tests/ui.mjs                  # headless Chromium, its own server on port 8013, screenshots in data/tmp/
node tools/check-i18n.mjs          # missing or broken Finnish and Swedish translations
node tools/screenshots.mjs         # the README screenshots, from a demo instance on port 8014
```

## Languages

UI strings are written in English in the code (`t('…')`); `static/js/i18n.js` maps them to Finnish and
`static/js/i18n-sv.js` to Swedish, and `extension/_locales/` holds the extension's texts. A new string needs
both translations, which `node tools/check-i18n.mjs` checks (placeholders such as `{n}` included). The
repository, the API, stashai and the scripts are in English only.

## Security in brief

- The session cookie is `HttpOnly; Secure; SameSite=Lax`; every state-changing call of the web API needs the
  `X-Stash` header (CSRF).
- The CSP allows only same-origin scripts and styles; bookmark addresses may be http(s), ftp, mailto and tel.
- Requests made by the server resolve the target first and refuse internal network and localhost addresses.
- The bookmarklet and the extension pass page details in the URL's `#` part, which never reaches server logs;
  configure the reverse proxy to log without query strings and cookies.
- API keys and the browsing history belong to one account and are deleted with it.
