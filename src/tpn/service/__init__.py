"""The tpn daemon: owns tor and tailscaled, and serves the control API.

Entry point (called by the Windows service host from a non-main thread, and by `tpn daemon`):

    run(settings: Settings | None = None, stop: threading.Event | None = None) -> None

Blocks until `stop` is set (or KeyboardInterrupt in the foreground), then disconnects cleanly
and returns. Installs NO signal handlers itself. Writes a fresh token at start (mode 0600;
on Windows restricts ACL as the doc says), deletes the token on clean exit. Logs to
`settings.daemon_log` (RotatingFileHandler, ~1 MB x 3) in addition to whatever root logging
exists; never logs the auth key.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from tpn.config import Settings
from tpn.control import new_token, write_token
from tpn.prefs import PrefsStore
from tpn.proc import SubprocessRunner
from tpn.service.acl import FileSecurity
from tpn.service.core import Service
from tpn.service.factory import DefaultParts, build_agent
from tpn.service.server import ControlServer
from tpn.state import StateStore
from tpn.tailscale import TailscaleClient

log = logging.getLogger(__name__)


def run(settings: Settings | None = None, stop: threading.Event | None = None) -> None:
    """Start the daemon and block until `stop` is set or KeyboardInterrupt."""
    settings = settings or Settings()
    daemon_stop = stop or threading.Event()
    pid = _pid()

    # Prepare state directory
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Logging to daemon log file (rotating ~1 MB x 3)
    _setup_daemon_log(settings.daemon_log)

    # Security: set up state dir and token permissions
    sec = FileSecurity(is_windows=False)
    sec.state_dir(settings.state_dir)

    # Write a fresh control token
    token = new_token()
    write_token(settings.token_file, token)
    sec.token(settings.token_file, token)

    # Build dependencies
    ts = TailscaleClient(SubprocessRunner(), binary=settings.tailscale_bin, socket=settings.socket_path)
    prefs_store = PrefsStore(settings.prefs_file)

    # If TPN_AUTH_KEY is in env at start, write it to the auth key file
    from tpn.config import Settings as _Settings
    _env_auth = _Settings().auth_key  # this reads TPN_AUTH_KEY
    if _env_auth and not settings.auth_key_file.is_file():
        try:
            sec.private_file(settings.auth_key_file, _env_auth.get_secret_value())
            log.info("auth key from TPN_AUTH_KEY stored")
        except Exception as e:
            log.warning("could not store env auth key: %s", e)

    # Service core (injected with real agent factory)
    def _build_agent(
        settings_: Settings,
        prefs_: Any,
        *,
        auth_key_file: Path | None = None,
        server: str | None = None,
        use_tor: bool | None = None,
        keep_tor: bool | None = None,
        store: StateStore | None = None,
        on_phase=None,
        clock=None,
        sleep=None,
    ) -> Any:
        return build_agent(
            settings_,
            prefs_,
            auth_key_file=auth_key_file,
            server=server,
            use_tor=use_tor,
            keep_tor=keep_tor,
            store=store,
            parts=DefaultParts(SubprocessRunner()),
            on_phase=on_phase,
            clock=clock,
            sleep=sleep,
        )

    service = Service(
        build_agent=_build_agent,
        prefs_store=prefs_store,
        ts=ts,
        settings=settings,
    )
    service.start(stop=daemon_stop)

    # Control server
    server = ControlServer(
        settings.control_port,
        service=service,
        token=token,
        token_file=settings.token_file,
        settings=settings,
        log_file=settings.daemon_log,
    )

    # Run the control server in a thread
    server_thread = threading.Thread(target=server.serve_forever, name="tpn-api", daemon=True)
    server_thread.start()

    log.info("tpn daemon started (pid %d)", pid)

    try:
        # Wait for the stop event or KeyboardInterrupt
        while not daemon_stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        log.info("keyboard interrupt, stopping")
        daemon_stop.set()

    # Shutdown sequence
    log.info("shutting down")
    service.shutdown()
    server.stop()
    server_thread.join(timeout=30)

    # Delete the token on clean exit
    try:
        settings.token_file.unlink(missing_ok=True)
        log.info("control token deleted")
    except OSError as e:
        log.warning("could not delete token file: %s", e)

    log.info("tpn daemon stopped")


def _setup_daemon_log(path: Path) -> None:
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _pid() -> int:
    import os

    return os.getpid()
