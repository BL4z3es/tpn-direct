"""`tpn` command line: the daemon and its clients (up/down/status), foreground connect, serve."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from tpn import host as plat
from tpn.config import Settings
from tpn.control import CONNECTING_PHASES, ControlClient, ControlError, DaemonNotRunning
from tpn.prefs import Prefs
from tpn.proc import wait_for
from tpn.service.factory import ConfigError, build_agent, tailscale_client
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient, TailscaleError

app = typer.Typer(no_args_is_help=True, help=__doc__)
log = logging.getLogger("tpn")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _control(settings: Settings) -> ControlClient:
    return ControlClient(port=settings.control_port, token_file=settings.token_file)


def _require_admin(what: str) -> None:
    if not plat.is_admin():
        who = "an elevated (Administrator) prompt" if plat.IS_WINDOWS else "root"
        log.error("%s needs %s", what, who)
        raise typer.Exit(1)


def _write_auth_key(settings: Settings) -> Path | None:
    if settings.auth_key is None:
        return None
    path = settings.state_dir / "authkey"
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(settings.auth_key.get_secret_value())
    return path


_pid_alive = plat.pid_alive


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
    _require_admin("tpn connect")
    settings = Settings()
    if _control(settings).is_running():
        log.error("the tpn daemon is running and owns the connection; use `tpn up` / `tpn down`")
        raise typer.Exit(1)
    store = StateStore(settings.state_file)
    previous = store.read()
    if (
        previous
        and previous.get("phase") not in ("disconnected", "failed")
        and _pid_alive(int(previous.get("pid", 0)))
    ):
        log.error("tpn is already running (pid %s); use `tpn disconnect`", previous["pid"])
        raise typer.Exit(1)

    generated_key = None if auth_key_file else _write_auth_key(settings)
    try:
        agent = build_agent(
            settings,
            Prefs(use_tor=tor, keep_tor=keep_tor, server=server, hostname=hostname),
            auth_key_file=auth_key_file or generated_key,
            store=store,
        )
    except ConfigError as e:
        log.error("%s", e)
        raise typer.Exit(2) from None
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if hasattr(signal, "SIGBREAK"):  # Ctrl-Break, and closing the console window, on Windows
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    stop_file = settings.stop_file
    stop_file.unlink(missing_ok=True)
    try:
        agent.run_forever(settings.watchdog_interval, should_stop=stop_file.exists)
    except Exception as e:
        log.error("connect failed: %s", e)
        raise typer.Exit(1) from None
    finally:
        if generated_key is not None:
            generated_key.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """Show the connection's state (from the daemon if it runs, else a foreground agent)."""
    settings = Settings()
    try:
        state: dict[str, Any] | None = dict(_control(settings).status())
    except DaemonNotRunning:
        state = StateStore(settings.state_file).read()
        if state is not None:
            alive = _pid_alive(int(state.get("pid", 0)))
            if not alive and state.get("phase") not in ("disconnected", "failed"):
                state["phase"] = "stale"
    except ControlError as e:
        typer.echo(f"tpn daemon: {e.message}", err=True)
        raise typer.Exit(1) from None
    if state is None:
        typer.echo("not running")
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(state, indent=2))
        return
    _print_status(state)


def _print_status(state: dict[str, Any]) -> None:
    for key in ("phase", "transport", "server", "server_ip", "detail"):
        if state.get(key):
            typer.echo(f"{key:<10} {state[key]}")
    if state.get("retry_at"):
        typer.echo(f"{'retry in':<10} {max(0, state['retry_at'] - time.time()):.0f}s")
    if state.get("logged_in") is False and not state.get("has_auth_key"):
        typer.echo("not logged in: store a key with `tpn set-key`")


@app.command()
def disconnect(timeout: float = 30.0) -> None:
    """Stop a running `tpn connect` (or, if the daemon runs, the daemon's connection)."""
    settings = Settings()
    client = _control(settings)
    if client.is_running():
        _call(client.disconnect)
        typer.echo("disconnected")
        return
    state = StateStore(settings.state_file).read()
    pid = int((state or {}).get("pid", 0))
    if not pid or not _pid_alive(pid):
        typer.echo("not running")
        return
    # The agent polls for this file every second and tears down cleanly. On Windows it is the
    # only way: os.kill() there is TerminateProcess, which would skip the teardown.
    settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
    settings.stop_file.touch()
    if not plat.IS_WINDOWS:
        os.kill(pid, signal.SIGTERM)
    if wait_for(lambda: not _pid_alive(pid), timeout, interval=0.5):
        typer.echo("disconnected")
    else:
        typer.echo(f"pid {pid} did not exit within {timeout:.0f}s", err=True)
        raise typer.Exit(1)


# -- the daemon and its clients ------------------------------------------------------------


@app.command()
def daemon(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """Run the tpn daemon in the foreground (what tpn.service and the Windows service run)."""
    _setup_logging(verbose)
    _require_admin("tpn daemon")
    from tpn import service

    stop = threading.Event()

    def on_signal(signum: int, frame: object) -> None:
        log.info("signal %s: stopping", signum)
        stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)
    try:
        service.run(Settings(), stop)
    except OSError as e:
        log.error("tpn daemon: %s", e)
        raise typer.Exit(1) from None


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a ControlClient call, turning its errors into a message and exit code 1."""
    try:
        return fn(*args, **kwargs)
    except DaemonNotRunning:
        how = "start the 'tpn' service" if plat.IS_WINDOWS else "systemctl start tpn"
        typer.echo(f"the tpn daemon is not running ({how}, or run `tpn daemon`)", err=True)
    except ControlError as e:
        typer.echo(f"tpn daemon: {e.message}", err=True)
    raise typer.Exit(1)


@app.command()
def up(
    server: Annotated[str | None, typer.Option(help="Use this VPN server's hostname.")] = None,
    tor: Annotated[
        bool | None, typer.Option("--tor/--no-tor", help="Bootstrap through tor (default: prefs).")
    ] = None,
    keep_tor: Annotated[
        bool | None, typer.Option("--keep-tor/--drop-tor", help="Never drop tor (default: prefs).")
    ] = None,
    wait: Annotated[bool, typer.Option(help="Wait until connected or failed.")] = True,
    timeout: Annotated[float, typer.Option(help="Seconds to wait.")] = 600.0,
) -> None:
    """Ask the daemon to connect. Overrides apply to this connection only."""
    client = _control(Settings())
    state = _call(client.connect, server=server, use_tor=tor, keep_tor=keep_tor)
    if not wait:
        typer.echo(state["phase"])
        return
    last = None
    deadline = time.monotonic() + timeout
    while True:
        if state["phase"] != last:
            typer.echo(state["phase"] + (f": {state['detail']}" if state.get("detail") else ""))
            last = state["phase"]
        if state["phase"] == "connected":
            typer.echo(f"connected via {state['server']} ({state['transport']})")
            return
        if state["phase"] not in CONNECTING_PHASES:
            if state.get("retry_at"):
                typer.echo("the daemon will retry; `tpn status` to follow, `tpn down` to stop")
            raise typer.Exit(1)
        if time.monotonic() > deadline:
            typer.echo("still connecting; `tpn status` to follow", err=True)
            raise typer.Exit(1)
        time.sleep(1.0)
        state = _call(client.status)


@app.command()
def down() -> None:
    """Ask the daemon to disconnect (waits for the teardown)."""
    _call(_control(Settings()).disconnect)
    typer.echo("disconnected")


@app.command()
def servers(
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """List the tailnet's VPN servers and their health."""
    found = _call(_control(Settings()).servers)
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    if not found:
        typer.echo("no VPN servers in the tailnet")
        return
    for s in found:
        health = {True: "healthy", False: "unhealthy", None: "-"}[s["healthy"]]
        latency = f"{s['latency_ms']:.0f} ms" if s["latency_ms"] is not None else ""
        mark = "*" if s["selected"] else " "
        online = "online" if s["online"] else "offline"
        typer.echo(f"{mark} {s['hostname']:<24} {s['ipv4']:<16} {online:<8} {health:<10} {latency}")


@app.command("set-key")
def set_key(
    clear: Annotated[bool, typer.Option("--clear", help="Delete the stored key.")] = False,
) -> None:
    """Store a Tailscale auth key for the next login (read from stdin or a hidden prompt)."""
    client = _control(Settings())
    if clear:
        _call(client.clear_auth_key)
        typer.echo("auth key deleted")
        return
    if sys.stdin.isatty():
        key = typer.prompt("Tailscale auth key (tag:vpn-client)", hide_input=True)
    else:
        key = sys.stdin.readline()
    if not key.strip():
        typer.echo("no key given", err=True)
        raise typer.Exit(1)
    _call(client.set_auth_key, key.strip())
    typer.echo("auth key stored; it is used at the next login and then deleted")


prefs_app = typer.Typer(
    help="Show or change the daemon's saved preferences.", invoke_without_command=True
)
app.add_typer(prefs_app, name="prefs")


@prefs_app.callback()
def prefs_show(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """Show the saved preferences."""
    if ctx.invoked_subcommand is not None:
        return
    current = _call(_control(Settings()).prefs)
    if as_json:
        typer.echo(json.dumps(current, indent=2))
        return
    for key, value in current.items():
        typer.echo(f"{key:<14} {json.dumps(value)}")


def _parse_assignment(item: str) -> tuple[str, Any]:
    key, sep, raw = item.partition("=")
    if not sep or not key:
        raise typer.BadParameter(f"expected KEY=VALUE, got {item!r}")
    try:
        return key, json.loads(raw)
    except json.JSONDecodeError:
        return key, raw


@prefs_app.command("set")
def prefs_set(
    items: Annotated[list[str], typer.Argument(help="KEY=VALUE; values are JSON when they parse.")],
) -> None:
    """Change preferences, e.g. `tpn prefs set keep_tor=true server=vpn-fra-1`."""
    patch = dict(_parse_assignment(item) for item in items)
    updated = _call(_control(Settings()).update_prefs, **patch)
    for key in patch:
        typer.echo(f"{key:<14} {json.dumps(updated[key])}")


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address (default: this node's tailnet IPv4).")
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port (default: TPN_API_PORT).")] = None,
    wait: Annotated[float, typer.Option(help="Seconds to wait for tailscale.")] = 120.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the VPN-server control API (Linux only). tailscale must be up as an exit node."""
    _setup_logging(verbose)
    if not plat.IS_LINUX:
        log.error("tpn serve runs on Linux only; this build is a client")
        raise typer.Exit(2)
    try:
        import uvicorn

        from tpn.server.app import create_app
    except ImportError:
        log.error("the server is not installed: pip install 'tpn-direct[server]'")
        raise typer.Exit(2) from None

    settings = Settings()
    ts = tailscale_client(settings)

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
