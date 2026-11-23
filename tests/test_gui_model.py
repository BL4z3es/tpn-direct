"""Headless tests for the GUI model: every phase → icon kind/headline/enabled actions.

Uses a fake ControlClient that never touches the network, so tests run without a display,
without tkinter, and without pystray.
"""

from __future__ import annotations

import time
from typing import Any

from tpn.control import ControlError, DaemonNotRunning, PrefsDict, ServerInfo, Status, Unauthorized
from tpn.gui.model import (
    ActionState,
    AppModel,
    IconKind,
    Preferences,
    ServerRow,
    ViewState,
)


class FakeClient:
    """Deterministic in-memory client that mimics the daemon's HTTP contract."""

    def __init__(self) -> None:
        self.statuses: list[Status] = []
        self._prefs = {
            "use_tor": True,
            "keep_tor": False,
            "server": None,
            "hostname": None,
            "tor_bridges": [],
            "auto_connect": False,
        }
        self._servers: list[ServerInfo] = []
        self._logs: list[str] = []
        self._status_index = 0
        self._raise_on_status: Exception | None = None
        self._raise_on_other: Exception | None = None
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def status(self) -> Status:
        self.calls.append(("status", None))
        if self._raise_on_status:
            raise self._raise_on_status
        if self.statuses:
            index = self._status_index
            if index < len(self.statuses):
                self._status_index += 1
            return {**self._default_status(), **self.statuses[min(index, len(self.statuses) - 1)]}
        return self._default_status()

    def _default_status(self) -> Status:
        return {
            "daemon": {"version": "0.3.0", "pid": 1, "started_at": time.time()},
            "phase": "disconnected",
            "transport": None,
            "server": None,
            "server_ip": None,
            "detail": "",
            "connected_since": None,
            "retry_at": None,
            "has_auth_key": False,
            "logged_in": True,
        }

    def prefs(self) -> PrefsDict:
        self.calls.append(("prefs", None))
        return self._prefs

    def update_prefs(self, **patch: Any) -> PrefsDict:
        self.calls.append(("update_prefs", patch))
        self._prefs = {**self._prefs, **patch}
        return self._prefs

    def connect(
        self,
        *,
        server: str | None = None,
        use_tor: bool | None = None,
        keep_tor: bool | None = None,
    ) -> Status:
        body = {
            k: v
            for k, v in {"server": server, "use_tor": use_tor, "keep_tor": keep_tor}.items()
            if v is not None
        }
        self.calls.append(("connect", body))
        if self._raise_on_other:
            raise self._raise_on_other
        status = self._default_status()
        status["phase"] = "tor_starting"
        return status

    def disconnect(self) -> Status:
        self.calls.append(("disconnect", None))
        if self._raise_on_other:
            raise self._raise_on_other
        status = self._default_status()
        status["phase"] = "disconnected"
        return status

    def servers(self) -> list[ServerInfo]:
        self.calls.append(("servers", None))
        return self._servers

    def set_auth_key(self, key: str) -> None:
        self.calls.append(("set_auth_key", {"auth_key": key}))
        if self._raise_on_other:
            raise self._raise_on_other

    def clear_auth_key(self) -> None:
        self.calls.append(("clear_auth_key", None))

    def logs(self, lines: int = 200) -> list[str]:
        self.calls.append(("logs", {"lines": lines}))
        return self._logs[-lines:]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_every_phase_maps_to_icon_and_headline():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "init"},
        {"phase": "tor_starting"},
        {"phase": "tor_ready"},
        {"phase": "tailnet_up"},
        {"phase": "probing"},
        {"phase": "going_direct"},
        {"phase": "connected", "transport": "direct"},
        {"phase": "connected", "transport": "tor"},
        {"phase": "failed", "detail": "tor timeout"},
        {"phase": "disconnected"},
    ]
    # No model.start(): its poll thread would race these explicit polls for the statuses.
    model = AppModel(client, clock=clock, action_runner=lambda f: f())

    expected: list[tuple[IconKind, str]] = [
        ("connecting", "Preparing connection…"),
        ("connecting", "Starting Tor…"),
        ("connecting", "Tor is ready"),
        ("connecting", "Joining the tailnet…"),
        ("connecting", "Testing the direct connection…"),
        ("connecting", "Switching to a direct connection…"),
        ("direct", "Connected directly"),
        ("tor", "Connected via Tor"),
        ("failed", "Connection failed"),
        ("disconnected", "Disconnected"),
    ]
    for i, (exp_icon, exp_headline) in enumerate(expected):
        model.poll()
        state = model.state
        assert state.icon_kind == exp_icon, f"index {i}: icon {state.icon_kind} != {exp_icon}"
        assert state.headline == exp_headline, (
            f"index {i}: headline {state.headline} != {exp_headline}"
        )


def test_actions_enabled_per_state():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "disconnected"},
        {"phase": "tor_starting"},
        {"phase": "connected", "transport": "direct", "connected_since": clock()},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # disconnected
    assert model.state.actions.connect is True
    assert model.state.actions.disconnect is False

    model.poll()  # connecting
    assert model.state.actions.connect is False
    assert model.state.actions.disconnect is True

    model.poll()  # connected
    assert model.state.actions.connect is False
    assert model.state.actions.disconnect is True


def test_daemon_down_then_recovery():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "connected", "transport": "direct"},
    ]
    client._raise_on_status = DaemonNotRunning()
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # first poll -> down
    state = model.state
    assert state.icon_kind == "failed"
    assert state.hint == "Start the 'tpn-direct VPN' service, or reinstall tpn"
    assert state.daemon_running is False

    client._raise_on_status = None
    client._status_index = 0
    model.poll()  # recovery
    state = model.state
    assert state.daemon_running is True
    assert state.icon_kind == "direct"
    assert state.hint == ""


def test_unauthorized_shows_token_hint():
    clock = FakeClock()
    client = FakeClient()
    client._raise_on_status = Unauthorized(401, "no")
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert model.state.icon_kind == "failed"
    assert "token" in model.state.hint.lower()


def test_connect_then_disconnect_calls_client():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "disconnected"},
        {"phase": "tor_starting"},
        {"phase": "connected", "transport": "direct", "connected_since": clock()},
        {"phase": "disconnected"},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert client.calls[0][0] == "status"
    model.connect()
    assert ("connect", {}) in client.calls
    model.poll()  # tor_starting
    model.poll()  # connected
    model.disconnect()
    assert ("disconnect", None) in client.calls
    model.poll()  # disconnected


def test_choose_server_while_connected_reconnects():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "connected", "transport": "direct", "connected_since": clock(), "server": "old"},
        {"phase": "tor_starting"},
        {"phase": "connected", "transport": "direct", "connected_since": clock(), "server": "new"},
    ]
    client._servers = [
        {
            "hostname": "new",
            "dns_name": "new.tail.ts.net",
            "ipv4": "100.64.0.1",
            "online": True,
            "healthy": True,
            "latency_ms": 10.0,
            "detail": "",
            "selected": False,
        }
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.choose_server("new")
    # Expect: update_prefs(server="new"), disconnect, connect
    assert ("update_prefs", {"server": "new"}) in client.calls
    assert ("disconnect", None) in client.calls
    assert ("connect", {}) in client.calls


def test_choose_server_while_disconnected_only_updates_prefs():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    client._servers = [
        {
            "hostname": "new",
            "dns_name": "new.tail.ts.net",
            "ipv4": "100.64.0.1",
            "online": True,
            "healthy": True,
            "latency_ms": 10.0,
            "detail": "",
            "selected": False,
        }
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.choose_server("new")
    assert ("update_prefs", {"server": "new"}) in client.calls
    assert ("disconnect", None) not in client.calls
    assert ("connect", {}) not in client.calls


def test_pref_toggle_updates_daemon_prefs():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.toggle_pref("use_tor", False)
    assert ("update_prefs", {"use_tor": False}) in client.calls
    model.toggle_pref("keep_tor", True)
    assert ("update_prefs", {"keep_tor": True}) in client.calls
    model.toggle_pref("auto_connect", True)
    assert ("update_prefs", {"auto_connect": True}) in client.calls


def test_control_error_400_surfaces_as_message():
    clock = FakeClock()
    client = FakeClient()
    client._raise_on_other = ControlError(409, "busy")
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.connect()
    assert "busy" in model.state.error.lower() or "409" in model.state.error


def test_poll_interval_switches_while_connecting():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "disconnected"},
        {"phase": "tor_starting"},
        {"phase": "tailnet_up"},
        {"phase": "connected", "transport": "direct", "connected_since": clock()},
    ]
    model = AppModel(
        client, clock=clock, action_runner=lambda f: f(), idle_interval=2.0, connecting_interval=0.5
    )
    model.poll()  # disconnected
    assert model.poll_interval == 2.0
    model.poll()  # tor_starting
    assert model.poll_interval == 0.5
    model.poll()  # tailnet_up
    assert model.poll_interval == 0.5
    model.poll()  # connected
    assert model.poll_interval == 2.0


def test_tooltip_length_capped_at_127():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {
            "phase": "connected",
            "transport": "direct",
            "connected_since": clock(),
            "server": "a" * 120,  # long server name
            "retry_at": clock() + 10,
        }
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert len(model.state.tooltip) <= 127


def test_auth_key_never_stored_in_model():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.set_auth_key("tskey-auth-supersecret123")
    # The key is sent but never retained in model state or view state.
    assert "supersecret" not in str(model.state)
    # The call went through with the correct argument.
    sent = [call for call in client.calls if call[0] == "set_auth_key"]
    assert sent[0][1]["auth_key"] == "tskey-auth-supersecret123"


def test_retry_countdown_displayed():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {
            "phase": "failed",
            "detail": "timeout",
            "retry_at": 1005.0,
        }
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert "Retrying in 5s" in model.state.retry or "Retrying now" in model.state.retry
    clock.now = 1005.5
    model.poll()
    assert "Retrying now" in model.state.retry


def test_save_settings_validates_hostname():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    # Invalid hostname (too long, bad chars)
    assert (
        model.save_settings(
            hostname="a" * 64, tor_bridges=[], use_tor=True, keep_tor=False, auto_connect=False
        )
        is False
    )
    assert "Hostname" in model.state.error
    # Valid hostname
    assert (
        model.save_settings(
            hostname="valid-host", tor_bridges=[], use_tor=True, keep_tor=False, auto_connect=False
        )
        is True
    )
    assert model.state.notice == "Settings saved."


def test_bridges_one_per_line():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert (
        model.save_settings(
            hostname=None,
            tor_bridges="bridge1\nbridge2\n\nbridge3  ",
            use_tor=True,
            keep_tor=False,
            auto_connect=False,
        )
        is True
    )
    # Verify the update_prefs call received a clean list
    pref_calls = [call for call in client.calls if call[0] == "update_prefs"]
    assert any("tor_bridges" in str(call[1]) and "bridge1" in str(call[1]) for call in pref_calls)


def test_refresh_logs_limits_to_500():
    clock = FakeClock()
    client = FakeClient()
    client._logs = [f"line {i}" for i in range(1000)]
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.refresh_logs()
    # Model keeps only the last 500
    assert len(model.state.logs) == 500
    assert model.state.logs[0] == "line 500"
    assert model.state.logs[-1] == "line 999"


def test_prefs_loaded_once():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}] * 5
    model = AppModel(client, clock=clock, action_runner=lambda f: f())

    for _ in range(5):
        model.poll()
    # prefs() should only be called once (the first time)
    prefs_calls = [call for call in client.calls if call[0] == "prefs"]
    assert len(prefs_calls) == 1


def test_servers_loaded_on_first_poll_when_connected():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "connected", "transport": "direct", "connected_since": clock()},
    ]
    client._servers = [
        {
            "hostname": "vpn-1",
            "dns_name": "vpn-1.tail.ts.net",
            "ipv4": "100.64.0.1",
            "online": True,
            "healthy": True,
            "latency_ms": 10.0,
            "detail": "",
            "selected": True,
        },
        {
            "hostname": "vpn-2",
            "dns_name": "vpn-2.tail.ts.net",
            "ipv4": "100.64.0.2",
            "online": True,
            "healthy": None,
            "latency_ms": None,
            "detail": "",
            "selected": False,
        },
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert len(model.state.servers) == 2
    assert model.state.servers[0].hostname == "vpn-1"
    assert model.state.servers[1].healthy is None


def test_refresh_servers_action():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    client._servers = [
        {
            "hostname": "vpn-new",
            "dns_name": "vpn-new.tail.ts.net",
            "ipv4": "100.64.0.3",
            "online": True,
            "healthy": True,
            "latency_ms": 5.0,
            "detail": "",
            "selected": False,
        },
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.refresh_servers()
    assert ("servers", None) in client.calls
    assert len(model.state.servers) == 1
    assert model.state.servers[0].hostname == "vpn-new"


def test_save_key_clears_entry():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.set_auth_key("tskey-auth-abc")
    assert "Authentication key saved." in model.state.notice
    # The key is not in any model state
    assert "abc" not in str(model.state)


def test_clear_key_clears_stored_key():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [
        {"phase": "disconnected", "has_auth_key": True},
        {"phase": "disconnected", "has_auth_key": False},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert model.state.has_auth_key is True
    model.clear_auth_key()
    assert ("clear_auth_key", None) in client.calls
    model.poll()
    assert model.state.has_auth_key is False
    assert "Authentication key cleared." in model.state.notice


def test_tick_updates_time_fields():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {
            "phase": "connected",
            "transport": "direct",
            "connected_since": 900.0,
            "retry_at": None,
        }
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    assert "1m 40s" in model.state.connected_for  # 1000 - 900
    clock.now = 1060.0
    model.tick()
    assert "2m 40s" in model.state.connected_for


def test_notification_on_connect():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {"phase": "tor_starting"},
        {
            "phase": "connected",
            "transport": "direct",
            "connected_since": clock(),
            "server": "vpn-1",
        },
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # tor_starting
    model.poll()  # connected
    notifications = model.drain_notifications()
    assert len(notifications) == 1
    assert notifications[0].title == "tpn connected"
    assert "vpn-1" in notifications[0].message


def test_notification_on_disconnect():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {"phase": "connected", "transport": "direct", "connected_since": 900.0, "server": "vpn-1"},
        {"phase": "disconnected", "detail": "user requested"},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # connected
    model.poll()  # disconnected
    notifications = model.drain_notifications()
    assert len(notifications) == 1
    assert notifications[0].title == "tpn disconnected"


def test_notification_on_failed():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {"phase": "tor_starting"},
        {"phase": "failed", "detail": "tor timeout"},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # tor_starting
    model.poll()  # failed
    notifications = model.drain_notifications()
    assert len(notifications) == 1
    assert notifications[0].title == "tpn connection failed"


def test_expected_disconnect_suppresses_notification():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {"phase": "connected", "transport": "direct", "connected_since": 900.0, "server": "vpn-1"},
        {"phase": "disconnected"},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # connected
    model.disconnect()
    model.poll()  # disconnected
    notifications = model.drain_notifications()
    assert len(notifications) == 0


def test_model_close_stops_poll():
    clock = FakeClock()
    client = FakeClient()
    client.statuses = [{"phase": "disconnected"}]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()
    model.close()
    model.poll()  # Should be a no-op
    assert model._closed


def test_preferences_immutable_and_display_safe():
    prefs = Preferences(
        use_tor=True,
        keep_tor=False,
        server="vpn-1",
        hostname="myhost",
        tor_bridges=("bridge1", "bridge2"),
        auto_connect=True,
    )
    d = prefs.as_dict()
    assert d["use_tor"] is True
    assert d["server"] == "vpn-1"
    # Original is untouched
    prefs2 = Preferences.from_dict(d)
    assert prefs2.tor_bridges == ("bridge1", "bridge2")


def test_server_row_from_info():
    info: ServerInfo = {
        "hostname": "vpn-1",
        "dns_name": "vpn-1.tail.ts.net",
        "ipv4": "100.64.0.1",
        "online": True,
        "healthy": True,
        "latency_ms": 42.0,
        "detail": "",
        "selected": True,
    }
    row = ServerRow.from_info(info)
    assert row.hostname == "vpn-1"
    assert row.online is True
    assert row.healthy is True
    assert row.latency_ms == 42.0
    assert row.selected is True


def test_view_state_can_connect_disconnect_properties():
    state = ViewState(actions=ActionState(connect=True, disconnect=False))
    assert state.can_connect is True
    assert state.can_disconnect is False

    state = ViewState(actions=ActionState(connect=False, disconnect=True))
    assert state.can_connect is False
    assert state.can_disconnect is True


def test_offline_server_shows_healthy_none():
    info: ServerInfo = {
        "hostname": "vpn-offline",
        "dns_name": "vpn-offline.tail.ts.net",
        "ipv4": "100.64.0.99",
        "online": False,
        "healthy": None,
        "latency_ms": None,
        "detail": "",
        "selected": False,
    }
    row = ServerRow.from_info(info)
    assert row.online is False
    assert row.healthy is None
    assert row.latency_ms is None


def test_failed_with_retry_offers_disconnect_not_connect():
    clock = FakeClock(1000.0)
    client = FakeClient()
    client.statuses = [
        {"phase": "failed", "detail": "tor: not found", "retry_at": 1015.0},
        {"phase": "failed", "detail": "not logged in", "retry_at": None},
    ]
    model = AppModel(client, clock=clock, action_runner=lambda f: f())
    model.poll()  # retry pending: the daemon still owns the attempt
    assert model.state.actions.connect is False
    assert model.state.actions.disconnect is True
    assert model.state.retry == "Retrying in 15s"
    model.poll()  # given up: a fresh connect is allowed, there is nothing to disconnect
    assert model.state.actions.connect is True
    assert model.state.actions.disconnect is False
