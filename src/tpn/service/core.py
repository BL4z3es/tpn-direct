"""The daemon's core: owns at most one Agent, retries failed attempts, keeps the auth key.

The HTTP layer (server.py) only translates requests into calls on Service. Each connection
runs Agent.run_forever() on a worker thread; disconnect() cancels it and waits for its own
teardown, so "disconnected" really means tailscaled is no longer pointed at tor.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from tpn import __version__, host
from tpn.bootstrap import Agent, NeedsLoginError, Phase
from tpn.config import Settings
from tpn.control import ServerInfo, Status
from tpn.prefs import Prefs, PrefsStore
from tpn.proc import CommandNotFound
from tpn.service.acl import FileSecurity
from tpn.service.factory import ConfigError, build_agent
from tpn.service.servers import ServerInventory
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient, TailscaleError

log = logging.getLogger(__name__)

RETRY_BASE = 15.0
RETRY_CAP = 300.0
LOGS_DEFAULT = 200
LOGS_MAX = 2000
# `tailscale status` is a subprocess; the tray app polls every couple of seconds.
LOGIN_CACHE_SECONDS = 5.0
DISCONNECT_TIMEOUT = 180.0

AgentFactory = Callable[..., Agent]


class ServiceError(RuntimeError):
    """A request the daemon refuses; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Service:
    def __init__(
        self,
        settings: Settings,
        *,
        prefs_store: PrefsStore,
        ts: TailscaleClient,
        agent_factory: AgentFactory = build_agent,
        inventory: ServerInventory | None = None,
        security: FileSecurity | None = None,
        wall: Callable[[], float] = time.time,
        wait: Callable[[threading.Event, float], bool] = lambda ev, s: ev.wait(s),
        pid_alive: Callable[[int], bool] = host.pid_alive,
        retry_base: float = RETRY_BASE,
        retry_cap: float = RETRY_CAP,
    ) -> None:
        self.settings = settings
        self.prefs_store = prefs_store
        self.ts = ts
        self.security = security or FileSecurity()
        self.inventory = inventory or ServerInventory(
            tag=settings.server_tag, api_port=settings.api_port, timeout=settings.health_timeout
        )
        self._factory = agent_factory
        self._wall = wall
        self._wait = wait
        self._pid_alive = pid_alive
        self._retry_base = retry_base
        self._retry_cap = retry_cap
        self.started_at = wall()

        self._lock = threading.RLock()
        self._prefs_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._closing = False
        self._agent: Agent | None = None
        self._phase = Phase.DISCONNECTED
        self._detail = ""
        self._connected_since: float | None = None
        self._retry_at: float | None = None
        self._login_cache: tuple[float, bool | None] | None = None

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """Connect at once when the user asked for it (prefs.auto_connect)."""
        if not self.read_prefs().auto_connect:
            return
        try:
            self.connect()
        except ServiceError as e:
            log.error("automatic connection did not start: %s", e.message)

    def shutdown(self) -> None:
        """Refuse new connections, then disconnect cleanly."""
        with self._lock:
            self._closing = True
        self.disconnect()

    def connect(
        self,
        *,
        server: str | None = None,
        use_tor: bool | None = None,
        keep_tor: bool | None = None,
    ) -> Status:
        with self._lock:
            if self._closing:
                raise ServiceError(409, "the daemon is stopping")
            if self._worker is not None and self._worker.is_alive():
                raise ServiceError(409, "already connecting or connected; disconnect first")
            self._refuse_foreground_owner()
            prefs = self.read_prefs()
            self._cancel = cancel = threading.Event()
            self._agent = None
            self._phase = Phase.INIT
            self._detail = ""
            self._connected_since = self._retry_at = None
            self._worker = threading.Thread(
                target=self._session,
                args=(prefs, server, use_tor, keep_tor, cancel),
                name="tpn-agent",
                daemon=True,
            )
            self._worker.start()
        return self.status()

    def disconnect(self) -> Status:
        with self._lock:
            worker = self._worker
            self._cancel.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(DISCONNECT_TIMEOUT)
            if worker.is_alive():
                log.error("the agent did not finish tearing down in %.0fs", DISCONNECT_TIMEOUT)
        with self._lock:
            self._worker = None
            self._agent = None
            self._phase = Phase.DISCONNECTED
            self._detail = ""
            self._connected_since = self._retry_at = None
        return self.status()

    # -- the worker --------------------------------------------------------------------------

    def _session(
        self,
        prefs: Prefs,
        server: str | None,
        use_tor: bool | None,
        keep_tor: bool | None,
        cancel: threading.Event,
    ) -> None:
        delay = self._retry_base
        while not cancel.is_set():
            key_file = self.settings.auth_key_file
            agent: Agent | None = None
            try:
                agent = self._factory(
                    self.settings,
                    prefs,
                    auth_key_file=key_file if key_file.is_file() else None,
                    server=server,
                    use_tor=use_tor,
                    keep_tor=keep_tor,
                    store=StateStore(self.settings.state_file),
                    on_phase=lambda phase, cancel=cancel: self._on_phase(cancel, phase),
                )
                with self._lock:
                    if cancel.is_set():
                        return
                    self._agent = agent
                # The agent polls should_stop every second while it watches the tunnel.
                agent.run_forever(self.settings.watchdog_interval, should_stop=cancel.is_set)
                return
            except Exception as e:
                if cancel.is_set():
                    return
                retry = not isinstance(e, (NeedsLoginError, ConfigError))
                self._failed(cancel, e, delay if retry else None)
                if not retry or self._wait(cancel, delay):
                    return
                delay = min(delay * 2, self._retry_cap)

    def _on_phase(self, cancel: threading.Event, phase: Phase) -> None:
        with self._lock:
            if cancel is not self._cancel or self._agent is None:
                return  # a stale attempt, or one already being torn down
            agent = self._agent
            self._phase = phase
            self._detail = self.security.redact(agent.detail)
            if phase is Phase.CONNECTED:
                self._connected_since = self._connected_since or self._wall()
                self._retry_at = None
        if phase is Phase.TAILNET_UP and agent.options.auth_key_file == self.settings.auth_key_file:
            # The node key now lives in tailscaled's state; the auth key is no longer needed.
            self.settings.auth_key_file.unlink(missing_ok=True)
            log.info("logged in; deleted the stored auth key")

    def _failed(self, cancel: threading.Event, error: Exception, retry_in: float | None) -> None:
        message = self.security.redact(str(error)) or type(error).__name__
        with self._lock:
            if cancel is not self._cancel:
                return
            self._agent = None
            self._phase = Phase.FAILED
            self._connected_since = None
            self._retry_at = self._wall() + retry_in if retry_in is not None else None
            self._detail = message
        if retry_in is None:
            log.error("connection failed, not retrying: %s", message)
        else:
            log.warning("connection failed, retrying in %.0fs: %s", retry_in, message)

    def _refuse_foreground_owner(self) -> None:
        state = StateStore(self.settings.state_file).read() or {}
        try:
            owner = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            return
        if (
            owner
            and owner != os.getpid()
            and state.get("phase") not in ("disconnected", "failed")
            and self._pid_alive(owner)
        ):
            raise ServiceError(409, f"a foreground `tpn connect` is running (pid {owner})")

    # -- queries -----------------------------------------------------------------------------

    def status(self) -> Status:
        logged_in = self._logged_in()  # outside the lock: it runs a subprocess
        with self._lock:
            agent = self._agent
            live = agent is not None and self._phase is not Phase.FAILED
            return {
                "daemon": {
                    "version": __version__,
                    "pid": os.getpid(),
                    "started_at": self.started_at,
                },
                "phase": self._phase.value,  # type: ignore[typeddict-item]
                "transport": agent.transport.value if live and agent.transport else None,
                "server": agent.server if live else None,
                "server_ip": agent.server_ip if live else None,
                "detail": self._detail,
                "connected_since": self._connected_since,
                "retry_at": self._retry_at,
                "has_auth_key": self.settings.auth_key_file.is_file(),
                "logged_in": logged_in,
            }

    def _logged_in(self) -> bool | None:
        now = self._wall()
        cached = self._login_cache
        if cached is not None and now - cached[0] < LOGIN_CACHE_SECONDS:
            return cached[1]
        try:
            value: bool | None = self.ts.status().get("BackendState") != "NeedsLogin"
        except (TailscaleError, CommandNotFound):
            value = None
        self._login_cache = (now, value)
        return value

    def servers(self) -> list[ServerInfo]:
        with self._lock:
            agent = self._agent
            selected = (
                agent.server_ip if agent is not None and self._phase is Phase.CONNECTED else None
            )
        try:
            return self.inventory.list(self.ts, selected_ip=selected)
        except (TailscaleError, CommandNotFound) as e:
            raise ServiceError(503, f"tailscaled is not answering: {e}") from e

    def logs(self, lines: int = LOGS_DEFAULT) -> list[str]:
        lines = max(1, min(int(lines), LOGS_MAX))
        try:
            with self.settings.daemon_log.open(encoding="utf-8", errors="replace") as f:
                return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
        except FileNotFoundError:
            return []

    # -- prefs and auth key ------------------------------------------------------------------

    def read_prefs(self) -> Prefs:
        with self._prefs_lock:
            return self.prefs_store.read()

    def update_prefs(self, patch: dict[str, Any]) -> Prefs:
        """Raises ValueError on an unknown key or a bad value. Applies to the next connection."""
        with self._prefs_lock:
            return self.prefs_store.update(patch)

    def set_auth_key(self, key: object) -> None:
        if not isinstance(key, str) or not key.strip().startswith("tskey-"):
            raise ServiceError(400, "auth_key must be a string starting with 'tskey-'")
        self.security.private_file(self.settings.auth_key_file, key.strip())
        log.info("stored an auth key for the next login")

    def clear_auth_key(self) -> None:
        self.settings.auth_key_file.unlink(missing_ok=True)
        log.info("deleted the stored auth key")
