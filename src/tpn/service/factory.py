"""Shared agent construction used by `tpn connect` and the daemon.

Neither the CLI nor the daemon imports `bootstrap` or `discovery` directly; both call
`build_agent()` so the two front ends can never drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tpn import host
from tpn.bootstrap import Agent, ConnectOptions
from tpn.config import Settings
from tpn.daemon import (
    DaemonController,
    ManagedTailscaled,
    SystemdTailscaled,
    WindowsServiceTailscaled,
)
from tpn.discovery import ServerSelector
from tpn.prefs import Prefs
from tpn.probe import DirectProbe
from tpn.proc import Runner, SubprocessRunner
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient
from tpn.tor import TorConfig, TorManager

log = logging.getLogger(__name__)


class AgentParts(Protocol):
    """The injectable surface: every dependency `Agent` needs, made from settings."""

    def make_ts(self, settings: Settings) -> TailscaleClient: ...
    def make_tor(self, settings: Settings, config: TorConfig) -> TorManager: ...
    def make_daemon(self, settings: Settings, ts: TailscaleClient) -> DaemonController: ...
    def make_probe(self, settings: Settings) -> DirectProbe: ...
    def make_selector(self, settings: Settings, prefs: Prefs) -> ServerSelector: ...


class DefaultParts:
    """Real implementations, built exactly as `tpn connect` built them before the daemon."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    def make_ts(self, settings: Settings) -> TailscaleClient:
        return TailscaleClient(self._runner, binary=settings.tailscale_bin, socket=settings.socket_path)

    def make_tor(self, settings: Settings, config: TorConfig) -> TorManager:
        return TorManager(config, tor_binary=settings.tor_bin)

    def make_daemon(self, settings: Settings, ts: TailscaleClient) -> DaemonController:
        if settings.daemon_mode == "windows":
            return WindowsServiceTailscaled(ts, self._runner, service=settings.windows_service)
        if host.IS_WINDOWS:
            raise ValueError(f"TPN_DAEMON_MODE={settings.daemon_mode} is not available on Windows")
        if settings.daemon_mode == "systemd":
            return SystemdTailscaled(ts, self._runner, dropin=settings.systemd_dropin)
        socket = settings.socket_path
        assert socket is not None
        return ManagedTailscaled(
            ts,
            state_dir=socket.parent,
            socket=socket,
            binary=settings.tailscaled_bin,
            tun_name=settings.tun_name,
        )

    def make_probe(self, settings: Settings) -> DirectProbe:
        return DirectProbe(settings.probe_targets, timeout=settings.probe_timeout)

    def make_selector(self, settings: Settings, prefs: Prefs) -> ServerSelector:
        return ServerSelector(
            tag=settings.server_tag,
            api_port=settings.api_port,
            timeout=settings.health_timeout,
            preferred=prefs.server,
        )


def _bridges_for(settings: Settings, prefs: Prefs) -> list[str]:
    """Prefs override Settings where they overlap: non-empty prefs win, else settings."""
    return prefs.tor_bridges if prefs.tor_bridges else settings.tor_bridges


def build_agent(
    settings: Settings,
    prefs: Prefs,
    *,
    auth_key_file: Path | None = None,
    server: str | None = None,
    use_tor: bool | None = None,
    keep_tor: bool | None = None,
    store: StateStore | None = None,
    parts: AgentParts | None = None,
    on_phase=None,
    clock=None,
    sleep=None,
) -> Agent:
    """Assemble an `Agent` from settings, prefs and per-connection overrides.

    `server`/`use_tor`/`keep_tor` are this connection's overrides (None = take the pref,
    which itself defaults sensibly). `auth_key_file` is passed straight through; the caller
    owns the file's lifecycle. `on_phase`, `clock` and `sleep` go to the agent, so the
    daemon can observe phases and tests can control time.
    """
    p = parts or DefaultParts()

    preferred = server if server is not None else prefs.server
    selector = p.make_selector(settings, prefs.model_copy(update={"server": preferred}))

    tor_config = TorConfig(
        data_dir=settings.state_dir / "tor" / "data",
        http_tunnel_port=settings.tor_http_port,
        bridges=_bridges_for(settings, prefs),
        obfs4proxy=settings.obfs4proxy_bin,
        geoip_dir=Path(settings.tor_bin).parent / "data"
        if Path(settings.tor_bin).is_absolute()
        else None,
    )

    ts = p.make_ts(settings)
    options = ConnectOptions(
        use_tor=prefs.use_tor if use_tor is None else use_tor,
        keep_tor=prefs.keep_tor if keep_tor is None else keep_tor,
        auth_key_file=auth_key_file,
        hostname=prefs.hostname,
        client_tags=settings.client_tags,
        tor_bootstrap_timeout=settings.tor_bootstrap_timeout,
        control_timeout=settings.control_timeout,
        direct_verify_timeout=settings.direct_verify_timeout,
        watchdog_failures=settings.watchdog_failures,
        unattended=host.IS_WINDOWS,
    )

    return Agent(
        tor=p.make_tor(settings, tor_config),
        daemon=p.make_daemon(settings, ts),
        ts=ts,
        probe=p.make_probe(settings),
        selector=selector,
        options=options,
        store=store,
        clock=clock,
        sleep=sleep,
        on_phase=on_phase,
    )
