"""One log file per session: requests, every model reply and tool result, plans and changes."""
from __future__ import annotations

import os
import time
from pathlib import Path

from stashai.config import state_dir


class RunLog:
    def __init__(self, directory: Path | None = None):
        directory = directory or state_dir() / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / time.strftime("session-%Y%m%d-%H%M%S.log")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._f = os.fdopen(fd, "a", encoding="utf-8")

    def __call__(self, text: str) -> None:
        self._f.write(text.rstrip("\n") + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()
