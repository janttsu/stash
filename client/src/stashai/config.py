"""Settings: ~/.config/stashai/config.toml (written by `stashai login`, readable only by you)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "qwen3.6:35b-a3b"
DEFAULT_LLM_URL = "http://127.0.0.1:11434"

EXAMPLE_RULES = """\
# Your standing rules for stashai, in plain language and in any language.
# The model follows them in every plan. Lines starting with # are ignored, so
# remove the # in front of an example to use it.

# - Recipes always have the tag food and no other tags.
# - Bookmarks about a single company are tagged with the company's name.
# - Never delete bookmarks that are on the Dashboard tab "Start".
"""


def ollama_host_url(value: str) -> str:
    """OLLAMA_HOST as the ollama command reads it: "host", "host:port" or a URL; 0.0.0.0 means this machine."""
    value = value.strip()
    if not value:
        return DEFAULT_LLM_URL
    if "://" not in value:
        value = "http://" + value
    scheme, rest = value.split("://", 1)
    hostport = rest.split("/", 1)[0]
    if hostport.startswith("["):
        host, _, port = hostport[1:].partition("]")
        port = port.lstrip(":")
        host = "::1" if host in ("::", "") else host
        host = f"[{host}]"
    else:
        host, _, port = hostport.partition(":")
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"{scheme}://{host}:{port or '11434'}"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "stashai"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "stashai"


@dataclass
class Config:
    stash_url: str = ""
    api_key: str = ""
    llm_url: str = DEFAULT_LLM_URL
    model: str = DEFAULT_MODEL
    num_ctx: int = 0              # 0 = what the server has set (OLLAMA_CONTEXT_LENGTH)
    temperature: float = 0.2
    think: bool = False           # Qwen's hidden reasoning: better plans, much slower answers
    keep_alive: str = "30m"
    max_steps: int = 24           # tool calls per request before the model has to answer
    judge_batch: int = 60         # bookmarks per model call when checking a set one by one
    web: bool = True              # the model may read web pages and check links
    web_search: str = "duckduckgo"   # "duckduckgo", the address of a SearXNG instance, or "off"
    web_private: bool = False     # also fetch addresses in the local network (192.168.x, 10.x, VPN)
    timeout: float = 900.0
    rules_path: Path = field(default_factory=lambda: config_dir() / "rules.md")
    path: Path = field(default_factory=lambda: config_dir() / "config.toml")

    def rules(self) -> str:
        try:
            text = self.rules_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        return "\n".join(lines)[:6000]


def load(path: Path | None = None) -> Config:
    cfg = Config()
    if path:
        cfg.path = path
    data: dict = {}
    if cfg.path.exists():
        data = tomllib.loads(cfg.path.read_text(encoding="utf-8"))
    stash, llm, web = data.get("stash", {}), data.get("llm", {}), data.get("web", {})
    cfg.web = bool(web.get("enabled", cfg.web))
    cfg.web_search = str(web.get("search", cfg.web_search))
    cfg.web_private = bool(web.get("allow_private", cfg.web_private))
    cfg.stash_url = str(stash.get("url", "")).rstrip("/")
    cfg.api_key = str(stash.get("key", ""))
    # like the ollama command: [llm] url, else OLLAMA_HOST, else 127.0.0.1:11434
    cfg.llm_url = str(llm.get("url") or ollama_host_url(os.environ.get("OLLAMA_HOST", ""))).rstrip("/")
    cfg.model = str(llm.get("model", cfg.model))
    cfg.num_ctx = int(llm.get("num_ctx", cfg.num_ctx))
    cfg.temperature = float(llm.get("temperature", cfg.temperature))
    cfg.think = bool(llm.get("think", cfg.think))
    cfg.keep_alive = str(llm.get("keep_alive", cfg.keep_alive))
    cfg.max_steps = int(llm.get("max_steps", cfg.max_steps))
    cfg.judge_batch = max(5, min(200, int(llm.get("judge_batch", cfg.judge_batch))))
    cfg.timeout = float(llm.get("timeout", cfg.timeout))
    if data.get("rules_path"):
        cfg.rules_path = Path(str(data["rules_path"])).expanduser()
    # the environment wins, so a key never has to be written to disk
    cfg.stash_url = os.environ.get("STASHAI_URL", cfg.stash_url).rstrip("/")
    cfg.api_key = os.environ.get("STASHAI_KEY", cfg.api_key)
    cfg.model = os.environ.get("STASHAI_MODEL", cfg.model)
    return cfg


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_login(cfg: Config, url: str, key: str) -> None:
    """Write the Stash address and key, keeping any [llm] settings already there."""
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.path.parent, 0o700)
    old = cfg.path.read_text(encoding="utf-8") if cfg.path.exists() else ""
    rest, skipping = [], False
    for line in old.splitlines():
        if line.strip().startswith("["):
            skipping = line.strip() == "[stash]"
        if not skipping:
            rest.append(line)
    if not old:
        rest = ["", "[llm]", f"# url = {_q(DEFAULT_LLM_URL)}      # Ollama on this computer (only loopback is accepted)",
                f"# model = {_q(DEFAULT_MODEL)}", "# num_ctx = 0           # 0 = the server's OLLAMA_CONTEXT_LENGTH",
                "# think = false         # true: slower, sometimes better plans", "# judge_batch = 60",
                "", "[web]", "# enabled = true        # the model may read pages and check links",
                '# search = "duckduckgo" # or the address of your SearXNG, or "off"',
                "# allow_private = false # true: also addresses in your local network / VPN"]
    text = "\n".join(["[stash]", f"url = {_q(url.rstrip('/'))}", f"key = {_q(key)}", *rest]).rstrip() + "\n"
    fd = os.open(cfg.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(cfg.path, 0o600)
    if not cfg.rules_path.exists():
        cfg.rules_path.write_text(EXAMPLE_RULES, encoding="utf-8")
