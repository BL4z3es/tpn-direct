"""`tpn` command line: connect / status / disconnect on clients, serve on VPN servers."""

from __future__ import annotations

import json
import logging
import os
import signal
from pathlib import Path
from typing import Annotated

import typer

from tpn.bootstrap import Agent, ConnectOptions
from tpn.config import Settings
from tpn.daemon import DaemonController, ManagedTailscaled, SystemdTailscaled
from tpn.discovery import ServerSelector
from tpn.probe import DirectProbe
from tpn.proc import SubprocessRunner, wait_for
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient, TailscaleError
from tpn.tor import TorConfig, TorManager

app = typer.Typer(no_args_is_help=True, help=__doc__)
log = logging.getLogger("tpn")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _tailscale(settings: Settings) -> TailscaleClient:
    return TailscaleClient(
        SubprocessRunner(), binary=settings.tailscale_bin, socket=settings.socket_path
    )


def _daemon(settings: Settings, ts: TailscaleClient) -> DaemonController:
    if settings.daemon_mode == "systemd":
        return SystemdTailscaled(ts, SubprocessRunner(), dropin=settings.systemd_dropin)
    socket = settings.socket_path
    assert socket is not None
    return ManagedTailscaled(
        ts,
        state_dir=socket.parent,
        socket=socket,
        binary=settings.tailscaled_bin,
        tun_name=settings.tun_name,
    )


def _write_auth_key(settings: Settings) -> Path | None:
    if settings.auth_key is None:
        return None
    path = settings.state_dir / "authkey"
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(settings.auth_key.get_secret_value())
    return path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@app.command()
def connect(
    tor: Annotated[bool, typer.Option(help="Bootstrap through tor.")] = True,
    keep_tor: Annotated[
        bool, typer.Option(help="Never drop tor, even if the direct path works.")
    ] = False,
    server: Annotated[str | None, typer.Option(help="Use this VPN server's hostname.")] = None,
    hostname: Annotated[str | None, typer.Option(help="Tailnet hostname for this device.")] = None,
    auth_key_file: Annotated[
        Path | None, typer.Option(help="File holding a Tailscale auth key (or set TPN_AUTH_KEY).")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Connect to a VPN server and stay in the foreground watching the tunnel."""
    _setup_logging(verbose)
    settings = Settings()
    store = StateStore(settings.state_file)
    previous = store.read()
    if (
        previous
        and previous.get("phase") not in ("disconnected", "failed")
        and _pid_alive(int(previous.get("pid", 0)))
    ):
        log.error("tpn is already running (pid %s); use `tpn disconnect`", previous["pid"])
        raise typer.Exit(1)

    ts = _tailscale(settings)
    generated_key = None if auth_key_file else _write_auth_key(settings)
    agent = Agent(
        tor=TorManager(
            TorConfig(
                data_dir=settings.state_dir / "tor" / "data",
                http_tunnel_port=settings.tor_http_port,
                bridges=settings.tor_bridges,
                obfs4proxy=settings.obfs4proxy_bin,
            ),
            tor_binary=settings.tor_bin,
        ),
        daemon=_daemon(settings, ts),
        ts=ts,
        probe=DirectProbe(settings.probe_targets, timeout=settings.probe_timeout),
        selector=ServerSelector(
            tag=settings.server_tag,
            api_port=settings.api_port,
            timeout=settings.health_timeout,
            preferred=server,
        ),
        options=ConnectOptions(
            use_tor=tor,
            keep_tor=keep_tor,
            auth_key_file=auth_key_file or generated_key,
            hostname=hostname,
            client_tags=settings.client_tags,
            tor_bootstrap_timeout=settings.tor_bootstrap_timeout,
            control_timeout=settings.control_timeout,
            direct_verify_timeout=settings.direct_verify_timeout,
            watchdog_failures=settings.watchdog_failures,
        ),
        store=store,
    )
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        agent.run_forever(settings.watchdog_interval)
    except Exception as e:
        log.error("connect failed: %s", e)
        raise typer.Exit(1) from None
    finally:
        if generated_key is not None:
            generated_key.unlink(missing_ok=True)


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """Show the agent's current state."""
    state = StateStore(Settings().state_file).read()
    if state is None:
        typer.echo("not running")
        raise typer.Exit(1)
    alive = _pid_alive(int(state.get("pid", 0)))
    if not alive and state.get("phase") not in ("disconnected", "failed"):
        state["phase"] = "stale"
    if as_json:
        typer.echo(json.dumps(state, indent=2))
        return
    for key in ("phase", "transport", "server", "server_ip", "detail"):
        if state.get(key):
            typer.echo(f"{key:<10} {state[key]}")


@app.command()
def disconnect(timeout: float = 30.0) -> None:
    """Stop a running `tpn connect`."""
    state = StateStore(Settings().state_file).read()
    pid = int((state or {}).get("pid", 0))
    if not pid or not _pid_alive(pid):
        typer.echo("not running")
        return
    os.kill(pid, signal.SIGTERM)
    if wait_for(lambda: not _pid_alive(pid), timeout, interval=0.5):
        typer.echo("disconnected")
    else:
        typer.echo(f"pid {pid} did not exit within {timeout:.0f}s", err=True)
        raise typer.Exit(1)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address (default: this node's tailnet IPv4).")
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port (default: TPN_API_PORT).")] = None,
    wait: Annotated[float, typer.Option(help="Seconds to wait for tailscale.")] = 120.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the VPN-server control API. tailscale must already be up as an exit node."""
    import uvicorn

    from tpn.server.app import create_app

    _setup_logging(verbose)
    settings = Settings()
    ts = _tailscale(settings)

    status: dict = {}

    def running() -> bool:
        nonlocal status
        try:
            status = ts.status()
        except TailscaleError:
            return False
        return status.get("BackendState") == "Running"

    if not wait_for(running, wait):
        log.error("tailscale is not running; bring it up with --advertise-exit-node first")
        raise typer.Exit(1)
    if host is None:
        ipv4 = [ip for ip in TailscaleClient.self_ips(status) if ":" not in ip]
        if not ipv4:
            log.error("this node has no tailnet IPv4 address")
            raise typer.Exit(1)
        host = ipv4[0]

    forward = Path("/proc/sys/net/ipv4/ip_forward")
    if forward.exists() and forward.read_text().strip() != "1":
        log.warning("net.ipv4.ip_forward is off: this node cannot route exit-node traffic")
    if settings.server_tag not in ((status.get("Self") or {}).get("Tags") or []):
        log.warning("this node is not tagged %s; clients will not find it", settings.server_tag)

    uvicorn.run(create_app(ts), host=host, port=port or settings.api_port)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
