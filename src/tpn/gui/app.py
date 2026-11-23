"""The tpn desktop app: a tray icon and window that drive the daemon over tpn/control.py.

All daemon interactions go through AppModel, which runs its poll loop on a background thread.
Tray callbacks and window events marshal to the Tk main thread via root.after. A second
launch signal_other's the first instance and exits.

The module is fully importable without a display: tkinter and pystray are lazy-imported
inside the controllers that need them, so headless tests can exercise the model and the
single-instance logic.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from tpn.config import Settings
from tpn.control import ControlClient
from tpn.gui.model import AppModel
from tpn.gui.single_instance import SingleInstance

log = logging.getLogger(__name__)


class DesktopApp:
    """The application lifetime owner: single-instance lock, model, tray, window."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.client = ControlClient(
            port=self.settings.control_port,
            token_file=self.settings.token_file,
        )
        self.model: AppModel | None = None
        self.tray: Any = None
        self.window: Any = None
        self.single: SingleInstance | None = None
        self._root: Any = None
        self._started = False
        self._closing = threading.Event()

    def run(self) -> None:
        if self._started:
            return
        self._started = True
        # High-DPI awareness on Windows (guarded, no-op on other platforms).
        if sys.platform == "win32":
            try:
                from ctypes import windll

                windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI v2
            except Exception:
                try:
                    windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

        # Single instance: acquire the lock or signal the existing owner.
        self.single = SingleInstance(self._show_window)
        if not self.single.acquire():
            if self.single.signal_other():
                return
            # If signalling failed (stale lock), try once more after a tiny delay.
            time.sleep(0.2)
            if self.single.signal_other():
                return
            log.warning("Another tpn-app is running but could not be signalled; continuing.")

        # Create the model, tray and window.
        self.model = AppModel(
            self.client,
            on_change=self._on_model_change,
            action_runner=self._run_on_background_thread,
        )
        self.model.start()

        # Build the Tk root and UI controllers.
        self._build_tk()

        # Start the tray icon (pystray thread).
        self._start_tray()

        # Enter the Tk main loop.
        try:
            self._root.mainloop()
        finally:
            self._close()

    def _build_tk(self) -> None:
        import tkinter as tk

        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("tpn")
        self._root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Import UI controllers lazily so they can be replaced in tests.
        from tpn.gui.tray import TrayController
        from tpn.gui.window import WindowController

        def marshal(callback):
            self._root.after(0, callback)

        self.tray = TrayController(
            self.model,
            on_show=self._show_window,
            on_quit=self._quit,
            marshal=marshal,
        )
        self.window = WindowController(
            self.model,
            on_close=self._on_window_close,
            marshal=marshal,
        )

    def _start_tray(self) -> None:
        self.tray.start()

    def _on_model_change(self) -> None:
        """Called from the model's poll or action threads; schedule a UI refresh."""
        if self._root is not None and not self._closing.is_set():
            self._root.after(0, self._refresh_ui)

    def _refresh_ui(self) -> None:
        if self.tray is not None:
            self.tray.refresh()
        if self.window is not None:
            self.window._refresh()

    def _show_window(self) -> None:
        if self.window is not None:
            self.window.show()

    def _on_window_close(self) -> None:
        if self.window is not None:
            self.window.hide()

    def _quit(self) -> None:
        self._closing.set()
        if self._root is not None:
            self._root.quit()

    def _run_on_background_thread(self, work):
        threading.Thread(target=work, name="tpn-control-action", daemon=True).start()

    def _close(self) -> None:
        self._closing.set()
        if self.tray is not None:
            self.tray.close()
        if self.window is not None:
            self.window.close()
        if self.model is not None:
            self.model.close()
        if self.single is not None:
            self.single.close()
        self._started = False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = DesktopApp()
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error in tpn-app")
        sys.exit(1)


if __name__ == "__main__":
    main()