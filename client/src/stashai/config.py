"""Settings: ~/.config/stashai/config.toml (written by `stashai login`, readable only by you).

An [llm] section from stashai before 0.2 is ignored: the language model now lives in your MCP client.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

EXAMPLE_RULES = """\
# Your standing rules for Stash, in plain language and in any language.
# `stashai mcp` hands them to your MCP client's model, which follows them in every change.
# Lines starting with # are ignored, so remove the # in front of an example to use it.

# - Recipes always have the tag food and no other tags.
# - Bookmarks about a single company are tagged with the company's name.
# - Never delete bookmarks that are on the Dashboard tab "Start".
"""


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "stashai"


@dataclass
class Config:
    stash_url: str = ""
    api_key: str = ""
    web: bool = True              # pages may be read and links checked from this computer
    web_private: bool = False     # also fetch addresses in the local network (192.168.x, 10.x, VPN)
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
    stash, web = data.get("stash", {}), data.get("web", {})
    cfg.web = bool(web.get("enabled", cfg.web))
    cfg.web_private = bool(web.get("allow_private", cfg.web_private))
    cfg.stash_url = str(stash.get("url", "")).rstrip("/")
    cfg.api_key = str(stash.get("key", ""))
    if data.get("rules_path"):
        cfg.rules_path = Path(str(data["rules_path"])).expanduser()
    # the environment wins, so a key never has to be written to disk
    cfg.stash_url = os.environ.get("STASHAI_URL", cfg.stash_url).rstrip("/")
    cfg.api_key = os.environ.get("STASHAI_KEY", cfg.api_key)
    return cfg


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_login(cfg: Config, url: str, key: str) -> None:
    """Write the Stash address and key, keeping any other settings already there."""
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
        rest = ["", "[web]", "# enabled = true        # pages may be read and links checked from this computer",
                "# allow_private = false # true: also addresses in your local network / VPN"]
    text = "\n".join(["[stash]", f"url = {_q(url.rstrip('/'))}", f"key = {_q(key)}", *rest]).rstrip() + "\n"
    fd = os.open(cfg.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(cfg.path, 0o600)
    if not cfg.rules_path.exists():
        cfg.rules_path.write_text(EXAMPLE_RULES, encoding="utf-8")
