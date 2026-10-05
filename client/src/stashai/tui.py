"""The terminal UI: conversation on the left, results and proposals on the right, the request line below."""
from __future__ import annotations

import threading

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from stashai.agent import Outcome
from stashai.api import StashError
from stashai.config import Config, last_model, remember_model
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
  bring my most used bookmarks to the Dashboard, most used first   (needs the browsing history)

Nothing changes until you accept: a proposal is shown on the right with every change.
  y / Enter on an empty line = apply     n = reject     or write a correction instead

Commands:
  /undo [ID] [force]   undo the latest change (or change ID)
  /history             the latest changes
  /sets                the result sets of this session      /show S3   list a set
  /refresh             load the bookmarks again              /new       forget this conversation
  /rules               your rules file                       /model [NAME]  (F2: choose from a list)
  q                    quit stashai (or /quit)
Keys: Esc stops the model · F2 changes the model · PgUp/PgDn scroll the right pane · q (or Ctrl+Q) quits
The status line shows how full the model's context was at its latest step (yellow from 60 %, red from 85 %);
older tool results are shortened automatically when it fills up, and every request starts afresh."""

YES = {"y", "yes", "k", "kyllä", "joo", "ok"}
NO = {"n", "no", "e", "ei"}


def model_label(m: dict, marks: list[str]) -> str:
    size = f"{m['size_gb']:>5.1f} GB" if m.get("size_gb") else "        "
    info = " ".join(x for x in (m.get("params"), m.get("quant")) if x)
    tail = f"  ({', '.join(marks)})" if marks else ""
    return f"{m['name']:<36} {size}  {info:<16}{tail}"


class ModelPicker(ModalScreen[str]):
    """Choose the local model; Enter picks, Esc keeps the highlighted default."""

    CSS = """
    ModelPicker { align: center middle; }
    #picker { width: 96; max-width: 95%; height: auto; max-height: 80%; border: round $primary;
              background: $surface; padding: 0 1; }
    #picker-title { padding: 1 0 0 0; }
    #picker-help { color: $text-muted; padding: 0 0 1 0; }
    #models { height: auto; max-height: 24; }
    """
    BINDINGS = [Binding("escape", "keep", "Use the highlighted one")]

    def __init__(self, models: list[dict], default: str, recommended: str, last: str = ""):
        super().__init__()
        self.models, self.default, self.recommended, self.last = models, default, recommended, last

    def compose(self) -> ComposeResult:
        options = []
        for m in self.models:
            marks = [x for x, on in (("last used", m["name"] == self.last),
                                     ("default", m["name"] == self.recommended),
                                     ("loaded now", m.get("loaded"))) if on]
            options.append(Option(Text(model_label(m, marks)), id=m["name"]))
        with Vertical(id="picker"):
            yield Static(Text("Which local model should read your bookmarks and pages?", style="bold"), id="picker-title")
            yield Static(Text(f"{len(self.models)} models on this computer. ↑/↓ choose, Enter use it. "
                              "Bigger models plan better, smaller ones answer faster. "
                              "Next time: stashai -m NAME skips this question."), id="picker-help")
            self.option_list = OptionList(*options, id="models")
            yield self.option_list

    def on_mount(self) -> None:
        self.call_after_refresh(self.highlight_default)

    def highlight_default(self) -> None:
        names = [m["name"] for m in self.models]
        self.option_list.highlighted = names.index(self.default) if self.default in names else 0
        self.option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_keep(self) -> None:
        self.dismiss(self.models[self.option_list.highlighted or 0]["name"])


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
        Binding("f2", "pick_model", "Model"),
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
        status = Text(" · ".join(parts))
        if a and a.context_used:
            pct = 100 * a.context_used / max(1, a.context)
            style = "bold red" if pct >= 85 else "yellow" if pct >= 60 else ""
            status.append(" · ")
            status.append(f"context {a.context_used / 1024:.1f}k/{a.context / 1024:.0f}k ({pct:.0f} %)", style=style)
        status.append(f" · {state}")
        self.query_one("#status", Static).update(status)
        prompt = self.query_one("#prompt", Input)
        if a and a.pending and not self.busy:
            prompt.placeholder = "y = apply the proposal · n = reject · or write a correction"
        elif self.busy:
            prompt.placeholder = "The model is working… (Esc stops it)"
        else:
            prompt.placeholder = "What should be done? (F1 = help · q = quit)"

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
            if not self.cfg.model_chosen:
                models = self.agent.llm.model_details()
                if not models:
                    raise LLMError(f"Ollama at {self.agent.llm.base} has no models. Pull one, e.g.: "
                                   f"ollama pull {self.cfg.model}")
                self.call_from_thread(self.ask_model, models)
                return  # start_model continues once a model is chosen
            self.start_model()
        except (StashError, LLMError, ValueError, OSError) as e:
            self.call_from_thread(self.say, f"Cannot start: {getattr(e, 'code', None) or e}", "bold red")
            self.call_from_thread(self.say, "Check the settings with: stashai doctor")
        except SystemExit as e:
            self.call_from_thread(self.say, str(e), "bold red")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready" if self.ready else
                                  "choose a model" if self.picking else "not connected")

    picking = False

    def ask_model(self, models: list[dict]) -> None:
        """Show the model list; once one is chosen, load it."""
        self.picking = True
        last = last_model()
        default = last if last in {m["name"] for m in models} else self.cfg.model

        def chosen(name: str | None) -> None:
            self.picking = False
            if not name:
                return
            self.cfg.model = self.agent.llm.model = name
            remember_model(name)
            self.say(f"Model: {name}")
            self.background(self.start_model)

        self.push_screen(ModelPicker(models, default, self.cfg.model, last), chosen)

    def start_model(self) -> None:
        """Check the chosen model, size its context and load it (runs in a worker thread)."""
        try:
            ok, msg = self.agent.llm.health()
            if not ok:
                raise LLMError(msg)
            self.cfg.model = self.agent.llm.model
            self.agent.llm.num_ctx = self.cfg.num_ctx  # a new model gets its context sized again
            self.agent.context_used = 0
            ctx = self.agent.check_context()
            self.call_from_thread(self.set_state, "loading the model")
            self.agent.llm.chat([{"role": "system", "content": "Reply with {}"}, {"role": "user", "content": "{}"}],
                                max_tokens=4)
            self.call_from_thread(self.say, f"Model {self.agent.llm.model} ready ({ctx}). Log: {self.log_file.path}", "dim")
            first = not self.ready
            self.ready = True
            if first:
                self.check_update()
        except (LLMError, ValueError, OSError) as e:
            self.call_from_thread(self.say, f"The model does not work: {e}", "bold red")
            self.call_from_thread(self.say, "Choose another with /model, or check: stashai doctor")
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
        if text.lower() in ("q", "quit", "exit", "/q", "/quit", "/exit"):
            self.exit()
            return
        if self.busy:
            self.say("The model is still working; press Esc to stop it first.", "yellow")
            return
        if not self.agent or not self.ready:
            if text in ("/quit", "/exit"):
                self.exit()
            elif self.agent and text.split()[:1] == ["/model"]:
                self.command(text)  # a model that did not work can be replaced before anything else
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
        elif kind == "context":
            self.call_from_thread(self.set_state, self.state)

    def do_ask(self, text: str) -> None:
        try:
            out = self.agent.ask(text, cancel=self.cancel, emit=self.emit)
        except Exception as e:  # noqa: BLE001  (a bug must not end the session; the log has the details)
            out = Outcome("error", self.unexpected(e))
        finally:
            self.busy = False
        self.call_from_thread(self.outcome, out)

    def unexpected(self, e: Exception) -> str:
        import traceback

        if self.log_file:
            self.log_file(f"--- UNEXPECTED ERROR\n{traceback.format_exc()}")
        where = f" Details: {self.log_file.path}" if self.log_file else ""
        return f"Something went wrong ({type(e).__name__}: {e}). Nothing was changed.{where}"

    def outcome(self, out: Outcome) -> None:
        try:
            self.show_outcome(out)
        except Exception as e:  # noqa: BLE001  (a display problem must never take the session down)
            import traceback

            if self.log_file:
                self.log_file(f"--- DISPLAY ERROR\n{traceback.format_exc()}")
            self.say(f"Could not show the result ({type(e).__name__}: {e}). The details are in the log; "
                     "nothing was changed.", "bold red")
            self.set_state("ready")

    def show_outcome(self, out: Outcome) -> None:
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
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.say, self.unexpected(e).replace("Nothing was changed.", "Check /history: "
                                  "the change may or may not have been applied."), "bold red")
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
            remember_model(args[0])
            self.say(f"Model: {args[0]}")
            self.background(self.start_model)
        elif cmd == "/model":
            self.background(self.do_pick_model)
        else:
            self.say("Unknown command. F1 = help.", "yellow")

    def do_pick_model(self) -> None:
        try:
            models = self.agent.llm.model_details()
            self.call_from_thread(self.ask_model, models)
        except Exception as e:  # noqa: BLE001  (shown to the user)
            self.call_from_thread(self.say, f"Cannot list the models: {e}", "red")
        finally:
            self.busy = False
            self.call_from_thread(self.set_state, "ready")

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

    def action_pick_model(self) -> None:
        if self.busy:
            self.say("The model is working; press Esc first, then F2.", "yellow")
        elif self.agent and not self.picking:
            self.background(self.do_pick_model)

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
