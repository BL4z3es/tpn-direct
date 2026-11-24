"""The tpn desktop app: a tray icon and window that drive the daemon over tpn/control.py.

Threads, and who may touch what:
- Tk main thread: owns the single Tk root, the window (a Toplevel of it) and every widget.
- the model's poll thread and action threads: talk to the daemon, then call on_change.
- pystray's thread: runs the tray icon and calls menu actions.
- the single-instance wake thread: a second launch asking us to show the window.
Everything that is not the Tk thread reaches Tk only through `_marshal`, which puts the callback
on a queue that the Tk thread drains every PUMP_MS. Never root.after() from another thread: with
some Tk builds (Tk 9 included) it returns without error and the callback simply never runs.

Importable without a display or tkinter: both are imported when the app starts.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any

from tpn.config import Settings
from tpn.control import ControlClient
from tpn.gui.model import AppModel
from tpn.gui.single_instance import SingleInstance

log = logging.getLogger(__name__)

PUMP_MS = 50


def _dpi_aware() -> None:
    """Before Tk starts, so Windows renders text crisply instead of bitmap-scaling the window."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]


class DesktopApp:
    """Owns the app's lifetime: single-instance lock, model, Tk root, tray and window."""

    def __init__(self, client: Any = None, *, single: SingleInstance | None = None) -> None:
        settings = Settings()
        self.client = client or ControlClient(
            port=settings.control_port, token_file=settings.token_file
        )
        self.single = single or SingleInstance(self._wake)
        self.model: AppModel | None = None
        self.tray: Any = None
        self.window: Any = None
        self._root: Any = None
        self._closing = threading.Event()
        self._calls: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

    def run(self) -> None:
        if not self.single.acquire():
            if self.single.signal_other():
                log.info("tpn-app is already running; asked it to show its window")
                return
            log.warning("another tpn-app holds the lock but did not answer; starting anyway")
        _dpi_aware()
        import tkinter as tk

        from tpn.gui.tray import TrayController
        from tpn.gui.window import WindowController

        self._root = tk.Tk()
        self._root.withdraw()  # the root stays hidden; the window is its Toplevel
        self.model = AppModel(self.client, on_change=self._on_model_change)
        self.window = WindowController(
            self.model, master=self._root, on_close=self._hide_window, marshal=self._marshal
        )
        self.tray = TrayController(
            self.model, on_show=self._show_window, on_quit=self._quit, marshal=self._marshal
        )
        self._root.after(PUMP_MS, self._pump)
        self.model.start()
        try:
            self.tray.start()
        except Exception:
            # No notification area (some Linux desktops): run as a plain window instead.
            log.exception("the tray icon could not start; showing the window instead")
            self.tray = None
            self._marshal(self._show_window)
        try:
            self._root.mainloop()
        finally:
            self._close()

    # -- thread hand-off ---------------------------------------------------------------------

    def _marshal(self, callback: Callable[[], None]) -> None:
        """Run `callback` on the Tk thread soon. Safe from any thread; a no-op once closing."""
        if self._root is None or self._closing.is_set():
            return
        self._calls.put(callback)

    def _pump(self) -> None:
        """Tk thread: run the queued callbacks, then come back in PUMP_MS."""
        while True:
            try:
                callback = self._calls.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                log.exception("UI callback failed")
        if self._root is not None and not self._closing.is_set():
            self._root.after(PUMP_MS, self._pump)

    def _wake(self) -> None:
        """Called by SingleInstance on its own thread when a second launch asks for the window."""
        self._marshal(self._show_window)

    def _on_model_change(self) -> None:
        self._marshal(self._refresh_ui)

    # -- Tk thread only ----------------------------------------------------------------------

    def _refresh_ui(self) -> None:
        if self.tray is not None:
            self.tray.refresh()
        if self.window is not None:
            self.window._refresh()

    def _show_window(self) -> None:
        if self.window is not None:
            self.window.show()

    def _hide_window(self) -> None:
        if self.window is not None:
            self.window.hide()

    def _quit(self) -> None:
        # Quitting the app never touches the VPN: the daemon keeps it up.
        self._closing.set()
        if self._root is not None:
            self._root.quit()

    def _close(self) -> None:
        self._closing.set()
        for part in (self.tray, self.window, self.model):
            if part is not None:
                try:
                    part.close()
                except Exception:
                    log.debug("closing %s failed", type(part).__name__, exc_info=True)
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                log.debug("destroying the Tk root failed", exc_info=True)
            self._root = None
        self.single.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per 2-second poll otherwise
    try:
        DesktopApp().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("tpn-app failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
