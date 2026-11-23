"""tkinter window, entirely driven by the view state.

The window never calls the daemon directly: actions are forwarded to the model and its
asynchronous action runner. All Tk widget access happens on the Tk main thread (the only
thread that may touch Tk). The model's `on_change` schedules a `_refresh` through
`root.after`, and a periodic `tick` call updates time-dependent fields like the connected
timer and retry countdown.

Lazy imports keep the module importable on a headless CI system without tkinter.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from tpn.gui.model import ViewState
from tpn.gui.icons import render_icon

log = logging.getLogger(__name__)

LOG_MAX_LINES = 500


def _lazy_import_tkinter() -> tuple[Any, Any]:
    import tkinter as tk
    from tkinter import ttk

    return tk, ttk


class WindowController:
    """Connect a Tk window to an AppModel.

    The window is created hidden; `show()` and `hide()` are called from the tray
    controller (which runs on its own thread) through `root.after` so Tk remains
    single-threaded.
    """

    def __init__(
        self,
        model: Any,
        *,
        on_close: Callable[[], None],
        marshal: Callable[[Callable[[], None]], None],
    ) -> None:
        self.model = model
        self.on_close = on_close
        self.marshal = marshal
        self.root: tk.Tk | None = None
        self._notebook: ttk.Notebook | None = None
        self._status_widgets: dict[str, Any] = {}
        self._servers_tree: ttk.Treeview | None = None
        self._settings_widgets: dict[str, Any] = {}
        self._logs_text: tk.Text | None = None
        self._visible = False
        self._closed = False

    def _ensure_root(self) -> None:
        if self.root is not None:
            return
        tk, ttk = _lazy_import_tkinter()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("tpn")
        self.root.minsize(420, 360)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        # High-DPI awareness on Windows (guarded, no-op on other platforms).
        if hasattr(tk, "call"):
            try:
                from ctypes import windll

                windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI v2
            except Exception:
                try:
                    windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
        # Build the three tabs.
        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._build_status_tab()
        self._build_servers_tab()
        self._build_settings_tab()
        self._build_logs_tab()
        # Initial geometry and centre.
        self.root.update_idletasks()
        self.root.geometry(
            f"+{(self.root.winfo_screenwidth() - self.root.winfo_width()) // 2}"
            f"+{(self.root.winfo_screenheight() - self.root.winfo_height()) // 2}"
        )
        # Periodic UI clock (connected-for, retry countdown).
        self._tick()

    def _on_close_request(self) -> None:
        self.hide()

    def _tick(self) -> None:
        if self.root is None or self._closed:
            return
        try:
            self.model.tick()
        finally:
            self.root.after(1000, self._tick)

    def show(self) -> None:
        self._ensure_root()
        if self._visible:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            return
        self._visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._refresh()

    def hide(self) -> None:
        if self.root is not None and self._visible:
            self._visible = False
            self.root.withdraw()

    def _refresh(self) -> None:
        """Called from Tk main thread via `root.after` after model.on_change."""
        if self.root is None or self._closed:
            return
        state = self.model.state
        self._refresh_status(state)
        self._refresh_servers(state)
        self._refresh_settings(state)
        self._refresh_logs(state)
        self._refresh_buttons(state)

    def _marshal_action(self, action: Callable[[], bool]) -> None:
        def wrapped() -> None:
            try:
                action()
            except Exception as exc:
                log.debug("Window action failed", exc_info=exc)
        self.marshal(wrapped)

    # ---- Status tab ---------------------------------------------------------

    def _build_status_tab(self) -> None:
        _, ttk = _lazy_import_tkinter()
        frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(frame, text="Status")
        # Big state line.
        self._status_widgets["headline"] = ttk.Label(frame, text="Checking tpn service…", font=("Segoe UI", 13, "bold"))
        self._status_widgets["headline"].pack(anchor=tk.W, pady=(0, 4))
        # Transport, server, IP.
        details = ttk.Frame(frame)
        details.pack(fill=tk.X, pady=2)
        self._status_widgets["transport"] = ttk.Label(details, text="Transport: —")
        self._status_widgets["transport"].pack(anchor=tk.W)
        self._status_widgets["server"] = ttk.Label(details, text="Server: Automatic")
        self._status_widgets["server"].pack(anchor=tk.W)
        self._status_widgets["server_ip"] = ttk.Label(details, text="Exit IP: —")
        self._status_widgets["server_ip"].pack(anchor=tk.W)
        # Connected-for timer.
        self._status_widgets["connected_for"] = ttk.Label(frame, text="Connected for: —")
        self._status_widgets["connected_for"].pack(anchor=tk.W, pady=(4, 0))
        # Retry countdown.
        self._status_widgets["retry"] = ttk.Label(frame, text="", foreground="red")
        self._status_widgets["retry"].pack(anchor=tk.W)
        # Detail text.
        self._status_widgets["detail"] = ttk.Label(frame, text="", wraplength=380, justify=tk.LEFT)
        self._status_widgets["detail"].pack(fill=tk.X, pady=(8, 4))
        # Primary button.
        self._status_widgets["primary"] = ttk.Button(frame, text="Connect", command=self._on_primary)
        self._status_widgets["primary"].pack(fill=tk.X, pady=(4, 0))
        # Error / hint banner.
        self._status_widgets["hint"] = ttk.Label(frame, text="", foreground="red", wraplength=380, justify=tk.LEFT)
        self._status_widgets["hint"].pack(fill=tk.X, pady=(8, 0))

    def _refresh_status(self, state: ViewState) -> None:
        w = self._status_widgets
        w["headline"].config(text=state.headline)
        w["transport"].config(text=f"Transport: {state.transport}")
        w["server"].config(text=f"Server: {state.server}")
        w["server_ip"].config(text=f"Exit IP: {state.server_ip}")
        w["connected_for"].config(text=f"Connected for: {state.connected_for}")
        w["retry"].config(text=state.retry)
        w["detail"].config(text=state.detail)
        w["hint"].config(text=state.hint)
        primary = w["primary"]
        if state.actions.connect:
            primary.config(text="Connect", command=lambda: self._marshal_action(self.model.connect))
        elif state.actions.disconnect:
            primary.config(text="Disconnect", command=lambda: self._marshal_action(self.model.disconnect))
        else:
            primary.config(text="Connect", state=tk.DISABLED)
        if state.error:
            w["hint"].config(text=state.error)
        if state.notice:
            w["hint"].config(text=state.notice)

    def _on_primary(self) -> None:
        state = self.model.state
        if state.actions.connect:
            self._marshal_action(self.model.connect)
        elif state.actions.disconnect:
            self._marshal_action(self.model.disconnect)

    # ---- Servers tab --------------------------------------------------------

    def _build_servers_tab(self) -> None:
        _, ttk = _lazy_import_tkinter()
        frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(frame, text="Servers")
        cols = ("hostname", "online", "healthy", "latency", "selected")
        self._servers_tree = ttk.Treeview(frame, columns=cols, show="headings", height=10)
        self._servers_tree.heading("hostname", text="Hostname")
        self._servers_tree.heading("online", text="Online")
        self._servers_tree.heading("healthy", text="Healthy")
        self._servers_tree.heading("latency", text="Latency (ms)")
        self._servers_tree.heading("selected", text="Selected")
        self._servers_tree.column("hostname", width=180, anchor=tk.W)
        self._servers_tree.column("online", width=60, anchor=tk.CENTER)
        self._servers_tree.column("healthy", width=70, anchor=tk.CENTER)
        self._servers_tree.column("latency", width=100, anchor=tk.CENTER)
        self._servers_tree.column("selected", width=70, anchor=tk.CENTER)
        self._servers_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._servers_tree.yview)
        self._servers_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(fill=tk.Y, side=tk.LEFT)
        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btns, text="Refresh", command=lambda: self._marshal_action(self.model.refresh_servers)).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Use this server", command=self._on_use_server).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Automatic", command=lambda: self._marshal_action(lambda: self.model.choose_server(None))).pack(side=tk.LEFT)

    def _on_use_server(self) -> None:
        if self._servers_tree is None:
            return
        selection = self._servers_tree.selection()
        if not selection:
            return
        item = self._servers_tree.item(selection[0])
        hostname = item["values"][0]
        if hostname:
            self._marshal_action(lambda: self.model.choose_server(hostname))

    def _refresh_servers(self, state: ViewState) -> None:
        if self._servers_tree is None:
            return
        # Preserve selection if possible.
        selected = self._servers_tree.selection()
        selected_host = self._servers_tree.item(selected[0])["values"][0] if selected else None
        self._servers_tree.delete(*self._servers_tree.get_children())
        for server in state.servers:
            online = "Yes" if server.online else "No"
            healthy = "Yes" if server.healthy else ("No" if server.healthy is False else "—")
            latency = f"{server.latency_ms:.1f}" if server.latency_ms is not None else "—"
            selected = "Yes" if server.selected else "No"
            self._servers_tree.insert("", tk.END, values=(server.hostname, online, healthy, latency, selected))
        # Restore selection.
        if selected_host:
            for item_id in self._servers_tree.get_children():
                if self._servers_tree.item(item_id)["values"][0] == selected_host:
                    self._servers_tree.selection_set(item_id)
                    self._servers_tree.see(item_id)
                    break

    # ---- Settings tab -------------------------------------------------------

    def _build_settings_tab(self) -> None:
        _, ttk = _lazy_import_tkinter()
        frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(frame, text="Settings")
        # Auth key.
        auth_frame = ttk.LabelFrame(frame, text="Authentication key", padding=8)
        auth_frame.pack(fill=tk.X, pady=(0, 8))
        self._settings_widgets["auth_status"] = ttk.Label(auth_frame, text="No key stored")
        self._settings_widgets["auth_status"].pack(anchor=tk.W, pady=(0, 4))
        entry_frame = ttk.Frame(auth_frame)
        entry_frame.pack(fill=tk.X, pady=(0, 4))
        self._settings_widgets["auth_entry"] = ttk.Entry(entry_frame, show="●")
        self._settings_widgets["auth_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(entry_frame, text="Save key", command=self._on_save_key).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(entry_frame, text="Clear key", command=lambda: self._marshal_action(self.model.clear_auth_key)).pack(side=tk.LEFT, padx=(4, 0))
        # Hostname.
        host_frame = ttk.LabelFrame(frame, text="Device hostname (optional)", padding=8)
        host_frame.pack(fill=tk.X, pady=(0, 8))
        self._settings_widgets["hostname"] = ttk.Entry(host_frame)
        self._settings_widgets["hostname"].pack(fill=tk.X)
        # Tor bridges.
        bridges_frame = ttk.LabelFrame(frame, text="Tor bridges (one per line, optional)", padding=8)
        bridges_frame.pack(fill=tk.X, pady=(0, 8))
        self._settings_widgets["bridges"] = tk.Text(bridges_frame, height=4, wrap=tk.NONE)
        self._settings_widgets["bridges"].pack(fill=tk.X)
        # Checkboxes.
        checks = ttk.Frame(frame)
        checks.pack(fill=tk.X, pady=(0, 8))
        self._settings_widgets["use_tor"] = tk.BooleanVar()
        ttk.Checkbutton(checks, text="Route through Tor", variable=self._settings_widgets["use_tor"],
                       command=lambda: self._marshal_action(
                           lambda: self.model.toggle_pref("use_tor", self._settings_widgets["use_tor"].get())
                       )).pack(anchor=tk.W)
        self._settings_widgets["keep_tor"] = tk.BooleanVar()
        ttk.Checkbutton(checks, text="Keep Tor (never drop it)", variable=self._settings_widgets["keep_tor"],
                       command=lambda: self._marshal_action(
                           lambda: self.model.toggle_pref("keep_tor", self._settings_widgets["keep_tor"].get())
                       )).pack(anchor=tk.W)
        self._settings_widgets["auto_connect"] = tk.BooleanVar()
        ttk.Checkbutton(checks, text="Connect automatically at boot", variable=self._settings_widgets["auto_connect"],
                       command=lambda: self._marshal_action(
                           lambda: self.model.toggle_pref("auto_connect", self._settings_widgets["auto_connect"].get())
                       )).pack(anchor=tk.W)
        # Save button and error banner.
        ttk.Button(frame, text="Save settings", command=self._on_save_settings).pack(fill=tk.X, pady=(4, 0))
        self._settings_widgets["error"] = ttk.Label(frame, text="", foreground="red", wraplength=380, justify=tk.LEFT)
        self._settings_widgets["error"].pack(fill=tk.X, pady=(8, 0))

    def _refresh_settings(self, state: ViewState) -> None:
        w = self._settings_widgets
        if state.prefs_loaded:
            prefs = state.prefs
            w["auth_status"].config(text="A key is stored" if state.has_auth_key else "No key stored")
            w["hostname"].delete(0, tk.END)
            w["hostname"].insert(0, prefs.hostname or "")
            w["bridges"].delete("1.0", tk.END)
            if prefs.tor_bridges:
                w["bridges"].insert("1.0", "\n".join(prefs.tor_bridges))
            w["use_tor"].set(prefs.use_tor)
            w["keep_tor"].set(prefs.keep_tor)
            w["auto_connect"].set(prefs.auto_connect)
        w["error"].config(text=state.settings_error)

    def _on_save_key(self) -> None:
        key = self._settings_widgets["auth_entry"].get().strip()
        if key:
            self._marshal_action(lambda: self.model.set_auth_key(key))
            self._settings_widgets["auth_entry"].delete(0, tk.END)

    def _on_save_settings(self) -> None:
        hostname = self._settings_widgets["hostname"].get().strip() or None
        bridges_text = self._settings_widgets["bridges"].get("1.0", tk.END)
        bridges = [line.strip() for line in bridges_text.splitlines() if line.strip()]
        self._marshal_action(lambda: self.model.save_settings(
            hostname=hostname,
            tor_bridges=bridges,
            use_tor=self._settings_widgets["use_tor"].get(),
            keep_tor=self._settings_widgets["keep_tor"].get(),
            auto_connect=self._settings_widgets["auto_connect"].get(),
        ))

    # ---- Logs tab -----------------------------------------------------------

    def _build_logs_tab(self) -> None:
        _, ttk = _lazy_import_tkinter()
        frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(frame, text="Logs")
        self._logs_text = tk.Text(frame, height=20, wrap=tk.NONE, state=tk.DISABLED)
        self._logs_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._logs_text.yview)
        self._logs_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(fill=tk.Y, side=tk.LEFT)
        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btns, text="Refresh", command=lambda: self._marshal_action(self.model.refresh_logs)).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Copy to clipboard", command=self._on_copy_logs).pack(side=tk.LEFT)

    def _refresh_logs(self, state: ViewState) -> None:
        if self._logs_text is None:
            return
        self._logs_text.config(state=tk.NORMAL)
        self._logs_text.delete("1.0", tk.END)
        if state.logs:
            self._logs_text.insert("1.0", "\n".join(state.logs))
            self._logs_text.see(tk.END)
        self._logs_text.config(state=tk.DISABLED)

    def _on_copy_logs(self) -> None:
        if self._logs_text is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self._logs_text.get("1.0", tk.END).rstrip())

    # ---- Common button state ------------------------------------------------

    def _refresh_buttons(self, state: ViewState) -> None:
        """Update per-tab buttons for busy state and daemon availability."""
        if self._status_widgets.get("primary"):
            self._status_widgets["primary"].config(state=tk.NORMAL if (state.actions.connect or state.actions.disconnect) else tk.DISABLED)
        # The servers, settings and logs tabs use the same logic implicitly through action_state.

    def close(self) -> None:
        self._closed = True
        if self.root is not None:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                log.debug("Error destroying Tk window", exc_info=True)
            self.root = None


class WindowControllerForTest:
    """A no-op implementation for environments without tkinter or a display."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def _refresh(self) -> None:
        pass

    def close(self) -> None:
        pass