"""The daemon's service core: one connection owner with retries and auth-key lifecycle.

The HTTP layer (`server.py`) calls this class; a worker thread drives `Agent.run_forever`.
Everything external is injected so tests use fakes without tor or tailscaled.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from tpn import __version__, host
from tpn.bootstrap import Phase
from tpn.config import Settings
from tpn.control import CONNECTING_PHASES, Status
from tpn.prefs import Prefs, PrefsStore
from tpn.service.acl import FileSecurity
from tpn.service.factory import build_agent
from tpn.service.servers import ServerInventory
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient, TailscaleError

log = logging.getLogger(__name__)

RETRY_BASE = 15.0
RETRY_CAP = 300.0


class AgentFactory(Protocol):
    def __call__(
        self,
        settings: Settings,
        prefs: Prefs,
        *,
        auth_key_file: Any = None,
        server: str | None = None,
        use_tor: bool | None = None,
        keep_tor: bool | None = None,
        store: StateStore | None = None,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> Any: ...


class ServiceError(RuntimeError):
    """A request cannot be performed; `status` is its HTTP response code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Service:
    """Owns at most one `Agent` and serializes connection lifecycle changes."""

    def __init__(
        self,
        settings: Settings,
        prefs_store: PrefsStore,
        tailscale: TailscaleClient,
        *,
        agent_factory: AgentFactory = build_agent,
        inventory: ServerInventory | None = None,
        security: FileSecurity | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] | None = None,
        pid_alive: Callable[[int], bool] = host.pid_alive,
    ) -> None:
        self.settings = settings
        self.prefs_store = prefs_store
        self.tailscale = tailscale
        self.security = security or FileSecurity()
        self.inventory = inventory or ServerInventory(
            tag=settings.server_tag, api_port=settings.api_port, timeout=settings.health_timeout
        )
        self._agent_factory = agent_factory
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._pid_alive = pid_alive

        self._lock = threading.RLock()
        self._prefs_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._agent: Any = None
        self._cancel = threading.Event()
        self._closing = False
        self._daemon_stop: threading.Event | None = None
        self._phase = Phase.DISCONNECTED
        self._detail = ""
        self._connected_since: float | None = None
        self._retry_at: float | None = None
        self.started_at = wall()

    def start(self, stop: threading.Event | None = None) -> None:
        """Start automatically when the stored preference asks for it."""
        self._daemon_stop = stop
        if self.read_prefs().auto_connect:
            try:
                self.connect(pid=os.getpid())
            except ServiceError as e:
                log.error("automatic connection did not start: %s", e.message)

    def connect(
        self,
        *,
        server: str | None = None,
        use_tor: bool | None = None,
        keep_tor: bool | None = None,
        pid: int = 0,
    ) -> Status:
        """Move synchronously out of idle, then let the worker perform the connection."""
        daemon_pid = pid or os.getpid()
        with self._lock:
            if self._closing:
                raise ServiceError(409, "the daemon is stopping")
            if self._worker is not None and self._worker.is_alive():
                raise ServiceError(409, "already connecting or connected")
            self._refuse_foreground_owner(daemon_pid)
            prefs = self.read_prefs()
            self._cancel = threading.Event()
            self._phase = Phase.INIT
            self._detail = ""
            self._connected_since = None
            self._retry_at = None
            worker = threading.Thread(
                target=self._session,
                args=(prefs, server, use_tor, keep_tor, self._cancel),
                name="tpn-agent",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return self.status()

    def disconnect(self) -> Status:
        """Cancel retries and wait until the agent's own teardown has completed."""
        with self._lock:
            worker = self._worker
            self._cancel.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join()
        with self._lock:
            self._phase = Phase.DISCONNECTED
            self._detail = ""
            self._connected_since = None
            self._retry_at = None
            return self.status()

    def shutdown(self) -> None:
        """Stop reconnecting, then perform the same clean teardown as `disconnect`."""
        with self._lock:
            self._closing = True
        self.disconnect()

    def _session(
        self,
        prefs: Prefs,
        server: str | None,
        use_tor: bool | None,
        keep_tor: bool | None,
        cancel: threading.Event,
    ) -> None:
        delay = RETRY_BASE
        while not cancel.is_set():
            key_file = self.settings.auth_key_file
            auth_key_file = key_file if key_file.is_file() else None
            try:
                agent = self._agent_factory(
                    self.settings,
                    prefs,
                    auth_key_file=auth_key_file,
                    server=server,
                    use_tor=use_tor,
                    keep_tor=keep_tor,
                    store=StateStore(self.settings.state_file),
                    on_phase=lambda phase, current=None: self._on_phase(current, phase),
                )
                agent._on_phase = lambda phase, current=agent: self._on_phase(current, phase)
            except Exception as e:
                if cancel.is_set():
                    return
                self._failed(e)
                if not self._backoff(delay, cancel):
                    return
                delay = min(delay * 2, RETRY_CAP)
                continue

            with self._lock:
                self._agent = agent
            try:
                should_stop = cancel.is_set
                if self._daemon_stop is not None:
                    should_stop = lambda: cancel.is_set() or self._daemon_stop.is_set()
                agent.run_forever(self.settings.watchdog_interval, should_stop=should_stop)
                return
            except KeyboardInterrupt:
                return
            except Exception as e:
                if cancel.is_set():
                    return
                self._failed(e)
                if not self._retryable(agent):
                    return
                if not self._backoff(delay, cancel):
                    return
                delay = min(delay * 2, RETRY_CAP)
            finally:
                pass

    def _backoff(self, seconds: float, cancel: threading.Event) -> bool:
        """Wait for the next attempt, returning false immediately when cancelled."""
        deadline = self._clock() + seconds
        with self._lock:
            self._retry_at = self._wall() + seconds
        while not cancel.is_set():
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            step = min(1.0, remaining)
            if self._sleep is None:
                cancel.wait(step)
            else:
                self._sleep(step)
        return False

    def _failed(self, error: Exception) -> None:
        message = self.security.redact(str(error))
        log.warning("connection attempt failed: %s", message)
        with self._lock:
            self._phase = Phase.FAILED
            self._detail = message
            self._connected_since = None
            self._retry_at = None

    def _retryable(self, agent: Any) -> bool:
        """A missing login cannot fix itself; an unreachable tailscaled may recover."""
        if agent.options.auth_key_file is not None:
            return True
        try:
            needs_login = agent.ts.status().get("BackendState") == "NeedsLogin"
        except (TailscaleError, AttributeError):
            return True
        return not needs_login

    def _on_phase(self, agent: Any, phase: Phase) -> None:
        with self._lock:
            if agent is not self._agent:
                return
            self._phase = phase
            self._detail = self.security.redact(agent.detail)
            if phase is Phase.CONNECTED:
                self._connected_since = self._wall()
                self._retry_at = None
            elif phase in (Phase.FAILED, Phase.DISCONNECTED):
                self._connected_since = None
            if phase is Phase.TAILNET_UP:
                key_file = self.settings.auth_key_file
                if agent.options.auth_key_file == key_file and key_file.is_file():
                    try:
                        key_file.unlink()
                        log.info("login succeeded; deleted the stored auth key")
                    except OSError as e:
                        log.warning("could not delete the used auth key: %s", e)

    def _refuse_foreground_owner(self, daemon_pid: int) -> None:
        state = StateStore(self.settings.state_file).read() or {}
        try:
            owner = int(state.get("pid", 0))
        except (TypeError, ValueError):
            return
        if (
            owner
            and owner != daemon_pid
            and state.get("phase") not in (Phase.DISCONNECTED.value, Phase.FAILED.value)
            and self._pid_alive(owner)
        ):
            raise ServiceError(409, f"foreground `tpn connect` is running (pid {owner})")

    def status(self) -> Status:
        with self._lock:
            agent = self._agent
            connected = self._phase is Phase.CONNECTED and agent is not None
            status: Status = {
                "daemon": {
                    "version": __version__,
                    "pid": os.getpid(),
                    "started_at": self.started_at,
                },
                "phase": self._phase.value,
                "transport": str(agent.transport) if connected and agent.transport else None,
                "server": agent.server if connected else None,
                "server_ip": agent.server_ip if connected else None,
                "detail": self.security.redact(self._detail),
                "connected_since": self._connected_since,
                "retry_at": self._retry_at,
                "has_auth_key": self.settings.auth_key_file.is_file(),
                "logged_in": self.logged_in(),
            }
            return status

    def logged_in(self) -> bool | None:
        """Ask tailscaled on demand so a login performed elsewhere is visible."""
        try:
            return self.tailscale.status().get("BackendState") != "NeedsLogin"
        except TailscaleError:
            return None

    def servers(self) -> list[dict[str, Any]]:
        with self._lock:
            selected = self._agent.server_ip if self._phase is Phase.CONNECTED else None
        return self.inventory.list(self.tailscale, selected_ip=selected)

    def read_prefs(self) -> Prefs:
        with self._prefs_lock:
            return self.prefs_store.read()

    def update_prefs(self, patch: dict[str, Any]) -> Prefs:
        with self._prefs_lock:
            return self.prefs_store.update(patch)

    def set_auth_key(self, key: str) -> None:
        if not isinstance(key, str) or not key.startswith("tskey-"):
            raise ServiceError(400, "auth_key must be a string starting with 'tskey-'")
        self.security.private_file(self.settings.auth_key_file, key)
        log.info("stored a Tailscale auth key for the next login")

    def clear_auth_key(self) -> None:
        self.settings.auth_key_file.unlink(missing_ok=True)
        log.info("deleted the stored Tailscale auth key")