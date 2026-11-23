"""The tpn daemon: owns tor and tailscaled, and serves the control API (tpn/control.py).

    run(settings: Settings | None = None, stop: threading.Event | None = None) -> None

Blocks until `stop` is set (or KeyboardInterrupt in the foreground), then disconnects and
returns. It installs no signal handlers, so the Windows service host calls it from its own
thread; `tpn daemon` wires SIGTERM/SIGINT to `stop` itself.
"""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from tpn.config import Settings
from tpn.control import new_token
from tpn.prefs import PrefsStore
from tpn.service.acl import FileSecurity
from tpn.service.core import Service
from tpn.service.factory import tailscale_client
from tpn.service.server import ControlServer

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _Redact(logging.Filter):
    """Last line of defence: no tskey-... ever reaches the log file, whoever logs it."""

    def __init__(self, security: FileSecurity) -> None:
        super().__init__()
        self._security = security

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._security.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def _file_logging(path: Path, security: FileSecurity) -> logging.Handler:
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_Redact(security))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler


def run(
    settings: Settings | None = None,
    stop: threading.Event | None = None,
    *,
    service: Service | None = None,
    security: FileSecurity | None = None,
) -> None:
    """`service` and `security` are for tests; production builds them from `settings`."""
    settings = settings or Settings()
    stop = stop or threading.Event()
    security = security or FileSecurity()
    security.state_dir(settings.state_dir)
    handler = _file_logging(settings.daemon_log, security)
    try:
        if settings.auth_key is not None:
            # TPN_AUTH_KEY in the service's environment: same lifecycle as PUT /auth-key.
            security.private_file(settings.auth_key_file, settings.auth_key.get_secret_value())
        service = service or Service(
            settings,
            prefs_store=PrefsStore(settings.prefs_file),
            ts=tailscale_client(settings),
            security=security,
        )
        token = new_token()
        server = ControlServer(service, token=token, port=settings.control_port)
        security.token(settings.token_file, token)
        api = threading.Thread(target=server.serve_forever, name="tpn-api", daemon=True)
        api.start()
        log.info("tpn daemon started (control API on 127.0.0.1:%d)", server.port)
        try:
            service.start()
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            log.info("tpn daemon stopping")
            server.shutdown()
            settings.token_file.unlink(missing_ok=True)
            service.shutdown()
            api.join(5)
            log.info("tpn daemon stopped")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
