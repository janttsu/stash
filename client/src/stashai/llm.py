"""The local language model: Ollama on this computer (an OpenAI-compatible local server also works).

Like sorto, stashai only talks to a model on this machine: every address the model URL resolves
to must be loopback, and proxy settings are ignored. Your bookmarks are never sent to a cloud model.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx


class LLMError(RuntimeError):
    pass


class Cancelled(Exception):
    """The user pressed Esc while the model was answering."""


class NotLocalError(ValueError):
    pass


def ensure_local_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotLocalError(f"the model URL must be http(s)://host:port ({url!r})")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise NotLocalError(f"cannot resolve the model host {parts.hostname!r}: {e}") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%", 1)[0]).is_loopback:
            raise NotLocalError(f"the model host {parts.hostname!r} is not this computer ({info[4][0]}); "
                                "stashai only uses a local model (127.0.0.1 / localhost)")
    return url.rstrip("/").removesuffix("/v1")


class LLM:
    def __init__(self, url: str, model: str, *, num_ctx: int = 0, temperature: float = 0.2, think: bool = False,
                 keep_alive: str = "30m", timeout: float = 900.0):
        self.base = ensure_local_url(url)
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.think = think
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.native: bool | None = None       # Ollama's own API; asked on first use
        self.send_think = True
        self.send_schema = True
        self.last_stats: dict[str, float] = {}

    def _http(self, timeout: float | None = None) -> httpx.Client:
        # trust_env=False: never through HTTP(S)_PROXY to another machine
        t = httpx.Timeout(timeout or self.timeout, connect=5.0)
        return httpx.Client(timeout=t, trust_env=False, follow_redirects=False)

    def is_ollama(self) -> bool:
        if self.native is None:
            try:
                with self._http(5) as h:
                    r = h.get(self.base + "/api/version")
                self.native = r.status_code == 200 and "version" in r.json()
            except (httpx.HTTPError, ValueError):
                return False
        return self.native

    # --- chat ------------------------------------------------------------------

    def chat(self, messages: list[dict[str, str]], *, schema: dict | None = None, temperature: float | None = None,
             max_tokens: int = 1500, cancel: threading.Event | None = None,
             on_progress: Callable[[int], None] | None = None) -> str:
        t0 = time.monotonic()
        if self.is_ollama():
            text = self._chat_native(messages, schema, temperature, max_tokens, cancel, on_progress)
        else:
            text = self._chat_openai(messages, temperature, max_tokens)
        self.last_stats["seconds"] = time.monotonic() - t0
        return text

    def _chat_native(self, messages, schema, temperature, max_tokens, cancel, on_progress) -> str:
        options: dict[str, Any] = {"temperature": self.temperature if temperature is None else temperature,
                                   "num_predict": max_tokens}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True, "options": options,
                                "format": schema if (schema and self.send_schema) else "json"}
        if self.send_think:
            body["think"] = self.think
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive
        parts: list[str] = []
        chunks = 0
        with self._http() as h, h.stream("POST", self.base + "/api/chat", json=body) as r:
            if r.status_code >= 400:
                text = r.read().decode("utf-8", "replace")
                if r.status_code == 400 and self.send_think and "think" in text.lower():
                    self.send_think = False
                    return self._chat_native(messages, schema, temperature, max_tokens, cancel, on_progress)
                if r.status_code in (400, 500) and schema and self.send_schema and "format" in text.lower():
                    self.send_schema = False
                    return self._chat_native(messages, schema, temperature, max_tokens, cancel, on_progress)
                raise LLMError(f"model server {r.status_code}: {text[:300]}")
            for line in r.iter_lines():
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise LLMError(str(data["error"])[:300])
                msg = data.get("message") or {}
                if msg.get("content"):
                    parts.append(msg["content"])
                if msg.get("content") or msg.get("thinking"):
                    chunks += 1
                    if on_progress and chunks % 8 == 0:
                        on_progress(chunks)
                if data.get("done"):
                    self.last_stats = {"prompt_tokens": data.get("prompt_eval_count", 0),
                                       "answer_tokens": data.get("eval_count", 0)}
                    break
        text = "".join(parts)
        if not text.strip():
            raise LLMError("the model gave an empty answer (all of it went to thinking? set think = false)")
        return text

    def _chat_openai(self, messages, temperature, max_tokens) -> str:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                "temperature": self.temperature if temperature is None else temperature,
                "response_format": {"type": "json_object"}}
        with self._http() as h:
            r = h.post(self.base + "/v1/chat/completions", json=body)
            if r.status_code == 400:
                body.pop("response_format")
                r = h.post(self.base + "/v1/chat/completions", json=body)
        if r.status_code >= 400:
            raise LLMError(f"model server {r.status_code}: {r.text[:300]}")
        try:
            return str(r.json()["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"unexpected answer from the model server: {r.text[:300]}") from e

    # --- checks ----------------------------------------------------------------

    def models(self) -> list[str]:
        with self._http(8) as h:
            if self.is_ollama():
                r = h.get(self.base + "/api/tags")
                return [m.get("name") or m.get("model") or "" for m in r.json().get("models", [])]
            r = h.get(self.base + "/v1/models")
            return [m["id"] for m in r.json().get("data", [])]

    def model_details(self) -> list[dict]:
        """Every model the server has: name, size in GB, parameters, quantization, whether it is loaded now."""
        if not self.is_ollama():
            return [{"name": n, "size_gb": None, "params": "", "quant": "", "loaded": False} for n in self.models()]
        with self._http(8) as h:
            tags = h.get(self.base + "/api/tags").json().get("models", [])
            try:
                loaded = {m.get("name") for m in h.get(self.base + "/api/ps").json().get("models", [])}
            except (httpx.HTTPError, ValueError):
                loaded = set()
        out = []
        for m in tags:
            name = m.get("name") or m.get("model") or ""
            d = m.get("details") or {}
            out.append({"name": name, "size_gb": round((m.get("size") or 0) / 1e9, 1) or None,
                        "params": d.get("parameter_size", ""), "quant": d.get("quantization_level", ""),
                        "loaded": name in loaded})
        return sorted(out, key=lambda m: m["name"])

    def context_length(self) -> int | None:
        """The context the model runs with: num_ctx if set, else what the server loaded it with."""
        if self.num_ctx:
            return self.num_ctx
        if not self.is_ollama():
            return None
        try:
            with self._http(8) as h:
                for m in h.get(self.base + "/api/ps").json().get("models", []):
                    if m.get("name") == self.model and m.get("context_length"):
                        return int(m["context_length"])
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return None

    def health(self) -> tuple[bool, str]:
        try:
            names = self.models()
        except (httpx.HTTPError, ValueError) as e:
            return False, f"no model server at {self.base} ({e}). Is Ollama running?"
        bare = lambda n: n.removesuffix(":latest")  # noqa: E731
        found = next((n for n in names if bare(n) == bare(self.model)), None)
        if not found:
            have = ", ".join(sorted(names)[:8]) or "no models"
            return False, (f"the model server at {self.base} does not have {self.model} (it has: {have}). "
                           f"If `ollama pull` went to another server (OLLAMA_HOST), put that address in "
                           f"[llm] url in ~/.config/stashai/config.toml; otherwise run: ollama pull {self.model}")
        self.model = found
        return True, f"{self.model} at {self.base}"


def extract_json(text: str) -> dict:
    """The first JSON object in a reply (models sometimes wrap it in prose or ``` fences)."""
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        break
                    break
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object in the model's answer: {text[:200]!r}")
