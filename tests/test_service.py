"""The daemon: service core, the HTTP control API end to end, and run()."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from tpn.bootstrap import NeedsLoginError, Phase, Transport
from tpn.config import Settings
from tpn.control import ControlClient, ControlError, DaemonNotRunning, Unauthorized
from tpn.prefs import PrefsStore
from tpn.proc import CmdResult
from tpn.service import run
from tpn.service.acl import FileSecurity
from tpn.service.core import Service, ServiceError
from tpn.service.server import ControlServer
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient

from .conftest import FakeRunner, ok_json, peer, status

WAIT = 5.0


def eventually(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeAgent:
    """Walks through the phases, then holds 'connected' until should_stop()."""

    def __init__(self, kwargs, *, fail: Exception | None = None):
        self.kwargs = kwargs
        self.options = type("Opts", (), {"auth_key_file": kwargs["auth_key_file"]})()
        self.fail = fail
        self.detail = ""
        self.transport: Transport | None = None
        self.server = self.server_ip = None
        self.torn_down = threading.Event()

    def _set(self, phase):
        self.kwargs["on_phase"](phase)

    def run_forever(self, interval, *, should_stop):
        try:
            self._set(Phase.TOR_STARTING)
            self._set(Phase.TAILNET_UP)
            if self.fail is not None:
                self.detail = str(self.fail)
                raise self.fail
            self.transport = Transport.DIRECT
            self.server, self.server_ip = "vpn-1", "100.64.0.10"
            self._set(Phase.CONNECTED)
            while not should_stop():
                time.sleep(0.01)
        finally:
            self.transport = None
            self.server = self.server_ip = None
            self._set(Phase.DISCONNECTED)
            self.torn_down.set()


class Factory:
    def __init__(self, *failures: Exception | None):
        self.failures = list(failures)
        self.agents: list[FakeAgent] = []

    def __call__(self, settings, prefs, **kwargs):
        kwargs["prefs"] = prefs
        fail = self.failures.pop(0) if self.failures else None
        agent = FakeAgent(kwargs, fail=fail)
        self.agents.append(agent)
        return agent


class Waits:
    """Records backoff waits; returns immediately (or blocks until cancelled when asked)."""

    def __init__(self, block_after: int = 10_000):
        self.delays: list[float] = []
        self.block_after = block_after

    def __call__(self, event: threading.Event, seconds: float) -> bool:
        self.delays.append(seconds)
        if len(self.delays) > self.block_after:
            return event.wait(WAIT)
        return event.is_set()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("TPN_AUTH_KEY", raising=False)
    return Settings(state_dir=tmp_path, daemon_mode="managed", control_port=0)


def ts_client(backend="Running", peers=()):
    def handler(args):
        if "status" in args:
            return ok_json(status(backend=backend, peers=peers))
        return CmdResult(0, "", "")

    return TailscaleClient(FakeRunner(handler))


def make_service(settings, factory=None, waits=None, **kw):
    kw.setdefault("ts", ts_client())
    return Service(
        settings,
        prefs_store=PrefsStore(settings.prefs_file),
        agent_factory=factory or Factory(),
        security=FileSecurity(FakeRunner(), is_windows=False),
        wait=waits or Waits(),
        **kw,
    )


# -- core --------------------------------------------------------------------------------------


def test_connect_then_disconnect(settings):
    factory = Factory()
    svc = make_service(settings, factory)
    first = svc.connect(server="vpn-1", keep_tor=True)
    assert first["phase"] not in ("disconnected", "failed")
    assert eventually(lambda: svc.status()["phase"] == "connected")
    st = svc.status()
    assert (st["server"], st["server_ip"], st["transport"]) == ("vpn-1", "100.64.0.10", "direct")
    assert st["connected_since"] is not None and st["logged_in"] is True
    kwargs = factory.agents[0].kwargs
    assert kwargs["server"] == "vpn-1" and kwargs["keep_tor"] is True and kwargs["use_tor"] is None
    with pytest.raises(ServiceError) as e:
        svc.connect()
    assert e.value.status == 409

    down = svc.disconnect()
    assert down["phase"] == "disconnected" and down["server"] is None
    assert factory.agents[0].torn_down.is_set()


def test_retries_with_backoff_until_disconnect(settings):
    factory = Factory(*[RuntimeError("tor did not bootstrap")] * 6)
    waits = Waits(block_after=5)
    svc = make_service(settings, factory, waits, wall=lambda: 1000.0)
    svc.connect()
    assert eventually(lambda: len(waits.delays) == 6)
    assert waits.delays == [15, 30, 60, 120, 240, 300]
    st = svc.status()
    assert st["phase"] == "failed" and st["retry_at"] == 1300.0
    assert "tor did not bootstrap" in st["detail"]
    # disconnect cancels the pending backoff promptly
    started = time.monotonic()
    assert svc.disconnect()["phase"] == "disconnected"
    assert time.monotonic() - started < 2
    assert svc.status()["retry_at"] is None


def test_recovers_after_a_failed_attempt(settings):
    factory = Factory(RuntimeError("boom"))
    svc = make_service(settings, factory)
    svc.connect()
    assert eventually(lambda: svc.status()["phase"] == "connected")
    assert len(factory.agents) == 2
    svc.disconnect()


def test_not_logged_in_is_not_retried(settings):
    factory = Factory(NeedsLoginError("device is not logged in"))
    waits = Waits()
    svc = make_service(settings, factory, waits)
    svc.connect()
    assert eventually(lambda: svc.status()["phase"] == "failed")
    time.sleep(0.05)
    assert waits.delays == [] and len(factory.agents) == 1
    assert svc.status()["retry_at"] is None
    # the attempt is over: a new connect is allowed
    svc.connect()
    svc.disconnect()


def test_auth_key_stored_used_and_deleted_after_login(settings, caplog):
    caplog.set_level(logging.DEBUG)
    factory = Factory()
    svc = make_service(settings, factory, ts=ts_client(backend="NeedsLogin"))
    with pytest.raises(ServiceError):
        svc.set_auth_key("not-a-key")
    with pytest.raises(ServiceError):
        svc.set_auth_key(12)
    svc.set_auth_key("tskey-auth-SECRET123")
    assert settings.auth_key_file.read_text() == "tskey-auth-SECRET123"
    if os.name == "posix":
        assert settings.auth_key_file.stat().st_mode & 0o777 == 0o600
    st = svc.status()
    assert st["has_auth_key"] is True and st["logged_in"] is False
    assert "SECRET123" not in json.dumps(st)

    svc.connect()
    assert eventually(lambda: svc.status()["phase"] == "connected")
    assert factory.agents[0].kwargs["auth_key_file"] == settings.auth_key_file
    assert not settings.auth_key_file.exists()
    assert svc.status()["has_auth_key"] is False
    svc.disconnect()
    assert "SECRET123" not in caplog.text


def test_failure_detail_redacts_keys(settings):
    factory = Factory(NeedsLoginError("tailscale up --auth-key=tskey-auth-LEAK failed"))
    svc = make_service(settings, factory)
    svc.connect()
    assert eventually(lambda: svc.status()["phase"] == "failed")
    assert "LEAK" not in svc.status()["detail"]


def test_refuses_while_a_foreground_connect_runs(settings):
    StateStore(settings.state_file).write({"phase": "connected", "pid": 424242})
    svc = make_service(settings, pid_alive=lambda pid: pid == 424242)
    with pytest.raises(ServiceError) as e:
        svc.connect()
    assert e.value.status == 409 and "424242" in e.value.message
    StateStore(settings.state_file).write({"phase": "disconnected", "pid": 424242})
    svc.connect()
    svc.disconnect()


def test_prefs_apply_to_the_next_connection(settings):
    factory = Factory()
    svc = make_service(settings, factory)
    svc.update_prefs({"use_tor": False, "server": "vpn-2"})
    assert PrefsStore(settings.prefs_file).read().server == "vpn-2"
    with pytest.raises(ValueError):
        svc.update_prefs({"bogus": True})
    svc.connect()
    assert eventually(lambda: factory.agents)
    prefs = factory.agents[0].kwargs["prefs"]
    assert prefs.use_tor is False and prefs.server == "vpn-2"
    svc.disconnect()


def test_auto_connect(settings):
    PrefsStore(settings.prefs_file).update({"auto_connect": True})
    svc = make_service(settings)
    svc.start()
    assert eventually(lambda: svc.status()["phase"] == "connected")
    svc.shutdown()
    with pytest.raises(ServiceError):
        svc.connect()


def test_logs_tail(settings):
    svc = make_service(settings)
    assert svc.logs() == []
    settings.daemon_log.write_text("".join(f"line {i}\n" for i in range(3000)))
    assert svc.logs(2) == ["line 2998", "line 2999"]
    assert len(svc.logs(99999)) == 2000


def test_servers_marks_the_selected_one(settings, monkeypatch):
    peers = [peer("vpn-1", "100.64.0.10"), peer("vpn-2", "100.64.0.11", online=False)]
    svc = make_service(settings, ts=ts_client(peers=peers))
    monkeypatch.setattr(svc.inventory, "list", lambda ts, selected_ip: [selected_ip])
    assert svc.servers() == [None]
    svc.connect()
    assert eventually(lambda: svc.status()["phase"] == "connected")
    assert svc.servers() == ["100.64.0.10"]
    svc.disconnect()


def test_servers_without_tailscaled(settings):
    broken = TailscaleClient(FakeRunner(lambda a: CmdResult(1, "", "no socket")))
    svc = make_service(settings, ts=broken)
    with pytest.raises(ServiceError) as e:
        svc.servers()
    assert e.value.status == 503
    assert svc.status()["logged_in"] is None


# -- the HTTP API, end to end --------------------------------------------------------------------


@pytest.fixture
def api(settings):
    svc = make_service(settings, ts=ts_client(peers=[peer("vpn-1", "100.64.0.10")]))
    server = ControlServer(svc, token="tok", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (settings.state_dir / "control.token").write_text("tok")
    client = ControlClient(port=server.port, token_file=settings.token_file, timeout=WAIT)
    try:
        yield svc, server, client
    finally:
        svc.shutdown()
        server.shutdown()
        thread.join(WAIT)


def test_api_roundtrip(api, settings):
    _, _, client = api
    assert client.status()["phase"] == "disconnected"
    assert client.connect(server="vpn-1")["phase"] != "disconnected"
    assert eventually(lambda: client.status()["phase"] == "connected")
    with pytest.raises(ControlError) as e:
        client.connect()
    assert e.value.status == 409
    assert client.disconnect()["phase"] == "disconnected"

    assert client.update_prefs(keep_tor=True)["keep_tor"] is True
    assert client.prefs()["keep_tor"] is True
    with pytest.raises(ControlError) as e:
        client.update_prefs(nope=1)
    assert e.value.status == 400

    client.set_auth_key("tskey-auth-abc")
    assert client.status()["has_auth_key"] is True
    with pytest.raises(ControlError) as e:
        client.set_auth_key("nope")
    assert e.value.status == 400
    client.clear_auth_key()
    assert client.status()["has_auth_key"] is False

    settings.daemon_log.write_text("a\nb\nc\n")
    assert client.logs(2) == ["b", "c"]


def test_api_validates_connect_body(api):
    _, _, client = api
    for bad in ({"use_tor": "yes"}, {"server": 5}, {"other": 1}):
        with pytest.raises(ControlError) as e:
            client._request("POST", "/connect", json=bad)
        assert e.value.status == 400
    with pytest.raises(ControlError) as e:
        client._request("POST", "/connect", json=[1])
    assert e.value.status == 400


def test_api_404_405(api):
    _, _, client = api
    with pytest.raises(ControlError) as e:
        client._request("GET", "/nothing")
    assert e.value.status == 404
    with pytest.raises(ControlError) as e:
        client._request("DELETE", "/status")
    assert e.value.status == 405


def test_api_refuses_bad_token_host_and_origin(api, settings):
    import httpx

    _, server, client = api
    settings.token_file.write_text("wrong")
    with pytest.raises(Unauthorized):
        client.status()
    base = f"http://127.0.0.1:{server.port}/v1/status"
    good = {"Authorization": "Bearer tok"}
    with httpx.Client(trust_env=False, timeout=WAIT) as raw:
        assert raw.get(base, headers=good).status_code == 200
        assert raw.get(base, headers={**good, "Origin": "http://evil"}).status_code == 403
        assert raw.get(base, headers={**good, "Host": "evil.example"}).status_code == 403


def test_api_500_does_not_leak(api, monkeypatch):
    from tpn.service import server as server_mod

    _, _, client = api

    def explode(service, body, query):
        raise RuntimeError("tskey-auth-SECRET")

    monkeypatch.setitem(server_mod.ROUTES, "/status", {"GET": explode})
    with pytest.raises(ControlError) as e:
        client.status()
    assert e.value.status == 500 and "SECRET" not in e.value.message


# -- run() -------------------------------------------------------------------------------------


def test_run_serves_until_stopped(settings, monkeypatch):
    monkeypatch.setenv("TPN_AUTH_KEY", "tskey-auth-FROMENV")
    settings = Settings(state_dir=settings.state_dir, daemon_mode="managed", control_port=0)
    svc = make_service(settings)
    ports: list[int] = []
    real_init = ControlServer.__init__

    def spy(self, *a, **k):
        real_init(self, *a, **k)
        ports.append(self.port)

    monkeypatch.setattr(ControlServer, "__init__", spy)
    stop = threading.Event()
    thread = threading.Thread(
        target=run,
        args=(settings, stop),
        kwargs={"service": svc, "security": FileSecurity(FakeRunner(), is_windows=False)},
        daemon=True,
    )
    thread.start()
    try:
        assert eventually(lambda: settings.token_file.exists() and ports)
        client = ControlClient(port=ports[0], token_file=settings.token_file, timeout=WAIT)
        assert client.status()["has_auth_key"] is True
        client.connect()
        assert eventually(lambda: client.status()["phase"] == "connected")
    finally:
        stop.set()
        thread.join(WAIT)
    assert not thread.is_alive()
    assert not settings.token_file.exists()
    assert svc.status()["phase"] == "disconnected"
    log_text = Path(settings.daemon_log).read_text()
    assert "tpn daemon stopped" in log_text and "FROMENV" not in log_text
    with pytest.raises(DaemonNotRunning):
        client.status()


def test_windows_acls(tmp_path):
    runner = FakeRunner()
    sec = FileSecurity(runner, is_windows=True)
    sec.state_dir(tmp_path / "state")
    sec.token(tmp_path / "state" / "control.token", "t")
    sec.private_file(tmp_path / "state" / "authkey", "tskey-x")
    users = "*S-1-5-32-545:R"
    dir_cmd, token_cmd, key_cmd = runner.calls
    assert dir_cmd[:4] == ["icacls", str(tmp_path / "state"), "/inheritance:r", "/grant:r"]
    assert users not in dir_cmd and users in token_cmd and users not in key_cmd
    assert "*S-1-5-18:F" in key_cmd and "*S-1-5-32-544:F" in key_cmd


def test_windows_acl_failure_is_an_error(tmp_path):
    sec = FileSecurity(FakeRunner(lambda a: CmdResult(5, "", "denied")), is_windows=True)
    with pytest.raises(PermissionError):
        sec.state_dir(tmp_path)
