import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stashai.llm import LLM, Cancelled, LLMError, NotLocalError, ensure_local_url, extract_json


class FakeOllama(BaseHTTPRequestHandler):
    bodies: list = []
    reject = set()          # "think", "format": answer 400 when the request has them
    reply = '{"thought": "t", "tool": "answer", "args": {"message": "hei"}}'
    chunks = 4

    def log_message(self, *a):
        pass

    def _json(self, data, status=200):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/version":
            self._json({"version": "0.12.0"})
        elif self.path == "/api/tags":
            self._json({"models": [{"name": "qwen3.6:35b-a3b"}]})
        elif self.path == "/api/ps":
            self._json({"models": [{"name": "qwen3.6:35b-a3b", "context_length": 65536}]})
        else:
            self._json({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.bodies.append(body)
        if "think" in self.reject and "think" in body:
            return self._json({"error": "think value is not supported"}, 400)
        if "format" in self.reject and isinstance(body.get("format"), dict):
            return self._json({"error": "invalid format schema"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        text, n = self.reply, self.chunks
        size = max(1, len(text) // n + 1)
        for i in range(0, len(text), size):
            self.wfile.write(json.dumps({"message": {"content": text[i:i + size]}, "done": False}).encode() + b"\n")
            self.wfile.flush()
        self.wfile.write(json.dumps({"message": {"content": ""}, "done": True, "prompt_eval_count": 50,
                                     "eval_count": 9}).encode() + b"\n")


@pytest.fixture
def ollama():
    FakeOllama.bodies, FakeOllama.reject = [], set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    server.handle_error = lambda *a: None  # the cancel test hangs up mid-answer on purpose
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_streamed_answer_with_schema(ollama):
    llm = LLM(ollama + "/v1", "qwen3.6:35b-a3b", keep_alive="30m")
    progress = []
    text = llm.chat([{"role": "user", "content": "x"}], schema={"type": "object"}, on_progress=progress.append)
    assert json.loads(text)["args"]["message"] == "hei"
    body = FakeOllama.bodies[-1]
    assert body["format"] == {"type": "object"} and body["think"] is False and body["stream"] is True
    assert body["keep_alive"] == "30m" and "num_ctx" not in body["options"]
    assert llm.last_stats["prompt_tokens"] == 50
    assert llm.health() == (True, "qwen3.6:35b-a3b at " + ollama)
    assert llm.context_length() == 65536


def test_falls_back_when_the_server_refuses_think_or_schema(ollama):
    FakeOllama.reject = {"think", "format"}
    llm = LLM(ollama, "qwen3.6:35b-a3b", num_ctx=32768)
    llm.chat([{"role": "user", "content": "x"}], schema={"type": "object"})
    body = FakeOllama.bodies[-1]
    assert "think" not in body and body["format"] == "json" and body["options"]["num_ctx"] == 32768


def test_cancel_stops_reading(ollama):
    FakeOllama.chunks = 50
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        LLM(ollama, "m").chat([{"role": "user", "content": "x"}], cancel=stop)
    FakeOllama.chunks = 4


def test_missing_model_and_empty_answer(ollama):
    assert LLM(ollama, "llama9:1b").health()[0] is False
    FakeOllama.reply = " "
    with pytest.raises(LLMError):
        LLM(ollama, "m").chat([{"role": "user", "content": "x"}])
    FakeOllama.reply = '{"thought": "t", "tool": "answer", "args": {"message": "hei"}}'


def test_only_local_models():
    assert ensure_local_url("http://localhost:11434/v1") == "http://localhost:11434"
    assert ensure_local_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    for url in ("http://8.8.8.8:11434", "ftp://localhost", "http://"):
        with pytest.raises(NotLocalError):
            ensure_local_url(url)


def test_extract_json():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": "x \\" } y", "b": {"c": 2}}\n```') == {"a": 'x " } y', "b": {"c": 2}}
    assert extract_json('[1] then {"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_ollama_host_like_the_ollama_command(monkeypatch, tmp_path):
    from stashai.config import load, ollama_host_url

    assert ollama_host_url("") == "http://127.0.0.1:11434"
    assert ollama_host_url("127.0.0.1:11437") == "http://127.0.0.1:11437"
    assert ollama_host_url("0.0.0.0") == "http://127.0.0.1:11434"
    assert ollama_host_url("http://localhost:9999/") == "http://localhost:9999"
    assert ollama_host_url("[::]:11500") == "http://[::1]:11500"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11437")
    assert load(tmp_path / "none.toml").llm_url == "http://127.0.0.1:11437"
    (tmp_path / "c.toml").write_text('[llm]\nurl = "http://127.0.0.1:1234"\n')
    assert load(tmp_path / "c.toml").llm_url == "http://127.0.0.1:1234", "the config file wins"


def test_health_names_what_the_server_has(ollama):
    ok, msg = LLM(ollama, "qwen3.6:35b").health()
    assert not ok and "(it has: qwen3.6:35b-a3b)" in msg and "OLLAMA_HOST" in msg
    llm = LLM(ollama, "qwen3.6:35b-a3b:latest")
    assert llm.health()[0] and llm.model == "qwen3.6:35b-a3b"
