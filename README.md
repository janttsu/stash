# Stash

A self-hosted bookmark manager you can use in the browser, from other computers through an API, and in
plain language from AI assistants such as Claude Code, Crush or Qwen Code through its MCP server.

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
   - **Open tabs**: a browser's open tabs sent to Stash to be sorted out later, so the browser can be emptied
     with nothing lost (see *Open tabs* below).
   - **Playlists**: your Spotify and YouTube playlists copied into Stash, edited here (by hand or by the
     assistant) and pushed back to the service; the copy is also a backup (see *Playlists* below).
   - **Subscriptions**: YouTube channel subscriptions and Spotify's followed artists and playlists, with your
     own tags and notes, a check for channels that have gone quiet, and subscribing or unsubscribing as pending
     changes (see *Subscriptions* below).
   - **AI**: one section for everything about assistants and the tools that work with Stash from outside:
     connecting one (the MCP address, keys and your standing rules), what it can do, the handovers it saves,
     the changes it made (each undoable), and **Tools**: a list of every tool with where it is set up, the link
     check with a local model, the browsing history sync and the API for your own programs.
   - **Trash.** Deleted bookmarks wait 30 days in the trash (account menu → Trash), whichever way they were
     deleted: one at a time, many at once, with a whole category or tab, or through an API key. Every deletion
     shows an Undo button, and restoring puts a bookmark back where it was, making its category again if needed.
   - **Always current.** An open page redraws itself when the bookmarks change anywhere: in another tab or
     device, through an API key or MCP client, or in the background upkeep. It waits while you are typing, dragging
     or have a dialog open.
   - **Keeps titles and icons up to date.** In the background Stash gives unnamed bookmarks their page's real
     title and fetches site icons again (see *Background upkeep* below). Each account can switch this off.
   - The web UI and the browser extension speak **English, Finnish and Swedish** (Settings → Language, or the
     browser's language).
   - **API** (`/api/v1`) with API keys: all bookmarks at once, search, and changes with exact previews.
     Every change can be undone.
2. **An MCP endpoint** (`/mcp`, in `app/mcpserver/`) for AI assistants such as Claude Code, Crush, Qwen Code and
   Gemini CLI. You ask in plain language ("move everything about company X to tag Y", "check whether these
   links still work"); the assistant studies the bookmarks with Stash's tools, can read web pages and your
   browsing history, and previews every change as an exact dry run. Nothing changes until you agree to the
   preview, and everything can be undone. Nothing has to be installed: the assistant connects to your Stash
   with an API key (see *Using Stash from an AI assistant* below).
3. **Browsing history sync** (`scripts/stash-history-sync.py`): a small script (macOS and Linux, plain
   Python 3) that sends Firefox's visit counts to Stash every few hours. Then the assistant knows which bookmarks you
   really use: it can bring the most used ones to the Dashboard, order bookmarks and categories by use, find
   often visited pages that are not bookmarked yet, and suggest cleaning up the ones unused for years. The
   history is stored only on your own server, is never part of the bookmark export and can be deleted per
   device in AI → Tools. Before sending, query parameters that often carry secrets (token, session, code, …) are
   removed, and whole sites can be left out.
4. **Link check with a local model** (`scripts/stash-linkcheck.py`): opens each bookmark in a headless Chromium
   on your own computer, clicks cookie banners and age confirmations away, and lets a model in Ollama decide
   whether the content still exists ("video removed", "not available", 404, a parked domain). It lists the
   gone ones and removes them through MCP only after you agree (see *Checking links with a local model*).

## Layout

| Path | Contents |
| --- | --- |
| `app/main.py` | FastAPI app: accounts, tabs, categories, bookmarks, search, sharing, import/export |
| `app/db.py` | SQLite schema and connections (`data/stash.db`, WAL) |
| `app/net.py` | Outgoing requests (dead links, titles, favicon cache), to public addresses only |
| `app/security.py` | Passwords (scrypt), sessions, TOTP, rate limiting |
| `app/api_v1.py` | `/api/v1` for API keys, plus managing keys and the change history |
| `app/history.py` | Browsing history sent by devices: upload, visit counts for bookmarks, search |
| `app/mailer.py` | E-mail from the server: password reset links, signed with DKIM |
| `app/handover.py` | Project handovers saved by assistants through MCP: versions, the web view |
| `app/tabs.py` | Open browser tabs sent to Stash: lists, states, saving as bookmarks |
| `app/playlists.py` | Spotify and YouTube playlists: OAuth, fetching, pending edits, pushing back |
| `app/subscriptions.py` | YouTube channel subscriptions, Spotify followed artists and playlists: fetch, tags, push |
| `app/importer.py` | Reading and writing browser bookmark files (Netscape HTML) |
| `app/mcpserver/` | The MCP endpoint `/mcp`: its tools, named result sets, previews and page reading |
| `scripts/stash-history-sync.py` | Sends Firefox history to Stash (served at `/dl/stash-history-sync.py`) |
| `scripts/stash-linkcheck.py` | Finds bookmarks whose content is gone with a local model (served at `/dl/stash-linkcheck.py`) |
| `static/` | The web UI: native ES modules, no build step |
| `extension/` | Browser extension (downloadable from the app at `/extension.zip`) |
| `data/` | Database, favicon cache and `backups/`: not in version control |
| `scripts/backup.sh` | Hourly compressed copy of the database, 30 days kept (see *Backups*) |
| `handover.md` | The state of the work for whoever continues it: how it runs, conventions, what is done and what is next |

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

## Browser extension

Settings → Add-to-Stash buttons → **Download extension** gives a zip of `extension/` with this server's address
written into it. It adds a toolbar button, a right-click *Add link to Stash*, *Bookmark all open tabs* and
*Send all open tabs to Stash* (to Open tabs, every window at once).

- **Chrome, Edge, Brave**: unzip, open the Extensions page, turn on Developer mode, *Load unpacked*, choose the
  folder.
- **Firefox** installs only add-ons signed by Mozilla for good; an unsigned one is reported as "corrupt" even
  though the file is fine. To try it, `about:debugging` → *This Firefox* → *Load Temporary Add-on* → the
  `manifest.json` of the unzipped folder (it disappears when Firefox restarts). For a permanent install, sign it
  once as a self-distributed ("unlisted") add-on; this needs a free account at addons.mozilla.org and its API
  keys (Tools → Manage API Keys), and Node on any computer:

  ```sh
  curl -o stash-extension.zip https://stash.example.com/extension.zip && unzip -q stash-extension.zip
  npx web-ext sign --source-dir stash-extension --channel unlisted --api-key <JWT issuer> --api-secret <JWT secret>
  # → web-ext-artifacts/…-1.2.0.xpi: install it in Firefox (File → Open, or drag it in), and put it on the
  #   server as data/stash-extension.xpi so that /extension.xpi (Settings → "Download for Firefox") serves it
  ```

  Every changed version of the extension needs a higher `"version"` in `extension/manifest.json` and a new
  signing. If AMO says the add-on id is taken, change `browser_specific_settings.gecko.id` in the manifest to
  one of your own before signing. Without a signed file, `/extension.xpi` serves an unsigned one, which Firefox
  Developer Edition, Nightly and ESR accept after `xpinstall.signatures.required` is set to false in
  `about:config`.

## Backups

`scripts/backup.sh` writes a consistent, zstd-compressed copy of the whole database (every account's bookmarks,
tabs, categories, tags, trash, settings and change history) to `data/backups/stash-YYYY-MM-DD_HHMM.db.zst`, checks
it with `PRAGMA integrity_check`, and deletes copies older than 30 days (`STASH_BACKUP_DAYS`). It is safe while
Stash runs. Run hourly, that is a version history of 720 copies, about 1 MB each for a few thousand bookmarks.
It needs `sqlite3` and `zstd`. As systemd user units:

```ini
# ~/.config/systemd/user/stash-backup.service
[Unit]
Description=Stash: database backup

[Service]
Type=oneshot
ExecStart=%h/stash/scripts/backup.sh

# ~/.config/systemd/user/stash-backup.timer
[Unit]
Description=Stash: database backup every hour

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now stash-backup.timer
systemctl --user start stash-backup        # one backup now
ls data/backups/                           # the copies
```

The copies stay on the same disk, so also back up `data/` off the server (for example with restic).

To return the whole Stash to an earlier hour, stop it and put the copy in place:

```sh
systemctl --user stop stash
cp data/stash.db data/stash-before-restore.db
zstd -d -f data/backups/stash-2026-10-08_1100.db.zst -o data/stash.db && rm -f data/stash.db-wal data/stash.db-shm
systemctl --user start stash
```

To look at or take back only some bookmarks, open a copy beside the live database instead
(`zstd -d data/backups/… -o /tmp/old.db`, then `sqlite3 /tmp/old.db`).

## Using Stash from an AI assistant (MCP)

Stash is an MCP server at `https://<your stash>/mcp` (streamable HTTP). An assistant such as Claude Code, Qwen
Code or Gemini CLI connects to it with an API key and can then search, tidy and fix your bookmarks in plain
language. Nothing is installed on your computer.

The easy way is AI → Assistant (MCP) → **Connect an assistant**: choose your assistant, and Stash creates a
key and shows the finished setup with the key in it. The same card explains how to use it, lists your keys with
their last use, lets you switch a key between read-only and read-and-change, revoke it, and write standing rules
for the assistant ("recipes always have the tag food").

The setups it shows, with your own address and key:

```sh
# Claude Code
claude mcp add --transport http stash https://stash.example.com/mcp --header "Authorization: Bearer stash_…"
```

```json
// Qwen Code (~/.qwen/settings.json) and Gemini CLI (~/.gemini/settings.json)
{ "mcpServers": { "stash": { "httpUrl": "https://stash.example.com/mcp",
                             "headers": { "Authorization": "Bearer stash_…" } } } }
```

```sh
# Crush (~/.config/crush/crushrc, or .crushrc in a project)
mcp add stash --type http --url https://stash.example.com/mcp --header Authorization "Bearer stash_…"
```

Then ask, for example "which bookmarks about Python have no tags? suggest tags for them".

| Tool | What it does |
| --- | --- |
| `overview` | tags with counts, tabs and categories, the size of the collection, your rules, the sets made so far |
| `search` | finds bookmarks by words, regex, site, tags, tab, category, dates or use, and keeps them as a set (S1, S2, …) |
| `show`, `combine` | list a set; union, intersection or difference of two sets |
| `list_urls` | the whole addresses of a set as JSON with their tags and the tag vocabulary, for programs that open the pages themselves |
| `browsing_history` | visited addresses from your synced Firefox history, also those not bookmarked |
| `read_page` | reads one page as text |
| `check_links` | checks every link of a set: alive, moved, dead, unclear |
| `refresh_titles` | reads the pages' real titles and previews giving them to the bookmarks |
| `refresh_icons` | fetches the site icons again (a server cache, done at once) |
| `preview_changes` | runs changes as a dry run and returns the exact diff with a preview id (P1, P2, …) |
| `apply_changes` | applies a preview you agreed to |
| `recent_changes`, `undo` | the latest changes and taking one back |

How it stays safe:

- **Sets by name, not lists of ids.** Every search makes a set and changes refer to it by name. Stash expands
  names into ids itself, so the model cannot lose or invent bookmarks, and long lists never pass through it.
- **Preview, then apply.** `preview_changes` is an exact dry run. `apply_changes` only takes a preview id and
  runs the dry run once more first: if the bookmarks changed after the preview, nothing is applied. Keep
  `apply_changes` behind your assistant's confirmation prompt.
- **Undoable.** Every applied preview is one changeset (AI → Changes made with API keys), and deleted
  bookmarks also wait 30 days in the trash.
- **Keys.** Each request needs an API key; a read-only key can look and preview but not apply. A key revoked in
  Settings stops working at once. The endpoint answers only for this server's own address.
- **Untrusted pages.** Page text and titles are data, never instructions. Pages are fetched by the server, never
  from its own addresses or private networks. A site that refuses the server's location cannot be read.

A key's sets and previews are kept in memory for an hour after its last use, so a conversation can build on them.

### Open tabs

A browser with two hundred open tabs is hard to close: some of them are things left unfinished. **Open tabs**
in the top bar keeps them. Send them with the extension (its toolbar button's menu → *Send all open tabs to
Stash*, every window at once) or paste their addresses (Open tabs → *Paste a list…*; without the extension,
Firefox can bookmark all tabs into a folder with Ctrl+Shift+D, and copying that folder's bookmarks in the
Library, Ctrl+Shift+O, Ctrl+A, Ctrl+C, gives their addresses one per line), then close them in the browser. Every sending is a list,
and each tab in it is *open* (still to look at), *saved* (now a bookmark), *done* or *dropped*. A tab whose
address is already bookmarked is marked saved at once.

The sorting is meant for the assistant: "sort out the tabs I sent yesterday". With these tools it saves the
ones worth keeping as bookmarks (in a Dashboard category or the Catalog, as one undoable change), drops search
result pages, duplicates and the like, marks finished things done, and leaves the real work in progress open
with a note on what was unfinished, so you only look through those. The page shows the same lists and lets you
save, drop or finish tabs by hand, one at a time or many at once.

| Tool | What it does |
| --- | --- |
| `open_tabs_overview` | the sent lists with how many tabs in each are open, saved, done and dropped |
| `open_tabs_list` | the tabs of a list or all of them (T1, T2, …), by state, words or site |
| `open_tabs_save` | makes bookmarks of the given tabs (`tags`, `tab` and `category`, or the Catalog) as one undoable change, and marks them saved |
| `open_tabs_resolve` | marks tabs done or dropped, or leaves them open with a note for you |

The lists are kept apart from bookmarks: a list can be deleted without touching the bookmarks made from it, and
only the bookmarks made from it take part in previews, undo and the trash.

### Playlists

**Playlists** in the top bar holds your Spotify and YouTube playlists as a copy in Stash's database. You edit
the copy (add by link, remove, drag to reorder, rename; or ask the assistant: "move every live recording from
Favourites to a new playlist Live") and the changes wait as *pending* until you press **Push**; only then does
the service change. **Fetch playlists** brings the service's state in; a playlist with pending changes is left
alone until they are pushed or discarded. The page has one tab per service. Every fetch takes a YouTube
video's title from the video itself, so a renamed video is renamed in Stash; a video that has been deleted or
made private keeps the last title Stash saw, marked as such, instead of YouTube's "Deleted video". When a
service answers *429 Too Many Requests*, Stash waits as long as it asks (shown on the progress bar) and goes on;
a longer pause (Spotify gives apps that made too many requests one of about a day) ends the fetch: the page says
until when, and Stash sends nothing to that service before then, since requests during the pause can lengthen it.
A Spotify fetch skips playlists whose snapshot_id has not changed, so a routine fetch is only a few requests.

YouTube's *Watch later* cannot be read through its API (Google closed it, and the history, in 2016), but
Google Takeout exports it. On the YouTube tab, **Import from Google Takeout…** shows the steps: at
takeout.google.com deselect all, tick *YouTube and YouTube Music*, keep only *playlists*, create the export,
unzip it, and choose `Watch later-videos.csv` (and any other playlist files) from
`YouTube and YouTube Music/playlists/`. Each file becomes a playlist kept **only in Stash**: titles, channels and
lengths are read from YouTube (1 quota unit per 50 videos), edits take effect at once, nothing is ever sent to
YouTube, account fetches leave it alone, *Read the titles again* refreshes them, and importing the same file
again replaces its contents. Both the older Takeout format ("Video Id, Time Added" after a few lines about the
playlist) and the newer one ("Video ID, Playlist Video Creation Timestamp") are read.

**Synchronise** does both ways for a whole service: it pushes the
pending changes of every playlist (after asking, and naming them), then fetches everything back; a push that
fails is listed and its changes stay pending. Right after connecting, the first fetch starts by itself. A fetch
shows a progress bar (playlist *n* of *m*, its name, items so far) and ends with a summary: how many playlists
of how many were fetched, how many items, which were left alone and which failed and why; a playlist the
service refuses does not stop the others. A playlist deleted on the service stays in Stash marked *gone*, and
**Export all** downloads everything as JSON, so the copy doubles as a backup.

Each account connects its own developer app for each service (Stash never ships one): Playlists → **Connect…**
shows the steps for Spotify (developer.spotify.com/dashboard → Create app → Web API → redirect URI → Client ID
and secret) and for YouTube, and the exact redirect URI to paste:
`https://<your stash>/api/playlists/accounts/<service>/callback`. The app's client id and secret and the tokens
are stored in the database as they are.

For YouTube, everything happens in one Google Cloud project:

1. Enable **YouTube Data API v3** (APIs & Services → Library). If it is missing, connecting works but Google
   refuses every fetch ("YouTube Data API v3 has not been used in project … or it is disabled"). Stash then
   shows a notice with a link to that API page in the client's own project: enable it there, wait a minute,
   and press *Fetch playlists* (no new connection is needed).
2. Set up the consent screen (Google Auth Platform → Get started; External). Google allows creating a client
   only after this.
3. Add the Google account you sign in to YouTube with under Audience → **Test users**, and leave the app in
   *Testing*. Without it Google refuses with "has not completed the Google verification process … only
   developer-approved testers". Publishing the app instead did not work in practice for a personal setup.
4. Create a client of type **Web application** with the redirect URI. A *Desktop app* client (a JSON file with
   `"installed"` and `http://localhost`) is the wrong kind: Google would send you back to your own computer.

A Google app in *Testing* ends its connection after seven days; press **Connect again…** then. YouTube's default
quota is 10 000 units a day and every added, removed or moved item costs about 50; fetching is cheap. Spotify
playlists are pushed as a whole in one or two calls.

Spotify (a developer app in *Development Mode*, as every personal app is) has worked this way since February
2026: the app's owner needs Spotify Premium and at most five accounts can use the app; the songs of a playlist
can be read only when you own it or collaborate on it. Playlists someone else owns that you follow are kept
with their name, owner and size only (the fetch summary says how many); to edit one in Stash, copy it into a
playlist of your own on Spotify first. Stash uses Spotify's current calls (`/playlists/{id}/items`,
`/me/playlists`, `/me/library`).

| Tool | What it does |
| --- | --- |
| `playlists_overview` | the connected services and every playlist (P1, P2, …) with its size and pending changes |
| `playlist_show` | the items of a playlist in order, with what is added, removed or moved |
| `playlist_edit` | pending changes: `add` (addresses or ids, at a position), `remove`, `move`, `order`, `rename` |
| `playlist_push` | writes the pending changes to the service; the assistant is told to ask first |
| `playlist_discard` | drops the pending changes and fetches the playlist again |
| `playlists_sync` | fetches every playlist of a service; `push_pending: true` first pushes all pending changes (a two-way sync, only with your consent) |
| `playlist_create` | a new empty playlist on the service |

### Subscriptions

**Subscriptions** in the top bar uses the same connections as Playlists. For YouTube it holds your channel
subscriptions; for Spotify the artists you follow and the playlists you follow (those someone else owns).

- **Fetch** copies them into Stash, which is also a backup (**Export all** downloads them as JSON). A
  subscription that disappears from the service stays, marked *gone*, with its tags and notes.
- **Tags and notes** are Stash's own: tag channels by topic, filter and sort by them, write why you follow
  something. They never leave Stash.
- **Unsubscribe** and **Add by link…** (a channel `…/@name` or `…/channel/UC…`, a Spotify artist or playlist)
  mark changes as *pending*. **Synchronise** shows the list, with YouTube's quota cost (about 50 units each,
  10 000 a day), and only then sends them and fetches everything back. A change the service refuses is listed
  and stays pending.
- **Check activity** (YouTube) reads each channel's newest upload, 1 quota unit per channel, so that the
  filter *No upload in a year* and the sort *Quietest first* can find channels that have gone quiet.
- **Playlists saved from others on YouTube** cannot be listed or saved through YouTube's API. Added by link,
  they are kept in Stash only (state *only in Stash*), with their title, owner and size; they are never sent
  to YouTube.

Following and unfollowing on Spotify go through its library call (`/me/library`), whose documentation lists
playlists but not artists; if Spotify refuses an artist, the summary says so and the change stays pending.
Spotify needs the permissions `user-follow-*` and `user-library-*`; a connection made before Stash asked
for them is told to *Connect again* on the Playlists page.

| Tool | What it does |
| --- | --- |
| `subscriptions_overview` | counts per service and kind, pending changes, tags in use |
| `subscriptions_list` | the subscriptions by #id, filtered by kind, words, tag, state or `inactive_days`, sorted |
| `subscriptions_edit` | `subscribe`, `unsubscribe`, `keep`, `tag`, `set_tags`, `note`, `forget` |
| `subscriptions_sync` | fetches; `push: true` first sends the pending changes (only with your consent) |
| `subscriptions_activity` | YouTube: reads each channel's newest upload |

### Project handovers

Besides bookmarks, the MCP server keeps written handovers of your projects: a record of what was done, where
things are and what comes next, so a later session, another assistant or another computer can pick the work up.
It is meant for projects that have no repository of their own, and it stays on your own server. Ask the
assistant to "save the handover for this project to Stash".

| Tool | What it does |
| --- | --- |
| `handover_save` | stores the next version of a project's handover (`project`, `body`, and who wrote it: `ai`, `machine`, `session`, `title`, `git_commit`). With `expected_version` the save is refused unless that is still the latest version, so two assistants never overwrite each other unnoticed |
| `handover_get` | the latest version of a project, or a given one |
| `handover_list` | every project with its latest version, when it was updated, and the assistant and machine that wrote it |

A project name is 1–64 lowercase letters, digits and dashes; a handover is at most 200 000 characters. Every
save is a new version and earlier ones are kept; nothing is deleted. The tools need an API key like every other
MCP call, and saving needs a key that is allowed to make changes. Handovers are separate from bookmarks: they
have no sets, previews, changesets, trash or undo. The **AI** section in the top bar shows the projects and lets
you read any version in the browser.

## The API

Other computers and programs use Stash at `/api/v1` with an API key (`Authorization: Bearer stash_…`).
Keys are created in AI → Assistant (MCP); only a hash of the key is stored. A key can only read unless it is
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
AI → Changes made with API keys.

Site icons are a cache shared by all accounts. A refresh fetches only sites the caller has bookmarked, never
replaces a working icon with a failure, and changes the icon version the web UI loads icons with, so browsers
show a new icon at once instead of keeping their week-long cached copy.

## Background upkeep

A slow loop inside the server works through a small batch every few minutes. A bookmark whose title is empty or
just its own address is given the page's real title, and site icons are fetched again. A title you have written
is left untouched, and a page the server cannot reach keeps whatever title it already has. The loop also empties
the trash of what has been there longer than 30 days.

These edits are made directly, so they do not fill the undo history. Each account can turn this off in
Settings → Bookmarks, and `STASH_MAINTENANCE=off` disables it for the whole server. Duplicates and dead links are
found when you ask: My Bookmarks → Tools, or your assistant.

Browsing history is matched to bookmarks loosely (http/https, `www.` and a trailing slash do not matter). Each
device sends visit counts per address (last 30, 90 and 365 days, and all), and a new sync replaces that
device's earlier rows.

Sending the history from the Mac (or Linux computer) where Firefox is used:

```sh
curl -o ~/stash-history-sync.py https://stash.example.com/dl/stash-history-sync.py
python3 ~/stash-history-sync.py --setup       # Stash address, a key (AI → Tools → Browsing history → Create a key
                                              # for this computer), a name for this computer
python3 ~/stash-history-sync.py --dry-run     # shows what would be sent
python3 ~/stash-history-sync.py --schedule    # sends every 6 hours (launchd / cron)
```

## Checking links with a local model

The server's own link check sees only status codes. A video page that answers 200 but says "video unavailable",
a page hidden behind a cookie or age wall, or a domain now selling something else needs a browser and someone to
read the page. `scripts/stash-linkcheck.py` does that on your own computer with a local model in Ollama (default
`gemma4:e4b-128k`; any model with `--model`):

1. It asks Stash over MCP for the bookmarks (all, or `--tag`, `--host`, `--text`, `--tab`, `--where`).
2. Each page is opened in a headless Chromium, a few at a time. Cookie banners and age confirmations are
   answered like a person would; pictures and video are not downloaded.
3. The model gets the status, redirects, final address, title, headings, the start of the text and a video
   player's own status, and answers *exists*, *gone* or *unsure* with a short reason. Logins, captchas, blocks
   and timeouts are *unsure* and never offered for removal.
4. The gone ones are listed with their reasons. You answer: remove all, none, or keep the ids you name. The
   removal is one Stash change through `preview_changes` and `apply_changes`: undoable, and the bookmarks wait
   30 days in the trash.
5. For the pages that exist, the same answer holds the tags that fit them. The model sees the bookmark's
   current tags and the tags you use (with counts), keeps the tags that still fit, adds fitting ones from your own
   vocabulary (a new tag only when none fits) and drops the ones that clearly do not or that duplicate a better
   tag. The changes are printed and applied as one undoable Stash change without asking. Unreadable pages
   ("unsure") are never retagged. For a video, recording or gallery page it tags what the content is about (genre,
   topic, the people named, the setting), up to 8 tags, so the item can be found by what it shows. `--no-tags`
   leaves the tags alone and `--tag-language Finnish` (or any language) makes new tags come in that language (the model names the language of each tag it adds, and a new tag in another language is dropped).

```sh
sudo pacman -S --needed python chromium ollama      # Arch; Ollama also from ollama.com
ollama pull gemma4:e4b-128k
python -m venv ~/.local/share/stash-linkcheck
~/.local/share/stash-linkcheck/bin/pip install playwright
curl -o ~/stash-linkcheck.py https://stash.example.com/dl/stash-linkcheck.py
~/.local/share/stash-linkcheck/bin/python ~/stash-linkcheck.py --setup    # Stash address and an API key
~/.local/share/stash-linkcheck/bin/python ~/stash-linkcheck.py            # check, list, ask, remove
```

The key comes from AI → Assistant (MCP) → Create a key for a program, with "Allow changes" (a read-only key
can check and list but not remove). The verdicts are saved in `~/.cache/stash/linkcheck-report.json`;
`--resume` reuses them and checks only the rest, `--no-delete` only skips the removal (tags are still put in order), `--no-tags` skips the tagging, `--show-browser` shows the window.

## Accounts, e-mail and password reset

- **Registration** asks for an e-mail address, which is optional. Nothing is verified with a link: the address is
  only used to send password reset links. Each account can set, change or remove it in Settings → Account, with
  its password.
- **"Forgot your password?"** on the sign-in page mails a link that works for one hour and only once. Choosing a
  new password signs out every device. Two-factor authentication stays on: after a reset, signing in still asks
  for the code from the authenticator app.
- **Without an address** a password can still be reset by an administrator (`python -m app.cli reset-password
  <user>`, which also turns two-factor off, as before).
- **Setting an address** for an account from the server: `python -m app.cli set-email <user> <address>`
  (`-` removes it). Each address belongs to one account.
- **Sending**: the server sends the mail itself, directly to the recipient's mail server (port 25, signed with
  DKIM). Run `.venv/bin/python -m app.cli mail-dns` once: it prints the SPF and DKIM records for the sender's
  domain (`STASH_MAIL_FROM`, by default `Stash <noreply@HOST>` where HOST is the host name of `STASH_ORIGIN`).
  Add them at your DNS provider; without them some providers refuse the mail. `STASH_MAIL=dry` writes the mail
  to `data/outbox/` instead.
- **Settings** (environment): `STASH_MAIL_FROM` (sender), `STASH_MAIL_HELO` (the name the server introduces
  itself with, default the host of `STASH_ORIGIN`), `STASH_DKIM_KEY` (private key, default `data/dkim.key`,
  created on first use) and `STASH_DKIM_SELECTOR` (default `stash`).
- **Sharing another program's mail setup**: if something else on the server already sends DKIM-signed mail,
  point `STASH_DKIM_KEY` and `STASH_DKIM_SELECTOR` at its key and selector and `STASH_MAIL_FROM` at an address
  of its domain. Its SPF and DKIM records then cover Stash too, and no new DNS records are needed.

## Tests

```sh
.venv/bin/python tests/smoke.py    # the web API end to end, throwaway database
.venv/bin/python tests/api_v1.py   # API keys, /api/v1, previews and undo
.venv/bin/python tests/history.py  # the sync script against a fake Firefox profile, the history API, ordering
.venv/bin/python tests/trash.py    # the trash: every way of deleting, restoring, the 30-day limit
.venv/bin/python tests/account.py  # e-mail addresses, password reset links (once, one hour), two-factor after a reset
.venv/bin/python tests/maintenance.py  # the background upkeep of titles and icons
.venv/bin/python -m pytest -q tests/mcp    # the MCP tools and /mcp end to end, open tabs, playlists and subscriptions (fake services), handovers (pip install pytest)
node tests/ui.mjs                  # headless Chromium, its own server on port 8013, screenshots in data/tmp/
node tools/check-i18n.mjs          # missing or broken Finnish and Swedish translations
node tools/screenshots.mjs         # the README screenshots, from a demo instance on port 8014
```

## Languages

UI strings are written in English in the code (`t('…')`); `static/js/i18n.js` maps them to Finnish and
`static/js/i18n-sv.js` to Swedish, and `extension/_locales/` holds the extension's texts. A new string needs
both translations, which `node tools/check-i18n.mjs` checks (placeholders such as `{n}` included). The
repository, the API, the MCP tools and the scripts are in English only.

## Security in brief

- The session cookie is `HttpOnly; Secure; SameSite=Lax`; every state-changing call of the web API needs the
  `X-Stash` header (CSRF).
- The CSP allows only same-origin scripts and styles; bookmark addresses may be http(s), ftp, mailto and tel.
- Requests made by the server resolve the target first and refuse internal network and localhost addresses.
- The bookmarklet and the extension pass page details in the URL's `#` part, which never reaches server logs;
  configure the reverse proxy to log without query strings and cookies.
- API keys, the browsing history, open tabs, handovers and playlists belong to one account and are deleted
  with it. The Spotify and Google tokens and app secrets are stored in the database as they are, so protect
  `data/` and its backups like the database itself.
