"""The CLI commands that drive the daemon, against a fake ControlClient."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from tpn import cli
from tpn.control import ControlError, DaemonNotRunning


class FakeClient:
    def __init__(self, statuses: list[dict[str, Any]] | None = None, running: bool = True):
        self.calls: list[tuple[str, Any]] = []
        self.statuses = statuses or [{"phase": "connected", "server": "vpn-1", "transport": "tor"}]
        self.running = running

    def _check(self):
        if not self.running:
            raise DaemonNotRunning()

    def is_running(self):
        return self.running

    def connect(self, **kw):
        self._check()
        self.calls.append(("connect", kw))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def status(self):
        self._check()
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def disconnect(self):
        self._check()
        self.calls.append(("disconnect", None))
        return {"phase": "disconnected"}

    def set_auth_key(self, key):
        self.calls.append(("set_auth_key", key))

    def clear_auth_key(self):
        self.calls.append(("clear_auth_key", None))

    def prefs(self):
        return {"use_tor": True, "server": None}

    def update_prefs(self, **patch):
        self.calls.append(("update_prefs", patch))
        return {"use_tor": True, "server": None, **patch}

    def servers(self):
        return [
            {
                "hostname": "vpn-1",
                "ipv4": "100.64.0.10",
                "online": True,
                "healthy": True,
                "latency_ms": 41.2,
                "selected": True,
            }
        ]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TPN_STATE_DIR", str(tmp_path))
    fake = FakeClient()
    monkeypatch.setattr(cli, "_control", lambda settings: fake)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return fake


runner = CliRunner()


def test_up_waits_until_connected(client):
    client.statuses = [
        {"phase": "tor_starting", "detail": ""},
        {"phase": "probing", "detail": ""},
        {"phase": "connected", "server": "vpn-1", "transport": "direct"},
    ]
    result = runner.invoke(cli.app, ["up", "--server", "vpn-1", "--no-tor"])
    assert result.exit_code == 0, result.output
    assert client.calls[0] == ("connect", {"server": "vpn-1", "use_tor": False, "keep_tor": None})
    assert "tor_starting" in result.output and "connected via vpn-1 (direct)" in result.output


def test_up_reports_failure(client):
    client.statuses = [{"phase": "failed", "detail": "no server", "retry_at": 123.0}]
    result = runner.invoke(cli.app, ["up"])
    assert result.exit_code == 1
    assert "no server" in result.output and "will retry" in result.output


def test_up_without_daemon(client):
    client.running = False
    result = runner.invoke(cli.app, ["up", "--no-wait"])
    assert result.exit_code == 1 and "not running" in result.output


def test_up_conflict_message(client, monkeypatch):
    def busy(**kw):
        raise ControlError(409, "already connecting or connected; disconnect first")

    monkeypatch.setattr(client, "connect", busy)
    result = runner.invoke(cli.app, ["up"])
    assert result.exit_code == 1 and "already connecting" in result.output


def test_down(client):
    result = runner.invoke(cli.app, ["down"])
    assert result.exit_code == 0 and client.calls == [("disconnect", None)]


def test_disconnect_forwards_to_the_daemon(client):
    result = runner.invoke(cli.app, ["disconnect"])
    assert result.exit_code == 0 and client.calls == [("disconnect", None)]


def test_status_prefers_the_daemon(client):
    client.statuses = [{"phase": "connected", "server": "vpn-1", "transport": "tor"}]
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0 and "vpn-1" in result.output


def test_status_falls_back_to_state_file(client):
    client.running = False
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 1 and "not running" in result.output


def test_set_key_reads_stdin(client):
    result = runner.invoke(cli.app, ["set-key"], input="tskey-auth-abc\n")
    assert result.exit_code == 0, result.output
    assert client.calls == [("set_auth_key", "tskey-auth-abc")]
    assert "tskey" not in result.output


def test_set_key_refuses_empty_input(client):
    result = runner.invoke(cli.app, ["set-key"], input="\n")
    assert result.exit_code == 1 and client.calls == []


def test_set_key_clear(client):
    result = runner.invoke(cli.app, ["set-key", "--clear"])
    assert result.exit_code == 0 and client.calls == [("clear_auth_key", None)]


def test_prefs_set_parses_json_values(client):
    result = runner.invoke(
        cli.app, ["prefs", "set", "keep_tor=true", "server=vpn-fra-1", 'tor_bridges=["a"]']
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("update_prefs", {"keep_tor": True, "server": "vpn-fra-1", "tor_bridges": ["a"]})
    ]


def test_prefs_show(client):
    result = runner.invoke(cli.app, ["prefs"])
    assert result.exit_code == 0 and "use_tor" in result.output


def test_prefs_set_rejects_bad_items(client):
    result = runner.invoke(cli.app, ["prefs", "set", "nonsense"])
    assert result.exit_code != 0


def test_servers_table(client):
    result = runner.invoke(cli.app, ["servers"])
    assert result.exit_code == 0 and "* vpn-1" in result.output and "41 ms" in result.output


def test_connect_refuses_while_the_daemon_runs(client, monkeypatch):
    monkeypatch.setattr(cli.plat, "is_admin", lambda: True)
    result = runner.invoke(cli.app, ["connect"])
    assert result.exit_code == 1
