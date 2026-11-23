"""The tpn daemon: owns tor and tailscaled, and serves the control API (tpn/control.py).

Stub on the foundation commit; the daemon worker replaces it. Contract:

    run(settings: Settings | None = None, stop: threading.Event | None = None) -> None

Blocks until `stop` is set (or KeyboardInterrupt in the foreground), then disconnects and
returns. It installs no signal handlers, so the Windows service can call it from any thread.
"""

from __future__ import annotations

import threading

from tpn.config import Settings


def run(settings: Settings | None = None, stop: threading.Event | None = None) -> None:
    raise NotImplementedError("the tpn daemon is not built yet")
