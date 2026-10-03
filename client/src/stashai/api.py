"""Client for Stash's /api/v1 (API key in the Authorization header)."""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx

from stashai import __version__


class StashError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"Stash {status}: {code}")
        self.status, self.code = status, code


MESSAGES = {
    "bad_api_key": "the API key is not valid (revoked?). Run: stashai login",
    "api_key_required": "no API key. Run: stashai login",
    "read_only_key": "this API key can only read; create a key with “Allow changes” in Stash → Settings",
    "conflict": "later changes touched the same bookmarks",
    "already_undone": "this change has already been undone",
    "too_many_attempts": "too many failed attempts, try again in a few minutes",
}


def check_url(url: str) -> str:
    """https, or plain http only to this machine or the local network."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname:
        host = parts.hostname
        try:
            if host == "localhost" or ipaddress.ip_address(host).is_private:
                return url.rstrip("/")
        except ValueError:
            pass
    raise ValueError(f"the Stash address must start with https:// ({url!r}); the key would travel in clear text")


class StashAPI:
    def __init__(self, url: str, key: str, *, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self.url = check_url(url)
        self.http = httpx.Client(
            base_url=self.url + "/api/v1", timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {key}", "User-Agent": f"stashai/{__version__}"},
        )

    def _call(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise StashError(0, f"cannot reach Stash: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            code = detail if isinstance(detail, str) else str(detail)[:300]
            raise StashError(r.status_code, MESSAGES.get(code, code))
        return r.json()

    def me(self) -> dict:
        return self._call("GET", "/me")

    def snapshot(self) -> dict:
        return self._call("GET", "/snapshot")

    def changes(self, ops: list[dict], summary: str, *, dry_run: bool) -> dict:
        return self._call("POST", "/changes", json={"ops": ops, "summary": summary[:500], "dry_run": dry_run})

    def history(self, limit: int = 20) -> list[dict]:
        return self._call("GET", "/changes", params={"limit": limit})["changes"]

    def undo(self, changeset: int, *, force: bool = False) -> dict:
        return self._call("POST", f"/changes/{changeset}/undo", params={"force": "true"} if force else None)

    def close(self) -> None:
        self.http.close()
