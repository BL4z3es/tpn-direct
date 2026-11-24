"""pystray tray icon implementation, entirely driven by the view state.

The tray never calls the daemon directly: actions are forwarded to the model and its
asynchronous action runner. Tkinter calls are marshalled through `root.after` so the
tkinter main thread owns every widget update. This module is completely importable on
a headless system because pystray is lazy-imported inside `_make_icon`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from tpn.gui.icons import render_icon
from tpn.gui.model import ViewState

log = logging.getLogger(__name__)

# Rendered large and scaled down by the OS, so it stays crisp on high-DPI displays.
TRAY_SIZE = 64


def tray_title(text: str) -> str:
    """pystray's Xorg backend writes the title as latin-1; keep it to that on every platform."""
    text = text.replace("\u2026", "...").replace("\u2014", "-").replace("\u2013", "-")
    return text.encode("latin-1", "replace").decode("latin-1")


def menu_signature(state: ViewState) -> tuple[Any, ...]:
    """What the menu shows. The menu is rebuilt only when this changes, not on every tick."""
    return (
        state.headline,
        state.actions,
        state.prefs.use_tor,
        state.prefs.keep_tor,
        state.prefs.server,
        tuple((s.hostname, s.healthy) for s in state.servers),
    )


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
        self._menu_signature: tuple[Any, ...] | None = None

    def start(self) -> None:
        with self._lock:
            if self._icon is not None:
                return
            pystray = _lazy_import_pystray()
            image = self._render_image("disconnected", TRAY_SIZE)
            self._icon = pystray.Icon(
                name="tpn",
                icon=image,
                title=tray_title(self.model.state.tooltip),
                menu=self._make_menu(),
            )
            # run_detached starts pystray's thread and returns; its default setup makes the icon
            # visible. Setting .visible before that fails: the icon has no thread yet.
            self._icon.run_detached()

    def _render_image(self, kind: str, size: int) -> Any:
        # Cached per kind and size.
        key = f"{kind}@{size}"
        if key not in self._images:
            self._images[key] = render_icon(kind, size)
        return self._images[key]

    def _make_menu(self) -> Any:
        pystray = _lazy_import_pystray()
        state = self.model.state
        return pystray.Menu(
            pystray.MenuItem(state.headline, None, enabled=False),
            pystray.Menu.SEPARATOR,
            # Clicking the icon itself opens the window (the default item).
            pystray.MenuItem("Open tpn…", self._on_open, default=True),
            pystray.MenuItem("Connect", self._on_connect, enabled=state.actions.connect),
            pystray.MenuItem("Disconnect", self._on_disconnect, enabled=state.actions.disconnect),
            pystray.MenuItem("Server", self._servers_menu(state)),
            pystray.MenuItem(
                "Route through Tor",
                self._toggle("use_tor"),
                checked=self._checked("use_tor"),
                enabled=state.actions.prefs,
            ),
            pystray.MenuItem(
                "Keep Tor",
                self._toggle("keep_tor"),
                checked=self._checked("keep_tor"),
                enabled=state.actions.prefs,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    # pystray chooses how to call an action from its positional parameter count, defaults
    # included: every callable handed to it takes exactly (icon, item), or (item) for
    # `checked`, and nothing else. Values are bound by the factories below, never as defaults.

    def _on_open(self, icon: Any, item: Any) -> None:
        self.marshal(self.on_show)

    def _on_quit(self, icon: Any, item: Any) -> None:
        self.marshal(self.on_quit)

    def _on_connect(self, icon: Any, item: Any) -> None:
        self._marshal_action(self.model.connect)

    def _on_disconnect(self, icon: Any, item: Any) -> None:
        self._marshal_action(self.model.disconnect)

    def _toggle(self, name: str) -> Callable[[Any, Any], None]:
        def action(icon: Any, item: Any) -> None:
            current = getattr(self.model.state.prefs, name)
            self._marshal_action(lambda: self.model.toggle_pref(name, not current))

        return action

    def _checked(self, name: str) -> Callable[[Any], bool]:
        def checked(item: Any) -> bool:
            return bool(getattr(self.model.state.prefs, name))

        return checked

    def _pick(self, hostname: str | None) -> Callable[[Any, Any], None]:
        def action(icon: Any, item: Any) -> None:
            self._marshal_action(lambda: self.model.choose_server(hostname))

        return action

    def _is_server(self, hostname: str | None) -> Callable[[Any], bool]:
        def checked(item: Any) -> bool:
            return self.model.state.prefs.server == hostname

        return checked

    def _servers_menu(self, state: ViewState) -> Any:
        pystray = _lazy_import_pystray()
        items = [
            pystray.MenuItem(
                "Automatic",
                self._pick(None),
                checked=self._is_server(None),
                radio=True,
                enabled=state.actions.server,
            )
        ]
        for server in state.servers:
            suffix = " (unhealthy)" if server.healthy is False else ""
            items.append(
                pystray.MenuItem(
                    f"{server.hostname}{suffix}",
                    self._pick(server.hostname),
                    checked=self._is_server(server.hostname),
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
            except Exception:
                log.exception("tray action failed")

        self.marshal(wrapped)

    def refresh(self) -> None:
        """Called from the Tk main thread after the model publishes a new ViewState."""
        with self._lock:
            if self._closed or self._icon is None:
                return
            state = self.model.state
            # Update the icon image and tooltip when the kind changes.
            if state.icon_kind != self._icon_kind:
                self._icon.icon = self._render_image(state.icon_kind, TRAY_SIZE)
                self._icon_kind = state.icon_kind
            title = tray_title(state.tooltip)
            if self._icon.title != title:
                self._icon.title = title
            signature = menu_signature(state)
            if signature != self._menu_signature:
                self._menu_signature = signature
                self._icon.menu = self._make_menu()
            # The model hands out each notification exactly once.
            for notice in self.model.drain_notifications():
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
