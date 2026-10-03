import sys

import httpx

from stashai import __version__, cli
from stashai.config import Config


def fake_client(version):
    def get(url, timeout):
        assert url == "https://stash.test/api/client"
        return httpx.Response(200, json={"wheel": f"https://stash.test/dl/stashai-{version}-py3-none-any.whl",
                                         "version": version}, request=httpx.Request("GET", url))
    return get


def test_update_installs_a_newer_wheel(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(httpx, "get", fake_client("9.0.0"))
    monkeypatch.setattr("subprocess.call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(sys, "prefix", "/home/u/.local/share/pipx/venvs/stashai")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pipx")
    assert cli.cmd_update(Config(stash_url="https://stash.test"), None, False) == 0
    assert calls == [["pipx", "install", "--force", "https://stash.test/dl/stashai-9.0.0-py3-none-any.whl"]]
    assert "Updated to stashai 9.0.0" in capsys.readouterr().out


def test_update_outside_pipx_uses_pip(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "get", fake_client("9.0.0"))
    monkeypatch.setattr("subprocess.call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(sys, "prefix", "/tmp/venv")
    cli.cmd_update(Config(), "https://stash.test", False)
    assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]


def test_nothing_to_update(monkeypatch, capsys):
    monkeypatch.setattr(httpx, "get", fake_client(__version__))
    monkeypatch.setattr("subprocess.call", lambda cmd: 1 / 0)
    assert cli.cmd_update(Config(stash_url="https://stash.test"), None, False) == 0
    assert "is the newest version" in capsys.readouterr().out
    monkeypatch.setattr(httpx, "get", fake_client("9.0.0"))
    assert cli.cmd_update(Config(stash_url="https://stash.test"), None, True) == 0  # --check installs nothing
    assert cli.newer_version("https://stash.test")[0] == "9.0.0"
