# stashai

Your Stash bookmarks as an **MCP server**, so you can manage them in plain language from Qwen Code, Claude
Code, Gemini CLI or any other MCP client, plus a few terminal helpers. You write what you want, the client's
model finds the bookmarks with stashai's tools, shows you the exact change as a preview, and changes nothing
until you agree. Every change can be undone.

```
> list everything about company X
> move everything about company X to the tag y
> which bookmarks have no tags? suggest tags for them
> check whether the links tagged linux still work and fix the moved ones
> fix the titles of the bookmarks on the Work tab
> bring my most used bookmarks to the Dashboard, most used first
> which pages do I visit often but have not bookmarked?
```

Before version 0.2, stashai had its own terminal UI and drove a local Ollama model itself. That agent is gone:
the model now lives in your MCP client, and stashai provides the tools and the safety around them.

## Installing

You need Python 3.11+ and pipx.

```sh
pipx install --force https://stash.example.com/dl/stashai-0.2.0-py3-none-any.whl   # exact address: Stash → Settings → API keys
stashai login https://stash.example.com      # paste an API key that can change
stashai doctor                                # checks Stash and prints the MCP client settings below
stashai update                                # installs the newest version (served by your Stash)
```

Get an API key from Stash: Settings → API keys → Create API key, with "Allow changes". It is saved in
`~/.config/stashai/config.toml`, readable only by you (or give it in the environment variables `STASHAI_URL`
and `STASHAI_KEY`).

## Connecting an MCP client

The client starts `stashai mcp` itself and talks to it over stdio. `stashai doctor` prints these lines with the
full path to your `stashai`.

**Qwen Code**: add to `~/.qwen/settings.json` (or `.qwen/settings.json` in a project):

```json
{
  "mcpServers": {
    "stash": { "command": "stashai", "args": ["mcp"] }
  }
}
```

**Gemini CLI**: the same block in `~/.gemini/settings.json`.

**Claude Code**:

```sh
claude mcp add stash -- stashai mcp
```

Then just ask, for example "list my bookmarks about Python that have no tags". The server sends the client
instructions on how to work with the tools, so no separate prompt file is needed. Leave `apply_changes` and
`undo` behind the client's confirmation prompt (the default in Qwen Code and Claude Code): then nothing
changes without your explicit yes, even if the model misunderstands.

## The tools

| Tool | What it does |
| --- | --- |
| `overview` | tags with counts, tabs and categories, size of the collection, the sets made so far |
| `search` | finds bookmarks by words, regex, site, tags, tab, category, dates or use, and keeps them as a set (S1, S2, …) |
| `show`, `combine` | list a set; union, intersection or difference of two sets |
| `browsing_history` | visited addresses from your synced Firefox history, also those not bookmarked |
| `read_page` | reads one page as text from your computer |
| `check_links` | checks every link of a set: alive, moved, dead, unclear |
| `refresh_titles` | reads the pages' real titles from your computer and previews giving them to the bookmarks |
| `refresh_icons` | has Stash fetch the site icons again (a server cache, done at once) |
| `preview_changes` | runs changes as a dry run in Stash and returns the exact diff with a preview id (P1, P2, …) |
| `apply_changes` | applies a preview you agreed to |
| `recent_changes`, `undo` | the latest changes and taking one back |

Changes are operations on sets: add, remove or replace tags, rename a tag everywhere, delete, move to a
Dashboard category or the Catalog, update one bookmark, tag each bookmark differently, create, reorder,
sort by use, give moved links their new address, and give bookmarks their refreshed titles.

## How it stays safe

- **Sets by name, not lists of ids.** Every search makes a set and changes refer to it by name. stashai
  expands names into ids itself, so the model cannot lose or invent bookmarks, and long lists never have to
  pass through the model.
- **Preview, then apply.** `preview_changes` is an exact dry run in Stash. `apply_changes` only takes a preview
  id, and it runs the dry run once more first: if the bookmarks changed after the preview (for example in the
  web UI), nothing is applied and a new preview is needed.
- **Undoable.** Every applied preview is one changeset in Stash with the earlier state of everything it
  touched. `undo` (or Stash → Settings → Changes made with API keys) brings it back.
- **Untrusted pages.** The client is told that page text, titles and history titles are data, never
  instructions. Pages are fetched from your computer, never from its own addresses, and not from the local
  network unless `[web] allow_private = true`.

## Your own rules

`~/.config/stashai/rules.md` holds your standing rules in plain language, for example "recipes always have the
tag food and no other tags". `stashai mcp` hands them to the client's model with its instructions, so they
apply in every change. The file is read when the client starts the server.

## Privacy

stashai itself only talks to your Stash and to the pages it reads. What the model sees depends on your MCP
client: a cloud model receives the titles, addresses and tags in the tool results. To keep everything on your
own machines, point the client at a local model (Qwen Code and Gemini CLI accept an OpenAI-compatible
endpoint such as Ollama's).

## Terminal helpers

These need no model:

```sh
stashai history                # the latest changes made with API keys
stashai undo [ID] [--force]    # undo the latest change (or change ID)
stashai titles                 # fill in empty or address-only titles, ask before applying
stashai titles --all           # re-read every bookmark and fix any whose page now has a different title
stashai titles --host x.com    # only that site (subdomains included);  --tag news  only that tag
stashai titles --all --yes     # apply without asking (for cron);  --dry-run shows the changes only
```

`stashai titles` reads each page from the computer it runs on. A title your Stash server cannot get from where
it runs (for example a site that only answers in certain countries) is fixed by running the command on a
computer there. Error, login and placeholder pages are skipped, and `--icons` also has the server fetch the
site icons again.

Settings in `config.toml`: `[stash] url, key`; `[web] enabled` (true: pages may be read from this computer),
`allow_private` (false). An `[llm]` section from older versions is ignored.

## Development

```sh
cd client
python -m venv .venv && .venv/bin/pip install -e '.[dev]' fastapi pydantic
.venv/bin/python -m pytest -q             # runs the tools against the real Stash app (throwaway database)
.venv/bin/pip wheel --no-deps -w dist .   # the package Stash serves at /dl/
```
