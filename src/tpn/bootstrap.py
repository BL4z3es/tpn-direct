"""The client agent: bootstrap over tor, go direct when possible, keep the tunnel healthy.

    INIT -> TOR_STARTING -> TOR_READY -> TAILNET_UP -> PROBING -> GOING_DIRECT
         -> CONNECTED (transport=direct | tor)

Tor is dropped only when the direct path is proven to work; otherwise it stays as the carrier
for tailscaled's control/DERP traffic, while WireGuard data still flows over direct UDP.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from tpn.daemon import DaemonController, DaemonError
from tpn.discovery import NoServerAvailable, ServerSelector
from tpn.probe import DirectProbe
from tpn.proc import wait_for
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient, TailscaleError, control_healthy

log = logging.getLogger(__name__)


class Phase(StrEnum):
    INIT = "init"
    TOR_STARTING = "tor_starting"
    TOR_READY = "tor_ready"
    TAILNET_UP = "tailnet_up"
    PROBING = "probing"
    GOING_DIRECT = "going_direct"
    CONNECTED = "connected"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


class Transport(StrEnum):
    TOR = "tor"
    DIRECT = "direct"


class BootstrapError(RuntimeError):
    pass


class TorLike(Protocol):
    @property
    def proxy_url(self) -> str: ...
    def start(self, timeout: float = ...) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...


@dataclass
class ConnectOptions:
    use_tor: bool = True
    keep_tor: bool = False
    auth_key_file: Path | None = None
    hostname: str | None = None
    client_tags: Sequence[str] = ("tag:vpn-client",)
    allow_lan: bool = True
    tor_bootstrap_timeout: float = 180.0
    control_timeout: float = 90.0
    direct_verify_timeout: float = 60.0
    watchdog_failures: int = 3


class Agent:
    def __init__(
        self,
        *,
        tor: TorLike,
        daemon: DaemonController,
        ts: TailscaleClient,
        probe: DirectProbe,
        selector: ServerSelector,
        options: ConnectOptions,
        store: StateStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tor = tor
        self.daemon = daemon
        self.ts = ts
        self.probe = probe
        self.selector = selector
        self.options = options
        self.store = store
        self._clock = clock
        self._sleep = sleep
        self.phase = Phase.INIT
        self.transport: Transport | None = None
        self.server: str | None = None
        self.server_ip: str | None = None
        self.detail = ""
        self._unhealthy_ticks = 0

    # -- connect -----------------------------------------------------------------------------

    def connect(self) -> None:
        try:
            if self.options.use_tor:
                self._set(Phase.TOR_STARTING)
                self.tor.start(timeout=self.options.tor_bootstrap_timeout)
                self._set(Phase.TOR_READY)
                self._use(Transport.TOR)
            else:
                self._use(Transport.DIRECT)
            self._bring_up()
            if self.transport is Transport.TOR and not self.options.keep_tor:
                self._try_go_direct()
            self._enable_exit_node()
        except Exception as e:
            self._set(Phase.FAILED, detail=str(e))
            raise

    def _use(self, transport: Transport) -> None:
        self.daemon.apply(self.tor.proxy_url if transport is Transport.TOR else None)
        self.transport = transport

    def _bring_up(self) -> None:
        status = self.ts.status()
        needs_login = status.get("BackendState") == "NeedsLogin"
        if needs_login and self.options.auth_key_file is None:
            raise BootstrapError("device is not logged in: provide an auth key (TPN_AUTH_KEY)")
        self.ts.up(
            auth_key_file=self.options.auth_key_file if needs_login else None,
            hostname=self.options.hostname,
            tags=self.options.client_tags,
            timeout=self.options.control_timeout,
        )
        if not self._wait_control(self.options.control_timeout):
            raise BootstrapError("tailscaled never reached the coordination server")
        self._set(Phase.TAILNET_UP)

    def _try_go_direct(self) -> None:
        self._set(Phase.PROBING)
        results = self.probe.run()
        blocked = [r for r in results if not r.ok]
        if blocked:
            reason = "; ".join(f"{r.target}: {r.detail}" for r in blocked)
            log.warning("direct path blocked, keeping tor for control traffic: %s", reason)
            self.detail = f"direct path blocked ({reason})"
            return

        self._set(Phase.GOING_DIRECT)
        try:
            self._use(Transport.DIRECT)
            went_direct = self._wait_control(self.options.direct_verify_timeout)
        except DaemonError as e:
            log.warning("tailscaled failed to restart without the proxy: %s", e)
            went_direct = False
        if went_direct:
            self.tor.stop()
            log.info("direct path works, tor stopped")
            return

        log.warning("lost the coordination server after going direct, rolling back to tor")
        self._use(Transport.TOR)
        if not self._wait_control(self.options.control_timeout):
            raise BootstrapError("lost the coordination server even after rolling back to tor")
        self.detail = "direct path failed verification; using tor"

    def _enable_exit_node(self) -> None:
        candidate = self.selector.select(self.ts.status())
        self.ts.set_exit_node(candidate.peer.ipv4, allow_lan=self.options.allow_lan)
        self.server = candidate.peer.hostname
        self.server_ip = candidate.peer.ipv4
        self._set(Phase.CONNECTED)

    def _wait_control(self, timeout: float) -> bool:
        def healthy() -> bool:
            try:
                return control_healthy(self.ts.status())
            except TailscaleError:
                return False

        return wait_for(healthy, timeout, clock=self._clock, sleep=self._sleep)

    # -- watchdog ----------------------------------------------------------------------------

    def tick(self) -> None:
        """One watchdog pass. Called periodically while connected."""
        if self.transport is Transport.TOR and not self.tor.is_running():
            log.warning("tor died, restarting it")
            self.tor.start(timeout=self.options.tor_bootstrap_timeout)

        try:
            status = self.ts.status()
        except TailscaleError as e:
            log.warning("tailscaled not answering, restarting it: %s", e)
            status = {}
            assert self.transport is not None
            self._use(self.transport)
        healthy = control_healthy(status)
        self._unhealthy_ticks = 0 if healthy else self._unhealthy_ticks + 1

        if (
            self._unhealthy_ticks >= self.options.watchdog_failures
            and self.transport is Transport.DIRECT
            and self.options.use_tor
        ):
            log.warning("coordination server unreachable directly, falling back to tor")
            self.tor.start(timeout=self.options.tor_bootstrap_timeout)
            self._use(Transport.TOR)
            self._unhealthy_ticks = 0
            self.detail = "fell back to tor after losing the direct path"
            self._reselect()
            return

        exit_status = status.get("ExitNodeStatus")
        if healthy and not (exit_status and exit_status.get("Online")):
            log.warning("exit node %s is offline, selecting another", self.server)
            self._reselect()
        else:
            self._save()

    def _reselect(self) -> None:
        try:
            self._enable_exit_node()
        except (NoServerAvailable, TailscaleError) as e:
            self.detail = f"no usable VPN server: {e}"
            log.error("%s", self.detail)
            self._save()

    # -- teardown ----------------------------------------------------------------------------

    def teardown(self) -> None:
        for step in (lambda: self.ts.set_exit_node(None), self.daemon.shutdown, self.tor.stop):
            try:
                step()
            except Exception as e:
                log.warning("teardown step failed: %s", e)
        self.transport = None
        self.server = self.server_ip = None
        self._set(Phase.DISCONNECTED)

    def run_forever(self, interval: float) -> None:
        """Connect, then watch until interrupted (SIGINT/SIGTERM raise KeyboardInterrupt)."""
        try:
            self.connect()
            log.info("connected via %s (transport: %s)", self.server, self.transport)
            while True:
                self._sleep(interval)
                try:
                    self.tick()
                except Exception:
                    log.exception("watchdog tick failed")
        except KeyboardInterrupt:
            log.info("disconnecting")
        finally:
            self.teardown()

    # -- state -------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "transport": str(self.transport) if self.transport else None,
            "server": self.server,
            "server_ip": self.server_ip,
            "detail": self.detail,
            "pid": os.getpid(),
            "updated_at": time.time(),
        }

    def _set(self, phase: Phase, *, detail: str | None = None) -> None:
        self.phase = phase
        if detail is not None:
            self.detail = detail
        log.debug("phase -> %s", phase)
        self._save()

    def _save(self) -> None:
        if self.store is not None:
            self.store.write(self.snapshot())
