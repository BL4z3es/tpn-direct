"""Build an Agent from Settings, Prefs and per-connection overrides.

`tpn connect` (foreground) and the daemon both call build_agent(), so the two front ends cannot
drift apart. None of the constructors here do I/O: nothing starts until Agent.connect().
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tpn import host
from tpn.bootstrap import Agent, ConnectOptions, Phase
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


class ConfigError(ValueError):
    """The settings ask for something this platform cannot do."""


def tailscale_client(settings: Settings, runner: Runner | None = None) -> TailscaleClient:
    return TailscaleClient(
        runner or SubprocessRunner(), binary=settings.tailscale_bin, socket=settings.socket_path
    )


def daemon_controller(
    settings: Settings, ts: TailscaleClient, runner: Runner | None = None
) -> DaemonController:
    runner = runner or SubprocessRunner()
    if settings.daemon_mode == "windows":
        return WindowsServiceTailscaled(ts, runner, service=settings.windows_service)
    if host.IS_WINDOWS:
        raise ConfigError(f"TPN_DAEMON_MODE={settings.daemon_mode} is not available on Windows")
    if settings.daemon_mode == "systemd":
        return SystemdTailscaled(ts, runner, dropin=settings.systemd_dropin)
    socket = settings.socket_path
    assert socket is not None
    return ManagedTailscaled(
        ts,
        state_dir=socket.parent,
        socket=socket,
        binary=settings.tailscaled_bin,
        tun_name=settings.tun_name,
    )


def build_agent(
    settings: Settings,
    prefs: Prefs,
    *,
    auth_key_file: Path | None = None,
    server: str | None = None,
    use_tor: bool | None = None,
    keep_tor: bool | None = None,
    store: StateStore | None = None,
    on_phase: Callable[[Phase], None] | None = None,
    runner: Runner | None = None,
) -> Agent:
    """`server`, `use_tor`, `keep_tor`: this connection's overrides; None takes the pref.

    Prefs win over Settings where both exist (tor_bridges: non-empty prefs, else
    TPN_TOR_BRIDGES). The caller owns `auth_key_file`'s lifecycle.
    """
    runner = runner or SubprocessRunner()
    ts = tailscale_client(settings, runner)
    tor_bin = Path(settings.tor_bin)
    return Agent(
        tor=TorManager(
            TorConfig(
                data_dir=settings.state_dir / "tor" / "data",
                http_tunnel_port=settings.tor_http_port,
                bridges=prefs.tor_bridges or settings.tor_bridges,
                obfs4proxy=settings.obfs4proxy_bin,
                geoip_dir=tor_bin.parent / "data" if tor_bin.is_absolute() else None,
            ),
            tor_binary=settings.tor_bin,
        ),
        daemon=daemon_controller(settings, ts, runner),
        ts=ts,
        probe=DirectProbe(settings.probe_targets, timeout=settings.probe_timeout),
        selector=ServerSelector(
            tag=settings.server_tag,
            api_port=settings.api_port,
            timeout=settings.health_timeout,
            preferred=server if server is not None else prefs.server,
        ),
        options=ConnectOptions(
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
        ),
        store=store,
        on_phase=on_phase,
    )
