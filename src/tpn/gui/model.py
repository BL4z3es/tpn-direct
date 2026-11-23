"""UI-free state and actions for the desktop app.

The daemon is the only owner of VPN state. This model merely polls it, serialises control calls
and exposes immutable snapshots suitable for both tkinter and pystray. It deliberately knows
nothing about either toolkit: callers can test every transition without a display, and a slow
HTTP request never blocks a menu or a Tk callback.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from tpn.control import CONNECTING_PHASES, ControlError, DaemonNotRunning, Unauthorized
from tpn.control import PrefsDict, ServerInfo, Status
from tpn.gui.icons import IconKind

log = logging.getLogger(__name__)

DAEMON_HINT = "Start the 'tpn-direct VPN' service, or reinstall tpn"
TOKEN_HINT = "Check that the service token is readable, then restart the tpn service"
_LOG_LINES = 500
_AUTH_KEY_RE = re.compile(r"tskey-[A-Za-z0-9_-]+", re.IGNORECASE)
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

_PHASE_HEADLINES = {
    "init": "Preparing connection…",
    "tor_starting": "Starting Tor…",
    "tor_ready": "Tor is ready",
    "tailnet_up": "Joining the tailnet…",
    "probing": "Testing the direct connection…",
    "going_direct": "Switching to a direct connection…",
}


def _redact(value: object, sensitive: Sequence[str] = ()) -> str:
    """Auth keys are write-only even when a daemon or log accidentally echoes one."""
    text = _AUTH_KEY_RE.sub("[redacted auth key]", str(value))
    for secret in sensitive:
        if secret:
            text = text.replace(secret, "[redacted auth key]")
    return text


def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


@dataclass(frozen=True)
class Preferences:
    """An immutable, display-safe copy of the daemon's preferences."""

    use_tor: bool = True
    keep_tor: bool = False
    server: str | None = None
    hostname: str | None = None
    tor_bridges: tuple[str, ...] = ()
    auto_connect: bool = False

    @classmethod
    def from_dict(cls, prefs: PrefsDict) -> Preferences:
        return cls(
            use_tor=prefs["use_tor"],
            keep_tor=prefs["keep_tor"],
            server=prefs["server"],
            hostname=prefs["hostname"],
            tor_bridges=tuple(_redact(bridge) for bridge in prefs["tor_bridges"]),
            auto_connect=prefs["auto_connect"],
        )

    def as_dict(self) -> PrefsDict:
        return {
            "use_tor": self.use_tor,
            "keep_tor": self.keep_tor,
            "server": self.server,
            "hostname": self.hostname,
            "tor_bridges": list(self.tor_bridges),
            "auto_connect": self.auto_connect,
        }


@dataclass(frozen=True)
class ServerRow:
    hostname: str
    online: bool
    healthy: bool | None
    latency_ms: float | None
    selected: bool

    @classmethod
    def from_info(cls, info: ServerInfo) -> ServerRow:
        return cls(
            hostname=_redact(info["hostname"]),
            online=info["online"],
            healthy=info["healthy"],
            latency_ms=info["latency_ms"],
            selected=info["selected"],
        )


@dataclass(frozen=True)
class ActionState:
    connect: bool = False
    disconnect: bool = False
    server: bool = False
    prefs: bool = False
    auth_key: bool = False
    refresh: bool = False


@dataclass(frozen=True)
class Notification:
    title: str
    message: str


@dataclass(frozen=True)
class ViewState:
    """Everything either UI may render; no mutable daemon dictionaries or secrets leak into it."""

    icon_kind: IconKind = "disconnected"
    tooltip: str = "tpn: Checking tpn service…"
    headline: str = "Checking tpn service…"
    phase: str | None = None
    daemon_running: bool = False
    hint: str = ""
    error: str = ""
    notice: str = ""
    transport: str = "—"
    server: str = "Automatic"
    server_ip: str = "—"
    connected_for: str = "—"
    retry: str = ""
    detail: str = ""
    has_auth_key: bool = False
    prefs: Preferences = Preferences()
    prefs_loaded: bool = False
    servers: tuple[ServerRow, ...] = ()
    logs: tuple[str, ...] = ()
    actions: ActionState = ActionState()
    busy: bool = False
    revision: int = 0

    @property
    def can_connect(self) -> bool:
        return self.actions.connect

    @property
    def can_disconnect(self) -> bool:
        return self.actions.disconnect

    @property
    def settings_error(self) -> str:
        return self.error


class AppModel:
    """Serialise control calls and turn daemon responses into a common view state.

    `action_runner` and `clock` are injectable so tests can execute actions synchronously and
    advance timers without sleeping. The default runner uses daemon threads: quitting the app
    never waits for a disconnect (or any other control request) and never disconnects the VPN.
    """

    def __init__(
        self,
        client: Any,
        *,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
        idle_interval: float = 2.0,
        connecting_interval: float = 0.5,
        action_runner: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        if idle_interval <= 0 or connecting_interval <= 0:
            raise ValueError("poll intervals must be positive")
        self.client = client
        self.on_change = on_change
        self.clock = clock
        self.idle_interval = idle_interval
        self.connecting_interval = connecting_interval
        self._action_runner = action_runner or self._thread_runner
        self._lock = threading.RLock()
        self._client_lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._closed = False
        self._busy = False
        self._status: Status | None = None
        self._availability: Literal["unknown", "up", "down", "unauthorized", "error"] = "unknown"
        self._poll_error = ""
        self._action_error = ""
        self._notice = ""
        self._prefs: Preferences | None = None
        self._servers: tuple[ServerRow, ...] = ()
        self._servers_loaded = False
        self._logs: tuple[str, ...] = ()
        self._notifications: list[Notification] = []
        self._expect_disconnect = False
        self._revision = 0
        self._state = ViewState()
        self._publish()

    @property
    def state(self) -> ViewState:
        """The latest immutable snapshot, safe to read from a pystray menu callback."""
        with self._lock:
            return self._state

    @property
    def view_state(self) -> ViewState:
        return self.state

    def get_view_state(self) -> ViewState:
        return self.state

    @property
    def poll_interval(self) -> float:
        with self._lock:
            connecting = self._status is not None and self._status["phase"] in CONNECTING_PHASES
        return self.connecting_interval if connecting else self.idle_interval

    @staticmethod
    def _thread_runner(work: Callable[[], None]) -> None:
        threading.Thread(target=work, name="tpn-control-action", daemon=True).start()

    def _publish(self) -> None:
        self._state = self._make_state()
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                # A closing Tk window must not kill the poll thread or expose daemon messages.
                log.debug("View change callback failed", exc_info=True)

    def _make_state(self) -> ViewState:
        status = self._status
        phase = status["phase"] if status is not None else None
        headline = "Checking tpn service…"
        icon: IconKind = "disconnected"
        hint = ""
        error = self._action_error or self._poll_error
        if self._availability == "down":
            headline, icon, hint = "tpn service is not running", "failed", DAEMON_HINT
        elif self._availability == "unauthorized":
            headline, icon, hint = "Cannot read the service token", "failed", TOKEN_HINT
        elif self._availability == "error":
            headline, icon = "Cannot talk to the tpn service", "failed"
        elif status is not None:
            if phase == "connected":
                headline = "Connected via Tor" if status["transport"] == "tor" else "Connected directly"
                icon = "tor" if status["transport"] == "tor" else "direct"
            elif phase in CONNECTING_PHASES:
                headline, icon = _PHASE_HEADLINES.get(phase, "Connecting…"), "connecting"
            elif phase == "failed":
                headline, icon = "Connection failed", "failed"
            else:
                headline, icon = "Disconnected", "disconnected"

        connected_for = "—"
        if phase == "connected" and status is not None and status["connected_since"] is not None:
            connected_for = _elapsed(self.clock() - status["connected_since"])
        retry = ""
        if status is not None and status["retry_at"] is not None:
            remaining = max(0, int(status["retry_at"] - self.clock() + 0.999))
            retry = f"Retrying in {remaining}s" if remaining else "Retrying now…"
        server = _redact(status["server"] or "Automatic") if status is not None else "Automatic"
        server_ip = _redact(status["server_ip"] or "—") if status is not None else "—"
        tooltip = f"tpn: {headline}"
        if status is not None and status["server"]:
            tooltip += f" — {server}"
        if retry:
            tooltip += f" ({retry})"
        if len(tooltip) > 127:
            tooltip = tooltip[:126] + "…"

        available = self._availability == "up" and status is not None
        actions = ActionState(
            connect=bool(available and not self._busy and phase in {"disconnected", "failed"}),
            disconnect=bool(
                available and not self._busy and (phase in CONNECTING_PHASES | {"connected", "failed"})
            ),
            server=bool(available and not self._busy and phase not in CONNECTING_PHASES),
            prefs=bool(available and not self._busy),
            auth_key=bool(available and not self._busy),
            refresh=bool(available and not self._busy),
        )
        transport = "—"
        if status is not None and status["transport"]:
            transport = "Tor" if status["transport"] == "tor" else "Direct"
        self._revision += 1
        return ViewState(
            icon_kind=icon,
            tooltip=tooltip,
            headline=headline,
            phase=phase,
            daemon_running=self._availability == "up",
            hint=hint,
            error=_redact(error),
            notice=self._notice,
            transport=transport,
            server=server,
            server_ip=server_ip,
            connected_for=connected_for,
            retry=retry,
            detail=_redact(status["detail"]) if status is not None else "",
            has_auth_key=bool(status["has_auth_key"]) if status is not None else False,
            prefs=self._prefs or Preferences(),
            prefs_loaded=self._prefs is not None,
            servers=self._servers,
            logs=self._logs,
            actions=actions,
            busy=self._busy,
            revision=self._revision,
        )

    def _apply_status(self, status: Status, *, notify: bool = True) -> None:
        with self._lock:
            previous = self._status["phase"] if self._status is not None else None
            was_available = self._availability == "up"
            self._status = status
            self._availability = "up"
            self._poll_error = ""
            phase = status["phase"]
            if notify:
                if phase == "connected" and previous != "connected":
                    self._notifications.append(
                        Notification("tpn connected", f"Connected to {status['server'] or 'a VPN server'}.")
                    )
                elif phase == "disconnected" and previous in CONNECTING_PHASES | {"connected"}:
                    if self._expect_disconnect:
                        self._expect_disconnect = False
                    else:
                        self._notifications.append(
                            Notification(
                                "tpn disconnected",
                                _redact(status["detail"] or "The VPN disconnected unexpectedly."),
                            )
                        )
                elif phase == "failed" and (previous != "failed" or not was_available):
                    self._notifications.append(
                        Notification(
                            "tpn connection failed",
                            _redact(status["detail"] or "See the tpn window for details."),
                        )
                    )
            if phase != "disconnected":
                self._expect_disconnect = False
            self._publish()

    def _unavailable(self, exc: ControlError) -> None:
        with self._lock:
            previous = self._status["phase"] if self._status is not None else None
            self._status = None
            self._servers_loaded = False
            self._poll_error = ""
            if isinstance(exc, DaemonNotRunning):
                self._availability = "down"
            elif isinstance(exc, Unauthorized):
                self._availability = "unauthorized"
            else:
                self._availability = "error"
                self._poll_error = _redact(exc.message)
            if previous in CONNECTING_PHASES | {"connected"}:
                self._notifications.append(
                    Notification("tpn disconnected", "The tpn service is no longer available.")
                )
            self._publish()

    def _set_action_error(self, message: str) -> None:
        with self._lock:
            self._action_error = _redact(message)
            self._notice = ""
            self._publish()

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
        threading.Thread(target=self._poll_loop, name="tpn-status-poll", daemon=True).start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.poll_interval)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()

    def tick(self) -> None:
        """Republish time-dependent fields (connected-for, retry countdown) for the UI clock."""
        with self._lock:
            self._publish()

    def drain_notifications(self) -> tuple[Notification, ...]:
        with self._lock:
            notifications = tuple(self._notifications)
            self._notifications.clear()
        return notifications

    def _submit(self, name: str, work: Callable[[], Any], *, success: str = "") -> bool:
        """Run one atomic control action, so a poll cannot interleave disconnect and connect."""
        with self._lock:
            if self._closed or self._busy:
                return False
            self._busy = True
            self._action_error = ""
            self._notice = ""
            self._publish()

        def run() -> None:
            try:
                with self._client_lock:
                    result = work()
                if isinstance(result, dict) and "phase" in result:
                    self._apply_status(cast(Status, result))
                elif name == "servers":
                    self._servers = tuple(ServerRow.from_info(info) for info in result)
                    self._servers_loaded = True
                elif name == "logs":
                    self._logs = tuple(_redact(line) for line in result[-_LOG_LINES:])
                elif name in {"prefs", "server"}:
                    with self._lock:
                        self._prefs = Preferences.from_dict(cast(PrefsDict, result))
                elif name in {"save-key", "clear-key"}:
                    with self._client_lock:
                        status = self.client.status()
                    self._apply_status(status, notify=False)
                with self._lock:
                    self._notice = success
            except ControlError as exc:
                if isinstance(exc, DaemonNotRunning | Unauthorized):
                    self._unavailable(exc)
                else:
                    self._set_action_error(_redact(exc.message, getattr(exc, "secrets", ())))
            except Exception as exc:
                # Do not show a traceback or a raw exception: it may contain a request body.
                log.warning("Control action %s failed with %s", name, type(exc).__name__)
                self._set_action_error("The daemon request failed. Check the daemon logs for details.")
            finally:
                with self._lock:
                    self._busy = False
                    self._expect_disconnect = False
                    self._publish()

        try:
            self._action_runner(run)
        except Exception:
            with self._lock:
                self._busy = False
                self._publish()
            raise
        return True

    def connect(self) -> bool:
        return self._submit("connect", self.client.connect)

    def disconnect(self) -> bool:
        self._expect_disconnect = True
        return self._submit("disconnect", self.client.disconnect)

    def choose_server(self, hostname: str | None) -> bool:
        if hostname is not None:
            hostname = hostname.strip()
            if not hostname:
                self._set_action_error("Choose a server, or choose Automatic.")
                return False

        def choose() -> Any:
            prefs = self.client.update_prefs(server=hostname)
            with self._lock:
                self._prefs = Preferences.from_dict(prefs)
            if self._status is not None and self._status["phase"] == "connected":
                self._expect_disconnect = True
                self.client.disconnect()
                return self.client.connect()
            return prefs

        return self._submit("server", choose)

    def toggle_pref(self, name: Literal["use_tor", "keep_tor", "auto_connect"], value: bool) -> bool:
        if name not in {"use_tor", "keep_tor", "auto_connect"} or not isinstance(value, bool):
            self._set_action_error("That preference is not supported.")
            return False

        def toggle() -> PrefsDict:
            return self.client.update_prefs(**{name: value})

        return self._submit("prefs", toggle)

    def save_settings(
        self,
        *,
        hostname: str | None,
        tor_bridges: str | Sequence[str],
        use_tor: bool,
        keep_tor: bool,
        auto_connect: bool,
    ) -> bool:
        """Validate and save all settings fields, without including the write-only auth key."""
        hostname = hostname.strip() if isinstance(hostname, str) else None
        if hostname and (len(hostname) > 63 or not _HOSTNAME_RE.fullmatch(hostname)):
            self._set_action_error(
                "Hostname must be 1–63 letters, digits or hyphens, without leading/trailing hyphens."
            )
            return False
        if not all(isinstance(value, bool) for value in (use_tor, keep_tor, auto_connect)):
            self._set_action_error("Tor and automatic connection settings must be on or off.")
            return False
        if isinstance(tor_bridges, str):
            bridges = [line.strip() for line in tor_bridges.splitlines() if line.strip()]
        else:
            try:
                bridges = [line.strip() for line in tor_bridges if line.strip()]
            except (AttributeError, TypeError):
                self._set_action_error("Tor bridges must be text, one bridge per line.")
                return False
        if any("\x00" in bridge or "\n" in bridge or "\r" in bridge for bridge in bridges):
            self._set_action_error("Enter one valid Tor bridge per line.")
            return False

        def save() -> PrefsDict:
            return self.client.update_prefs(
                hostname=hostname or None,
                tor_bridges=bridges,
                use_tor=use_tor,
                keep_tor=keep_tor,
                auto_connect=auto_connect,
            )

        return self._submit("prefs", save, success="Settings saved.")

    def set_auth_key(self, key: str) -> bool:
        """Send a key without retaining it in model state, exceptions, notices or future closures."""
        if not isinstance(key, str) or not key.strip().startswith("tskey-") or len(key.strip()) <= 6:
            self._set_action_error("Enter an auth key starting with 'tskey-'.")
            return False
        secret = key.strip()

        def save() -> None:
            nonlocal secret
            try:
                self.client.set_auth_key(secret)
            except ControlError as exc:
                # The client normally raises only its parsed message, but redact the supplied key
                # as defence in depth if a server echoes an invalid key in its 400 response.
                exc.message = _redact(exc.message, (secret,))
                exc.args = (exc.message,)
                raise
            finally:
                secret = ""
        return self._submit("save-key", save, success="Authentication key saved.")

    def clear_auth_key(self) -> bool:
        return self._submit("clear-key", self.client.clear_auth_key, success="Authentication key cleared.")

    def refresh_servers(self) -> bool:
        return self._submit("servers", self.client.servers)

    def refresh_logs(self, lines: int = _LOG_LINES) -> bool:
        lines = min(2000, max(1, int(lines)))
        return self._submit("logs", lambda: self.client.logs(lines))

    def poll(self) -> None:
        """Poll synchronously; `start` calls this on a worker, never on the Tk main thread."""
        if self._closed:
            return
        with self._client_lock:
            try:
                status = self.client.status()
                # Preferences are loaded once, not fetched every two seconds. A transient prefs
                # error must not hide a healthy status response or stop subsequent status polls.
                prefs = None if self._prefs is not None else self.client.prefs()
            except ControlError as exc:
                self._unavailable(exc)
                return
        self._apply_status(status)
        if prefs is not None:
            with self._lock:
                self._prefs = Preferences.from_dict(prefs)
                self._publish()
        if self._status is not None and not self._servers_loaded:
            try:
                with self._client_lock:
                    servers = self.client.servers()
            except ControlError:
                # Some daemons cannot enumerate tailscaled peers while disconnected; the Servers
                # tab has an explicit Refresh action for when they become available.
                pass
            else:
                with self._lock:
                    self._servers = tuple(ServerRow.from_info(info) for info in servers)
                    self._servers_loaded = True
                    self._publish()