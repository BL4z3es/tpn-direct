from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tpn.bootstrap import Agent, BootstrapError, ConnectOptions, Phase, Transport
from tpn.daemon import DaemonError
from tpn.discovery import NoServerAvailable, ServerCandidate
from tpn.probe import ProbeResult
from tpn.state import StateStore
from tpn.tailscale import Peer, TailscaleError

from .conftest import status

PROXY = "http://127.0.0.1:9080"


class FakeTor:
    proxy_url = PROXY

    def __init__(self) -> None:
        self.running = False
        self.starts = 0

    def start(self, timeout: float = 0) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False

    def is_running(self) -> bool:
        return self.running


class FakeDaemon:
    def __init__(self) -> None:
        self.applied: list[str | None] = []
        self.shut_down = False

    @property
    def proxy(self) -> str | None:
        return self.applied[-1] if self.applied else None

    def apply(self, proxy: str | None) -> None:
        self.applied.append(proxy)

    def shutdown(self) -> None:
        self.shut_down = True


class FakeTs:
    """Status depends on which transport tailscaled is currently using."""

    def __init__(self, daemon: FakeDaemon, *, logged_in: bool = True) -> None:
        self.daemon = daemon
        self.logged_in = logged_in
        self.direct_works = True
        self.exit_node_online = True
        self.ups: list[dict[str, Any]] = []
        self.exit_nodes: list[str | None] = []

    def status(self) -> dict[str, Any]:
        if not self.logged_in:
            return status(backend="NeedsLogin")
        healthy = self.daemon.proxy is not None or self.direct_works
        health = [] if healthy else ["unable to connect to the Tailscale coordination server"]
        en = self.exit_node_online if self.exit_nodes and self.exit_nodes[-1] else None
        return status(health=health, exit_node_online=en)

    def up(self, **kwargs: Any) -> None:
        self.ups.append(kwargs)
        self.logged_in = True

    def set_exit_node(self, ip: str | None, *, allow_lan: bool = True) -> None:
        self.exit_nodes.append(ip)


class FakeProbe:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.runs = 0

    def run(self) -> list[ProbeResult]:
        self.runs += 1
        return [ProbeResult("https://controlplane.tailscale.com", self.ok, "x")]


class FakeSelector:
    def __init__(self) -> None:
        self.hosts = ["vpn1"]

    def select(self, status: dict[str, Any]) -> ServerCandidate:
        if not self.hosts:
            raise NoServerAvailable("none")
        host = self.hosts.pop(0) if len(self.hosts) > 1 else self.hosts[0]
        ip = {"vpn1": "100.64.0.10", "vpn2": "100.64.0.11"}[host]
        return ServerCandidate(Peer(host, host, (ip,), True, ("tag:vpn-server",), True), 5.0, {})


@pytest.fixture
def parts(clock, tmp_path):
    daemon = FakeDaemon()
    return {
        "tor": FakeTor(),
        "daemon": daemon,
        "ts": FakeTs(daemon),
        "probe": FakeProbe(),
        "selector": FakeSelector(),
        "store": StateStore(tmp_path / "state.json"),
        "clock": clock,
        "sleep": clock.sleep,
    }


def make(parts, **opts) -> Agent:
    return Agent(**parts, options=ConnectOptions(**opts))


def test_happy_path_goes_direct_and_stops_tor(parts):
    agent = make(parts)
    agent.connect()
    assert parts["daemon"].applied == [PROXY, None]
    assert not parts["tor"].running
    assert agent.transport is Transport.DIRECT
    assert agent.phase is Phase.CONNECTED
    assert parts["ts"].exit_nodes == ["100.64.0.10"]
    saved = parts["store"].read()
    assert saved["phase"] == "connected" and saved["server"] == "vpn1"


def test_exit_node_is_only_set_after_probing(parts):
    order = []
    parts["probe"].run = lambda: order.append("probe") or [ProbeResult("t", True, "")]
    original = parts["ts"].set_exit_node
    parts["ts"].set_exit_node = lambda ip, **k: order.append("exit") or original(ip, **k)
    make(parts).connect()
    assert order == ["probe", "exit"]


def test_blocked_direct_path_keeps_tor(parts):
    parts["probe"].ok = False
    agent = make(parts)
    agent.connect()
    assert parts["daemon"].applied == [PROXY]
    assert parts["tor"].running
    assert agent.transport is Transport.TOR
    assert "blocked" in agent.detail
    assert agent.phase is Phase.CONNECTED


def test_direct_verification_failure_rolls_back_to_tor(parts):
    parts["ts"].direct_works = False  # probe passes, but tailscaled can't hold the connection
    agent = make(parts, direct_verify_timeout=10)
    agent.connect()
    assert parts["daemon"].applied == [PROXY, None, PROXY]
    assert parts["tor"].running
    assert agent.transport is Transport.TOR
    assert agent.phase is Phase.CONNECTED


def test_keep_tor_skips_probe(parts):
    agent = make(parts, keep_tor=True)
    agent.connect()
    assert parts["probe"].runs == 0
    assert agent.transport is Transport.TOR


def test_no_tor_connects_directly(parts):
    agent = make(parts, use_tor=False)
    agent.connect()
    assert parts["tor"].starts == 0
    assert parts["daemon"].applied == [None]
    assert agent.transport is Transport.DIRECT


def test_login_uses_auth_key_only_when_needed(parts):
    parts["ts"].logged_in = False
    make(parts, auth_key_file=Path("/k")).connect()
    assert parts["ts"].ups[0]["auth_key_file"] == Path("/k")

    parts["daemon"].applied.clear()
    make(parts, auth_key_file=Path("/k")).connect()
    assert parts["ts"].ups[1]["auth_key_file"] is None


def test_needs_login_without_key_fails(parts):
    parts["ts"].logged_in = False
    agent = make(parts)
    with pytest.raises(BootstrapError, match="auth key"):
        agent.connect()
    assert agent.phase is Phase.FAILED
    assert parts["store"].read()["phase"] == "failed"


def test_no_server_fails_connect(parts):
    parts["selector"].hosts = []
    with pytest.raises(NoServerAvailable):
        make(parts).connect()


def test_watchdog_falls_back_to_tor_after_repeated_control_loss(parts):
    agent = make(parts, watchdog_failures=3)
    agent.connect()
    parts["ts"].direct_works = False
    agent.tick()
    agent.tick()
    assert agent.transport is Transport.DIRECT
    agent.tick()
    assert agent.transport is Transport.TOR
    assert parts["tor"].running
    assert parts["daemon"].applied[-1] == PROXY


def test_watchdog_does_not_fall_back_when_tor_disabled(parts):
    agent = make(parts, use_tor=False, watchdog_failures=1)
    agent.connect()
    parts["ts"].direct_works = False
    agent.tick()
    assert agent.transport is Transport.DIRECT
    assert parts["tor"].starts == 0


def test_watchdog_restarts_dead_tor(parts):
    parts["probe"].ok = False
    agent = make(parts)
    agent.connect()
    parts["tor"].running = False
    agent.tick()
    assert parts["tor"].running and parts["tor"].starts == 2


def test_watchdog_reselects_when_exit_node_goes_offline(parts):
    parts["selector"].hosts = ["vpn1", "vpn2"]
    agent = make(parts)
    agent.connect()
    parts["ts"].exit_node_online = False
    agent.tick()
    assert parts["ts"].exit_nodes == ["100.64.0.10", "100.64.0.11"]
    assert agent.server == "vpn2"


def test_watchdog_restarts_unresponsive_tailscaled_on_current_transport(parts):
    parts["probe"].ok = False
    agent = make(parts)
    agent.connect()

    def broken():
        raise TailscaleError("socket gone")

    parts["ts"].status = broken
    agent.tick()  # must not raise
    assert agent.phase is Phase.CONNECTED
    assert parts["daemon"].applied == [PROXY, PROXY]


def test_daemon_failure_while_going_direct_rolls_back_to_tor(parts):
    daemon = parts["daemon"]
    original = daemon.apply

    def apply(proxy):
        original(proxy)
        if proxy is None:
            raise DaemonError("tailscaled did not answer")

    daemon.apply = apply
    agent = make(parts)
    agent.connect()
    assert daemon.applied == [PROXY, None, PROXY]
    assert agent.transport is Transport.TOR
    assert parts["tor"].running


def test_teardown_clears_exit_node_and_stops_everything(parts):
    parts["probe"].ok = False
    agent = make(parts)
    agent.connect()
    agent.teardown()
    assert parts["ts"].exit_nodes[-1] is None
    assert parts["daemon"].shut_down
    assert not parts["tor"].running
    assert parts["store"].read()["phase"] == "disconnected"
