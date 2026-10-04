"""stashai – manage your Stash bookmarks in plain language with a local model.

  stashai [-m MODEL]            the terminal UI; without -m it asks which local model to use
  stashai login [URL]           save the Stash address and an API key (Stash → Settings → API keys)
  stashai ask "REQUEST" [--yes] [-m MODEL]   one request without the UI; asks before changing anything
  stashai doctor                check the connection to Stash and the model
  stashai history               the latest changes made with API keys
  stashai undo [ID] [--force]   undo the latest change (or change ID)
  stashai update                install the newest stashai from your Stash
"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading

from stashai import __version__
from stashai.config import Config, load, save_login


def build(cfg: Config, *, need_llm: bool = True):
    from stashai.agent import Agent
    from stashai.api import StashAPI
    from stashai.llm import LLM
    from stashai.runlog import RunLog
    from stashai.store import Store

    if not cfg.stash_url or not cfg.api_key:
        raise SystemExit("Not signed in. Run: stashai login https://your-stash.example")
    api = StashAPI(cfg.stash_url, cfg.api_key)
    llm = LLM(cfg.llm_url, cfg.model, num_ctx=cfg.num_ctx, temperature=cfg.temperature, think=cfg.think,
              keep_alive=cfg.keep_alive, timeout=cfg.timeout) if need_llm else None
    from stashai.web import Web

    log = RunLog()
    web = Web(allow_private=cfg.web_private, search=cfg.web_search) if cfg.web else None
    agent = Agent(api=api, llm=llm, store=Store(), rules=cfg.rules, max_steps=cfg.max_steps,
                  judge_batch=cfg.judge_batch, log=log, web=web)
    return agent, log


def cmd_login(cfg: Config, url: str | None) -> int:
    from stashai.api import StashAPI, StashError, check_url

    url = (url or input("Stash address (https://…): ")).strip()
    try:
        url = check_url(url if "://" in url else f"https://{url}")
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    key = getpass.getpass("API key (Stash → Settings → API keys; the input is hidden): ").strip()
    try:
        me = StashAPI(url, key).me()
    except StashError as e:
        print(f"Did not work: {e.code}", file=sys.stderr)
        return 1
    save_login(cfg, url, key)
    access = "read and change" if me["can_write"] else "read only"
    print(f"Signed in as {me['user']} ({me['bookmarks']} bookmarks), key “{me['key']}”: {access}.")
    print(f"Saved to {cfg.path} (readable only by you). Your own rules: {cfg.rules_path}")
    return 0


def cmd_doctor(cfg: Config) -> int:
    from stashai.api import StashAPI, StashError
    from stashai.llm import LLM, NotLocalError

    good = True
    try:
        if not cfg.stash_url or not cfg.api_key:
            raise ValueError("not signed in; run: stashai login https://your-stash.example")
        me = StashAPI(cfg.stash_url, cfg.api_key).me()
        print(f"ok   Stash {cfg.stash_url}: {me['user']}, {me['bookmarks']} bookmarks, key “{me['key']}”"
              f" ({'read and change' if me['can_write'] else 'READ ONLY: changes cannot be applied'})")
        good &= me["can_write"]
    except (StashError, ValueError) as e:
        print(f"FAIL Stash {cfg.stash_url}: {getattr(e, 'code', e)}".replace("Stash : ", "Stash: "))
        good = False
    try:
        llm = LLM(cfg.llm_url, cfg.model, num_ctx=cfg.num_ctx)
        ok, msg = llm.health()
        print(f"{'ok  ' if ok else 'FAIL'} model {msg}")
        good &= ok
        if ok:
            ctx = llm.context_length()
            print(f"     context: {ctx or 'not loaded yet'}"
                  + (" (stashai asks for 32768 when it is under 16384)" if not ctx or ctx < 16384 else ""))
    except NotLocalError as e:
        print(f"FAIL model: {e}")
        good = False
    rules = cfg.rules()
    print(f"     rules: {cfg.rules_path} ({len(rules.splitlines())} rules)")
    try:
        newer = newer_version(cfg.stash_url) if cfg.stash_url else None
        print(f"     stashai {__version__}" + (f"; {newer[0]} is available: run stashai update" if newer else " (newest)"))
    except Exception:  # noqa: BLE001  (an update check never fails the doctor)
        pass
    return 0 if good else 1


def cmd_ask(cfg: Config, request: str, yes: bool) -> int:
    from stashai.render import plan_text, set_text

    agent, log = build(cfg)
    agent.refresh()
    print(f"{len(agent.store.bookmarks)} bookmarks loaded; {agent.check_context()}", file=sys.stderr)

    def emit(kind: str, text: str) -> None:
        if kind == "status":
            print(f"\r\033[K… {text}", end="", file=sys.stderr, flush=True)
        else:
            print(f"\r\033[K{ {'thought': '  ·', 'tool': '  →', 'result': '    '}.get(kind, '  ')} {text}", file=sys.stderr)

    out = agent.ask(request, cancel=threading.Event(), emit=emit)
    print("\r\033[K", end="", file=sys.stderr)
    if out.kind != "plan":
        print(out.message)
        if out.show:
            print("\n".join(set_text(agent.store, out.show)))
        print(f"(log: {log.path})", file=sys.stderr)
        return 0 if out.kind == "answer" else 1
    print("\n".join(plan_text(out.plan.summary, out.plan.preview)))
    if not yes:
        try:
            yes = input("\nApply these changes? [y/N] ").strip().lower() in ("y", "yes", "k", "kyllä")
        except EOFError:
            yes = False
    if not yes:
        agent.discard()
        print("Nothing was changed.")
        return 0
    result = agent.apply()
    print(f"Done: change #{result['changeset']}. Undo with: stashai undo {result['changeset']}")
    return 0


def newer_version(stash_url: str) -> tuple[str, str] | None:
    """(version, wheel url) when Stash offers a newer stashai than this one."""
    import httpx

    from stashai.api import check_url

    r = httpx.get(check_url(stash_url) + "/api/client", timeout=10)
    r.raise_for_status()
    info = r.json()
    if not info.get("wheel"):
        return None
    parse = lambda v: tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))  # noqa: E731
    return (info["version"], info["wheel"]) if parse(info["version"]) > parse(__version__) else None


def cmd_update(cfg: Config, url: str | None, check: bool) -> int:
    import os
    import shutil
    import subprocess

    import httpx

    stash_url = url or cfg.stash_url
    if not stash_url:
        print("Give the Stash address: stashai update https://your-stash.example", file=sys.stderr)
        return 2
    try:
        newer = newer_version(stash_url)
    except (httpx.HTTPError, ValueError) as e:
        print(f"Cannot check for updates: {e}", file=sys.stderr)
        return 1
    if not newer:
        print(f"stashai {__version__} is the newest version.")
        return 0
    version, wheel = newer
    print(f"stashai {version} is available (this is {__version__}).")
    if check:
        return 0
    # installed with pipx: reinstall the app; otherwise upgrade it in the Python it runs in
    in_pipx = f"{os.sep}pipx{os.sep}venvs{os.sep}" in sys.prefix + os.sep
    if in_pipx and shutil.which("pipx"):
        command = ["pipx", "install", "--force", wheel]
    else:
        command = [sys.executable, "-m", "pip", "install", "--upgrade", wheel]
    print("Running:", " ".join(command))
    code = subprocess.call(command)
    if code == 0:
        print(f"Updated to stashai {version}.")
    return code


def cmd_history(cfg: Config) -> int:
    import time

    agent, _ = build(cfg, need_llm=False)
    for c in agent.api.history(30):
        counts = ", ".join(f"{n} {k}" for k, n in c["counts"].items() if n and k != "unchanged")
        state = "  (undone)" if c["undone_at"] else ""
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["created_at"]))
        print(f"#{c['id']:<5} {when}  {c['summary'][:70]:<70}  {counts}{state}")
    return 0


def cmd_undo(cfg: Config, changeset: int | None, force: bool) -> int:
    from stashai.api import StashError

    agent, _ = build(cfg, need_llm=False)
    try:
        target, result = agent.undo(changeset, force=force)
    except StashError as e:
        print(f"Could not undo: {e.code}" + (" (add --force to undo it anyway)" if e.status == 409 and "later" in e.code else ""),
              file=sys.stderr)
        return 1
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Undid #{target['id']} {target.get('summary', '')}: {result['restored']} restored, {result['removed']} removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stashai", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"stashai {__version__}")
    p.add_argument("-m", "--model", help="the local model to use, e.g. qwen3.6:35b-a3b (the UI asks when not given)")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("login")
    s.add_argument("url", nargs="?")
    s = sub.add_parser("ask")
    s.add_argument("request", nargs="+")
    s.add_argument("--yes", action="store_true", help="apply the proposal without asking")
    s.add_argument("-m", "--model", dest="ask_model", help="the local model to use (default: the config's)")
    sub.add_parser("doctor")
    sub.add_parser("history")
    s = sub.add_parser("undo")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--force", action="store_true", help="also when later changes touched the same bookmarks")
    s = sub.add_parser("update")
    s.add_argument("url", nargs="?", help="the Stash address (default: the one you logged in to)")
    s.add_argument("--check", action="store_true", help="only tell whether a newer version exists")
    args = p.parse_args(argv)
    cfg = load()
    model = args.model or getattr(args, "ask_model", None)
    if model:
        cfg.model, cfg.model_chosen = model, True
    try:
        if args.cmd == "login":
            return cmd_login(cfg, args.url)
        if args.cmd == "doctor":
            return cmd_doctor(cfg)
        if args.cmd == "ask":
            return cmd_ask(cfg, " ".join(args.request), args.yes)
        if args.cmd == "history":
            return cmd_history(cfg)
        if args.cmd == "undo":
            return cmd_undo(cfg, args.id, args.force)
        if args.cmd == "update":
            return cmd_update(cfg, args.url, args.check)
        from stashai.tui import run

        return run(cfg)
    except KeyboardInterrupt:
        return 130
