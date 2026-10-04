# stashai

Manage your Stash bookmarks in plain language from a Linux terminal. You write what you want, a language
model running on your own computer (Ollama, `qwen3.6:35b-a3b` by default) works out how, shows exactly what
would change, and changes your bookmarks only when you accept. The model runs on your own machine, so your
bookmarks, browsing history and requests never reach a cloud service.

```
> list everything about company X
> move everything about company X to the tag y
> which bookmarks have no tags? suggest tags for them
> delete those
> check whether the links tagged linux still work and fix the moved ones
> what is behind bookmark 1234? tag it properly
> bring my most used bookmarks to the Dashboard, most used first
> which pages do I visit often but have not bookmarked?
> which Dashboard bookmarks have I not used for a year?
```

Requests can be written in any language the model understands; it answers in the same language.

## Installing

You need Python 3.11+, pipx and Ollama with the model:

```sh
ollama pull qwen3.6:35b-a3b
pipx install --force https://stash.example.com/dl/stashai-0.1.6-py3-none-any.whl   # exact address: Stash → Settings → API keys
stashai login https://stash.example.com      # paste a key that can change
stashai doctor                                # checks Stash, the model and the context size
stashai                                       # the terminal UI: first asks which local model to use
stashai -m qwen3.6:35b-a3b                    # the terminal UI with this model, without asking
stashai update                                # installs the newest version (served by your Stash)
```

From version 0.1.1 on, stashai updates itself with `stashai update`; it also tells you at start and in
`stashai doctor` when a newer version is available.

The model is taken from the same Ollama the `ollama` command uses: `[llm] url` in `config.toml`, otherwise
`OLLAMA_HOST`, otherwise `127.0.0.1:11434`.

Get an API key from Stash: Settings → API keys → Create API key. It is saved in
`~/.config/stashai/config.toml`, readable only by you (or give it in the environment variable `STASHAI_KEY`).

## Using it

- At start, stashai lists every model your Ollama has (size, parameters, which one is loaded) and asks which
  one to use; the one you chose last time is highlighted. `-m NAME` (or `STASHAI_MODEL`) skips the question,
  and `/model` changes the model later.
- On the left: the conversation and the model's steps. On the right: results and proposals.
- A proposal shows every change (tags +/-, moves, new order, deletions). **y** or an empty Enter applies it,
  **n** rejects it, or write a correction instead ("don't delete the ones tagged x").
- **Esc** stops the model, **PgUp/PgDn** scroll the right pane, **F1** help, **Ctrl+Q** quits.
- Commands: `/undo [ID] [force]`, `/history`, `/sets`, `/show S3`, `/refresh`, `/new`, `/rules`, `/model NAME`.

Without the UI: `stashai ask "request" [-m MODEL]` (the model from the config unless given; asks before
changing anything; `--yes` applies right away),
`stashai history`, `stashai undo [ID]`.

### Your own rules

`~/.config/stashai/rules.md` holds your standing rules in plain language, for example "recipes always have the
tag food and no other tags". The model follows them in every proposal. The file is read at every request.

## How it works

The principles come from sorto, a local-LLM file sorter by the same author:

- **The model only proposes.** It uses read-only tools (search, list, check) and finally either answers or
  proposes changes. The program checks the proposal and Stash runs it as a dry run, so the preview is exactly
  what would happen. Nothing changes before you accept.
- **Sets by name, not lists of ids.** Every search makes a set (S1, S2, …) and the model refers to it by name
  ("delete S4"). The program expands names into ids, so the model cannot lose or invent bookmarks. Sets of up to
  60 bookmarks are listed whole, so the model has their ids at hand. "Those" means the set shown last.
- **Everything at once when needed.** All bookmarks are loaded onto your computer at once, so searches are
  instant. When words are not enough (topic, meaning, language), `judge` lets the model read a set through in
  batches of 60; `ALL` goes through the whole collection (slow: about 80 model calls for 4,700 bookmarks).
- **The web.** The model can read a page as text (`fetch`: status, redirects, title, description, headings,
  text), check all links of a set at once (`check`: alive / moved / dead / unclear) and search the web
  (`web_search`: DuckDuckGo or your own SearXNG). Moved links can be given their new address (`update_urls`).
  A link that redirects to a front page or a login is "unclear", not "moved", and "dead" means only 404/410, no
  such host or a refused connection. Requests go from your own computer (pages behind your VPN work), but never
  to this computer's own addresses, and not to the local network unless `allow_private` is set. Page text is
  data, not instructions: the model is told never to follow commands written in a page, and even if it tried,
  the result would only be a proposal you see before accepting. Web search queries go to DuckDuckGo;
  `[web] search = "off"` or the address of your own SearXNG changes that.
- **Browsing history.** Once your Firefox history has been sent to Stash (`stash-history-sync.py`, see Stash →
  Settings → Browsing history), every bookmark line shows the model its visit counts (30 / 90 days / all) and
  the last visit. Searches can filter by use (`used_min`, `unused_days`, `sort: "use"`), `history` lists visited
  addresses (also those not bookmarked), and `sort_by_use` orders a tab's bookmarks and categories most used
  first. The order is computed by the program from the visit counts, so it is always consistent; the model only
  decides what goes to the Dashboard and where.
- **Undoable.** Every applied change is stored in Stash as a changeset with the earlier state of what it
  touched. `/undo` (or Stash → Settings → Changes made with API keys) restores tags, places, order and details,
  brings deleted bookmarks back and removes added ones.
- **Only a local model.** The model's address must be this computer (loopback); proxy settings are ignored.
  Your bookmarks never go to cloud models.
- **A log.** Each session writes `~/.local/state/stashai/logs/session-*.log` (requests, model replies, tool
  results, proposals and applied changes).

Settings (`[llm]` in `config.toml`): `url`, `model`, `num_ctx` (0 = the server's `OLLAMA_CONTEXT_LENGTH`; with
less than 16,384 tokens stashai asks for 32,768), `think` (Qwen's hidden reasoning: sometimes better plans,
much slower), `judge_batch`, `max_steps`, `keep_alive`. `[web]`: `enabled` (true), `search` (`"duckduckgo"`,
a SearXNG address or `"off"`), `allow_private` (false).

## Development

```sh
cd client
python -m venv .venv && .venv/bin/pip install -e '.[dev]' fastapi pydantic
.venv/bin/python -m pytest -q             # runs the agent against the real Stash app (throwaway database)
.venv/bin/pip wheel --no-deps -w dist .   # the package Stash serves at /dl/
```
