"""pystray tray icon implementation, entirely driven by the view state.

The tray never calls the daemon directly: actions are forwarded to the model and its
asynchronous action runner. Tkinter calls are marshalled through `root.after` so the
tkinter main thread owns every widget update. This module is completely importable on
a headless system because pystray is lazy-imported inside `_make_icon`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from tpn.gui.icons import ICON_SIZES, render_icon
from tpn.gui.model import ActionState, ViewState

log = logging.getLogger(__name__)


def _lazy_import_pystray() -> Any:
    import pystray

    return pystray


class TrayController:
    """Connect a pystray icon to an AppModel and a Tkinter `root.after` callback.

    - `on_show` and `on_quit` are called from pystray's thread; the app is responsible
      for marshalling them to the Tk main thread with `root.after`.
    - `on_change` is called from the model's poll thread or action threads; it schedules
      `_refresh` on the Tk main thread so `Icon.update_menu` and icon/title changes
      happen on the thread that created the pystray icon (its own detached thread).
    """

    def __init__(
        self,
        model: Any,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        marshal: Callable[[Callable[[], None]], None],
    ) -> None:
        self.model = model
        self.on_show = on_show
        self.on_quit = on_quit
        self.marshal = marshal
        self._icon: Any = None
        self._images: dict[str, Any] = {}
        self._icon_kind: str | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._notification_guard: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._icon is not None:
                return
            pystray = _lazy_import_pystray()
            image = self._render_image("disconnected", 16)
            self._icon = pystray.Icon(
                name="tpn",
                icon=image,
                title="tpn: Checking tpn service…",
                menu=self._make_menu(),
            )
            self._icon.visible = True
            # run_detached spins up the pystray thread and returns immediately.
            self._icon.run_detached()

    def _render_image(self, kind: str, size: int) -> Any:
        # PIL Image is cached per kind at the 16 px tray size; the installer uses 256.
        key = f"{kind}@{size}"
        if key not in self._images:
            self._images[key] = render_icon(kind, size)
        return self._images[key]

    def _make_menu(self) -> Any:
        pystray = _lazy_import_pystray()
        state = self.model.state
        disabled = lambda text: pystray.MenuItem(text, None, enabled=False)
        servers_menu = self._servers_menu(state)
        return pystray.Menu(
            disabled(state.headline),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Connect",
                lambda: self._marshal_action(self.model.connect),
                enabled=state.actions.connect,
                default=True,
            ),
            pystray.MenuItem(
                "Disconnect",
                lambda: self._marshal_action(self.model.disconnect),
                enabled=state.actions.disconnect,
            ),
            pystray.MenuItem("Server", servers_menu),
            pystray.MenuItem(
                "Route through Tor",
                lambda: self._marshal_action(
                    lambda: self.model.toggle_pref("use_tor", not state.prefs.use_tor)
                ),
                checked=lambda _: state.prefs.use_tor,
                enabled=state.actions.prefs,
            ),
            pystray.MenuItem(
                "Keep Tor",
                lambda: self._marshal_action(
                    lambda: self.model.toggle_pref("keep_tor", not state.prefs.keep_tor)
                ),
                checked=lambda _: state.prefs.keep_tor,
                enabled=state.actions.prefs,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open tpn…", lambda: self.marshal(self.on_show), default=False),
            pystray.MenuItem("Quit", lambda: self.marshal(self.on_quit)),
        )

    def _servers_menu(self, state: ViewState) -> Any:
        pystray = _lazy_import_pystray()
        items = []
        if state.servers:
            for server in state.servers:
                suffix = " (unhealthy)" if server.healthy is False else ""
                items.append(
                    pystray.MenuItem(
                        f"{server.hostname}{suffix}",
                        lambda _, h=server.hostname: self._marshal_action(
                            lambda: self.model.choose_server(h)
                        ),
                        checked=lambda _, h=server.hostname: state.prefs.server == h,
                        radio=True,
                        enabled=state.actions.server,
                    )
                )
        items.append(
            pystray.MenuItem(
                "Automatic",
                lambda _: self._marshal_action(lambda: self.model.choose_server(None)),
                checked=lambda _: state.prefs.server is None,
                radio=True,
                enabled=state.actions.server,
            )
        )
        return pystray.Menu(*items)

    def _marshal_action(self, action: Callable[[], bool]) -> None:
        """Wrap an action so a failure keeps the tray responsive and surfaces a notice."""
        def wrapped() -> None:
            try:
                action()
            except Exception as exc:
                log.debug("Tray action failed", exc_info=exc)
        self.marshal(wrapped)

    def refresh(self) -> None:
        """Called from the Tk main thread after the model publishes a new ViewState."""
        with self._lock:
            if self._closed or self._icon is None:
                return
            state = self.model.state
            # Update the icon image and tooltip when the kind changes.
            if state.icon_kind != self._icon_kind:
                image = self._render_image(state.icon_kind, 16)
                self._icon.icon = image
                self._icon_kind = state.icon_kind
            # Tooltip and title are the same string on most platforms.
            self._icon.title = state.tooltip
            # Rebuild the menu if the server list or enabled actions changed.
            self._icon.menu = self._make_menu()
            # Fire any daemon notifications (connected / disconnected / failed) once.
            for notice in self.model.drain_notifications():
                if notice.message in self._notification_guard:
                    continue
                self._notification_guard.add(notice.message)
                if len(self._notification_guard) > 20:
                    self._notification_guard.pop()
                try:
                    if getattr(self._icon, "HAS_NOTIFICATION", True):
                        self._icon.notify(notice.message, title=notice.title)
                except Exception:
                    # Notification may be unsupported on some desktops; ignore.
                    log.debug("Failed to show tray notification", exc_info=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._icon is not None:
                try:
                    self._icon.stop()
                except Exception:
                    log.debug("Error stopping tray icon", exc_info=True)
                self._icon = None


class TrayControllerForTest:
    """A no-op implementation for environments without pystray or a display."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def close(self) -> None:
        pass