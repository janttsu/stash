"""The terminal UI: conversation on the left, results and proposals on the right, the request line below."""
from __future__ import annotations

import threading

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, RichLog, Static

from stashai.agent import Outcome
from stashai.api import StashError
from stashai.config import Config
from stashai.llm import LLMError
from stashai.render import plan_text, set_text

HELP = """\
Write what you want in plain language and press Enter, for example:
  list everything about company X
  move everything about company X to tag y
  which bookmarks have no tags? suggest tags for them
  delete those                        (refers to what was shown last)
  check whether the links tagged linux still work, and fix the moved ones
  what is behind bookmark 1234? tag it properly

Nothing changes until you accept: a proposal is shown on the right with every change.
  y / Enter on an empty line = apply     n = reject     or write a correction instead

Commands:
  /undo [ID] [force]   undo the latest change (or change ID)
  /history             the latest changes
  /sets                the result sets of this session      /show S3   list a set
  /refresh             load the bookmarks again              /new       forget this conversation
  /rules               your rules file                       /model NAME
  /quit
Keys: Esc stops the model · PgUp/PgDn scroll the right pane · Ctrl+Q quits"""

YES = {"y", "yes", "k", "kyllä", "joo", "ok"}
NO = {"n", "no", "e", "ei"}


class StashAI(App):
    TITLE = "stashai"
    CSS = """
    #status { height: 1; background: $primary-background; color: $text; padding: 0 1; }
    #main { height: 1fr; }
    #log { width: 1fr; border: round $primary; padding: 0 1; }
    #side { width: 1fr; }
    #view-title { height: 1; padding: 0 1; color: $text-muted; }
    #view { height: 1fr; border: round $secondary; padding: 0 1; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [
        Binding("escape", "stop", "Stop the model"),
        Binding("pageup", "scroll_view(-1)", "Scroll", show=False),
        Binding("pagedown", "scroll_view(1)", "Scroll", show=False),
        Binding("f1", "help", "Help"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.agent = None
        self.log_file = None
        self.cancel = threading.Event()
        self.busy = False
        self.ready = False
        self.state = "starting"

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal(id="main"):
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
            with Vertical(id="side"):
                yield Static("", id="view-title")
                yield RichLog(id="view", wrap=False, markup=False, highlight=False)
        yield Input(placeholder="Starting…", id="prompt")
        yield Footer()

    # --- helpers --------------------------------------------------------------

    def say(self, text: str, style: str = "") -> None:
        self.query_one("#log", RichLog).write(Text(text, style=style))

    def show(self, title: str, lines: list[str]) -> None:
        view = self.query_one("#view", RichLog)
        view.clear()
        self.query_one("#view-title", Static).update(Text(title))
        for line in lines:
            style = "red" if line.startswith("DELETE") else "green" if line.startswith("ADD") else \
                "bold" if line.startswith("PROPOSAL") else ""
            view.write(Text(line, style=style))
        view.scroll_home(animate=False)

    def set_state(self, state: str) -> None:
        self.state = state
        a = self.agent
        parts = ["stashai"]
        if a and a.store.bookmarks:
            parts.append(f"{a.api.url.split('://', 1)[1]} · {len(a.store.bookmarks)} bookmarks")
        parts.append(self.cfg.model)
        parts.append(state)
        self.query_one("#status", Static).update(Text(" · ".join(parts)))
        prompt = self.query_one("#prompt", Input)
        if a and a.pending and not self.busy:
            prompt.placeholder = "y = apply the proposal · n = reject · or write a correction"
        elif self.busy:
            prompt.placeholder = "The model is working… (Esc stops it)"
        else:
            prompt.placeholder = "What should be done? (F1 = help)"

    def on_mount(self) -> None:
        self.set_state("starting")
        self.say("stashai – your bookmarks in plain language. F1 = help.", "bold")
        self.query_one("#prompt", Input).focus()
        self.busy = True
        self.run_worker(self.start, thread=True, exclusive=True, group="agent")

    def start(self) -> None:
        from stashai.cli import build

        try:
            self.agent, self.log_file = build(self.cfg)
            me = self.agent.api.me()
            self.agent.refresh()
            self.call_from_thread(self.say, f"Signed in to {self.agent.api.url} as {me['user']}: "
                                  f"{len(self.agent.store.bookmarks)} bookmarks, {len(self.agent.store.tags)} tags."
                                  + ("" if me["can_write"] else "  This key is READ ONLY: changes cannot be applied."))
            ok, msg = self.agent.llm.health()
            if not ok:
                raise LLMError(msg)
            ctx = self.agent.check_context()
            self.call_from_thread(self.set_state, "loading the model")
            self.agent.llm.chat([{"role": "system", "content": "Reply with {}"}, {"role": "user", "content": "{}"}],
                                max_tokens=4)
            self.call_from_thread(self.say, f"Model {self.agent.llm.model} ready ({ctx}). Log: {self.log_file.path}", "dim")
            self.ready = True
            self.check_update()
        except (StashError, LLMError, ValueError, OSError) as e:
            self.call_from_thread(self.say, f"Cannot start: {getattr(e, 'code', None) or e}", "bold red")
            self.call_from_thread(self.say, "Check the settings with: stashai doctor")
        except SystemExit as e:
            self.call_from_thread(self.say, str(e), "bold red")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready" if self.ready else "not connected")

    def check_update(self) -> None:
        from stashai.cli import newer_version

        try:
            newer = newer_version(self.cfg.stash_url)
        except Exception:  # noqa: BLE001  (no update check must stop the program)
            return
        if newer:
            self.call_from_thread(self.say, f"stashai {newer[0]} is available. Quit and run: stashai update", "yellow")

    # --- input --------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if self.busy:
            self.say("The model is still working; press Esc to stop it first.", "yellow")
            return
        if not self.agent or not self.ready:
            if text in ("/quit", "/exit"):
                self.exit()
            return
        pending = self.agent.pending
        if pending and (text.lower() in YES or text == ""):
            self.say(f"> {text or 'y'}", "bold cyan")
            self.background(self.do_apply)
            return
        if pending and text.lower() in NO:
            self.say(f"> {text}", "bold cyan")
            self.agent.discard()
            self.say("Rejected; nothing was changed.")
            self.set_state("ready")
            return
        if not text:
            return
        if text.startswith("/"):
            self.command(text)
            return
        self.say(f"\n> {text}", "bold cyan")
        self.cancel = threading.Event()
        self.background(lambda: self.do_ask(text))

    def background(self, fn) -> None:
        self.busy = True
        self.set_state("working")
        self.run_worker(fn, thread=True, exclusive=True, group="agent")

    def emit(self, kind: str, text: str) -> None:
        if kind == "status":
            self.call_from_thread(self.set_state, text)
        elif kind == "thought":
            self.call_from_thread(self.say, f"  · {text}", "dim italic")
        elif kind == "tool":
            self.call_from_thread(self.say, f"  → {text}", "dim")
        elif kind == "result":
            self.call_from_thread(self.say, f"    {text}", "dim")

    def do_ask(self, text: str) -> None:
        try:
            out = self.agent.ask(text, cancel=self.cancel, emit=self.emit)
        finally:
            self.busy = False
        self.call_from_thread(self.outcome, out)

    def outcome(self, out: Outcome) -> None:
        if out.kind == "plan":
            p = out.plan
            c = p.preview["counts"]
            self.show("Proposal – y = apply, n = reject", plan_text(p.summary, p.preview))
            self.say(f"Proposal: {p.summary}", "bold")
            self.say(f"  {c['updated']} changed, {c['deleted']} deleted, {c['created']} added – see the right pane. "
                     "y = apply, n = reject, or write a correction.", "yellow" if c["deleted"] else "")
        elif out.kind == "answer":
            self.say(out.message)
            if out.show:
                self.show(f"{out.show} – PgUp/PgDn to scroll", set_text(self.agent.store, out.show))
        else:
            self.say(out.message, "bold red" if out.kind == "error" else "yellow")
        self.set_state("ready")

    def do_apply(self) -> None:
        try:
            result = self.agent.apply()
            c = result["counts"]
            self.call_from_thread(self.say, f"Done: change #{result['changeset']} ({c['updated']} changed, "
                                  f"{c['deleted']} deleted, {c['created']} added). /undo takes it back.", "green")
        except (StashError, ValueError) as e:
            self.call_from_thread(self.say, f"Could not apply: {getattr(e, 'code', None) or e}", "bold red")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready")

    # --- commands ----------------------------------------------------------------

    def command(self, text: str) -> None:
        cmd, *args = text.split()
        a = self.agent
        self.say(f"> {text}", "bold cyan")
        if cmd in ("/quit", "/exit", "/q"):
            self.exit()
        elif cmd == "/help":
            self.action_help()
        elif cmd == "/sets":
            self.show("Sets", [a.store.sets_text()])
        elif cmd == "/show" and args:
            try:
                self.show(f"{args[0].upper()} – PgUp/PgDn to scroll", set_text(a.store, args[0].upper()))
            except KeyError as e:
                self.say(str(e.args[0]), "red")
        elif cmd == "/new":
            a.reset()
            self.query_one("#log", RichLog).clear()
            self.say("A new conversation: earlier requests and sets are forgotten.")
        elif cmd == "/refresh":
            self.background(self.do_refresh)
        elif cmd == "/history":
            self.background(self.do_history)
        elif cmd == "/undo":
            force = "force" in args
            ids = [int(x.lstrip("#")) for x in args if x.lstrip("#").isdigit()]
            self.background(lambda: self.do_undo(ids[0] if ids else None, force))
        elif cmd == "/rules":
            rules = self.cfg.rules()
            self.show(f"Rules: {self.cfg.rules_path}", (rules or "(no rules; edit the file to add some)").splitlines()
                      + ["", "The file is read again for every request."])
        elif cmd == "/model" and args:
            self.cfg.model = a.llm.model = args[0]
            self.say(f"Model: {args[0]}")
            self.set_state("ready")
        else:
            self.say("Unknown command. F1 = help.", "yellow")

    def do_refresh(self) -> None:
        try:
            self.agent.refresh()
            self.call_from_thread(self.say, f"{len(self.agent.store.bookmarks)} bookmarks loaded.")
        except StashError as e:
            self.call_from_thread(self.say, f"Could not load: {e.code}", "red")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready")

    def do_history(self) -> None:
        import time

        try:
            rows = []
            for c in self.agent.api.history(50):
                counts = ", ".join(f"{n} {k}" for k, n in c["counts"].items() if n and k != "unchanged")
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["created_at"]))
                rows.append(f"#{c['id']:<5} {when}  {c['summary'][:60]}  ({counts})" + ("  undone" if c["undone_at"] else ""))
            self.call_from_thread(self.show, "Changes made with API keys – /undo ID", rows or ["(none yet)"])
        except StashError as e:
            self.call_from_thread(self.say, f"Could not load: {e.code}", "red")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready")

    def do_undo(self, changeset: int | None, force: bool) -> None:
        try:
            target, result = self.agent.undo(changeset, force=force)
            self.call_from_thread(self.say, f"Undid #{target['id']} {target.get('summary', '')}: "
                                  f"{result['restored']} restored, {result['removed']} removed.", "green")
        except StashError as e:
            extra = f"  Write /undo {changeset or ''} force to undo it anyway." if e.status == 409 and "later" in e.code else ""
            self.call_from_thread(self.say, f"Could not undo: {e.code}.{extra}", "red")
        except ValueError as e:
            self.call_from_thread(self.say, str(e), "yellow")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready")

    # --- actions ------------------------------------------------------------------

    def action_stop(self) -> None:
        if self.busy:
            self.cancel.set()
            self.say("Stopping…", "yellow")

    def action_help(self) -> None:
        self.show("Help", HELP.splitlines())

    def action_scroll_view(self, direction: int) -> None:
        view = self.query_one("#view", RichLog)
        if direction < 0:
            view.scroll_page_up(animate=False)
        else:
            view.scroll_page_down(animate=False)

    def on_unmount(self) -> None:
        self.cancel.set()
        if self.log_file:
            self.log_file.close()


def run(cfg: Config) -> int:
    StashAI(cfg).run()
    return 0
