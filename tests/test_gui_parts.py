"""The tray, single instance, app thread hand-off and icons: headless, with fakes for pystray."""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

from tpn.gui.model import ActionState, Notification, Preferences, ServerRow, ViewState

ROOT = Path(__file__).resolve().parent.parent


# -- a fake pystray that calls actions the way the real one does ---------------------------------


def _pystray_call(action, icon, item):
    """pystray picks the call from co_argcount, defaults included (pystray._base)."""
    argcount = action.__code__.co_argcount - (1 if hasattr(action, "__self__") else 0)
    if argcount == 0:
        return action()
    if argcount == 1:
        return action(icon)
    if argcount == 2:
        return action(icon, item)
    raise ValueError(action)


class FakeMenuItem:
    def __init__(self, text, action, checked=None, radio=False, default=False, enabled=True):
        self.text, self.action, self.checked_fn = text, action, checked
        self.default, self.enabled = default, enabled

    def click(self, icon="ICON"):
        return _pystray_call(self.action, icon, self)

    @property
    def checked(self):
        return self.checked_fn(self) if self.checked_fn else None


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = [i for i in items if i is not FakeMenu.SEPARATOR]

    def find(self, text):
        return next(i for i in self.items if i.text.startswith(text))


class FakeIcon:
    HAS_NOTIFICATION = True

    def __init__(self, name, icon, title, menu):
        self.icon, self.title, self._menu = icon, title, menu
        self.menu_sets = 0
        self.notified: list[tuple[str, str]] = []

    @property
    def menu(self):
        return self._menu

    @menu.setter
    def menu(self, value):
        self.menu_sets += 1
        self._menu = value

    def run_detached(self):
        pass

    def notify(self, message, title=None):
        self.notified.append((title, message))

    def stop(self):
        pass


@pytest.fixture
def fake_pystray(monkeypatch):
    module = types.SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeMenuItem)
    monkeypatch.setitem(sys.modules, "pystray", module)
    pytest.importorskip("PIL")
    return module


class FakeModel:
    def __init__(self, state: ViewState):
        self.state = state
        self.calls: list[tuple] = []
        self.pending: list[Notification] = []

    def choose_server(self, hostname):
        self.calls.append(("choose_server", hostname))
        return True

    def connect(self):
        self.calls.append(("connect",))
        return True

    def disconnect(self):
        self.calls.append(("disconnect",))
        return True

    def toggle_pref(self, name, value):
        self.calls.append(("toggle_pref", name, value))
        return True

    def drain_notifications(self):
        out, self.pending = tuple(self.pending), []
        return out


def connected_state(**kw) -> ViewState:
    base = dict(
        icon_kind="direct",
        headline="Connected directly",
        tooltip="tpn: Connected directly — vpn-1",
        actions=ActionState(disconnect=True, server=True, prefs=True),
        prefs=Preferences(server=None, use_tor=True),
        prefs_loaded=True,
        servers=(
            ServerRow("vpn-1", True, True, 40.0, True),
            ServerRow("vpn-2", True, False, None, False),
        ),
    )
    base.update(kw)
    return ViewState(**base)


def make_tray(model):
    from tpn.gui.tray import TrayController

    ran: list = []
    tray = TrayController(
        model,
        on_show=lambda: ran.append("show"),
        on_quit=lambda: ran.append("quit"),
        marshal=lambda cb: cb(),
    )
    tray.start()
    return tray, ran


def test_tray_server_pick_passes_the_hostname(fake_pystray):
    model = FakeModel(connected_state())
    tray, _ = make_tray(model)
    servers = tray._icon.menu.find("Server").action
    servers.find("vpn-2").click()
    servers.find("Automatic").click()
    assert model.calls == [("choose_server", "vpn-2"), ("choose_server", None)]
    assert servers.find("Automatic").checked is True
    assert servers.find("vpn-1").checked is False
    assert servers.find("vpn-2").text == "vpn-2 (unhealthy)"


def test_tray_actions_and_checks(fake_pystray):
    model = FakeModel(connected_state())
    tray, ran = make_tray(model)
    menu = tray._icon.menu
    assert menu.find("Open").default is True
    assert menu.find("Connect").enabled is False and menu.find("Disconnect").enabled is True
    menu.find("Disconnect").click()
    menu.find("Route through Tor").click()
    assert menu.find("Route through Tor").checked is True
    menu.find("Open").click()
    menu.find("Quit").click()
    assert model.calls == [("disconnect",), ("toggle_pref", "use_tor", False)]
    assert ran == ["show", "quit"]


def test_tray_notifies_every_time(fake_pystray):
    model = FakeModel(connected_state())
    tray, _ = make_tray(model)
    for _ in range(2):
        model.pending.append(Notification("tpn connected", "Connected to vpn-1."))
        tray.refresh()
    assert tray._icon.notified == [("tpn connected", "Connected to vpn-1.")] * 2


def test_tray_title_is_latin1_safe(fake_pystray):
    from tpn.gui.tray import tray_title

    model = FakeModel(connected_state(tooltip="tpn: Starting Tor\u2026 \u2014 vpn-1 \u2603"))
    tray, _ = make_tray(model)
    tray.refresh()
    tray._icon.title.encode("latin-1")
    assert tray_title("a\u2026") == "a..."


def test_tray_rebuilds_menu_only_when_it_changes(fake_pystray):
    model = FakeModel(connected_state())
    tray, _ = make_tray(model)
    tray.refresh()
    sets = tray._icon.menu_sets
    for _ in range(5):
        tray.refresh()  # e.g. the one-second clock tick
    assert tray._icon.menu_sets == sets
    model.state = connected_state(headline="Disconnected", actions=ActionState(connect=True))
    tray.refresh()
    assert tray._icon.menu_sets == sets + 1
    assert tray._icon.icon.size == (64, 64)


# -- single instance ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX backend")
def test_single_instance_posix(tmp_path):
    from tpn.gui.single_instance import SingleInstance

    shown = threading.Event()
    first = SingleInstance(shown.set, windows=False, directory=tmp_path)
    second = SingleInstance(lambda: None, windows=False, directory=tmp_path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert second.signal_other() is True
        assert shown.wait(5)
        assert (tmp_path / "tpn-app.sock").stat().st_mode & 0o777 != 0
    finally:
        first.close()
        second.close()
    third = SingleInstance(lambda: None, windows=False, directory=tmp_path)
    try:
        assert third.acquire() is True  # the lock dies with its owner
    finally:
        third.close()


def test_single_instance_signal_without_owner(tmp_path):
    from tpn.gui.single_instance import SingleInstance

    lone = SingleInstance(lambda: None, windows=False, directory=tmp_path / "nobody")
    assert lone.signal_other() is False


class FakeKernel32:
    """Named objects shared between 'processes' (instances using the same fake)."""

    def __init__(self):
        self.objects: dict[str, threading.Event | str] = {}
        self.handles: dict[int, str] = {}
        self.last_error = 0
        self._next = 1

    def _handle(self, name):
        self._next += 1
        self.handles[self._next] = name
        return self._next

    def get_last_error(self):
        return self.last_error

    def CreateMutexW(self, sa, owner, name):
        self.last_error = 183 if name in self.objects else 0
        self.objects.setdefault(name, "mutex")
        return self._handle(name)

    def CreateEventW(self, sa, manual, initial, name):
        self.objects[name] = threading.Event()
        return self._handle(name)

    def OpenEventW(self, access, inherit, name):
        return self._handle(name) if isinstance(self.objects.get(name), threading.Event) else 0

    def SetEvent(self, handle):
        self.objects[self.handles[handle]].set()
        return 1

    def WaitForSingleObject(self, handle, ms):
        event = self.objects[self.handles[handle]]
        if event.wait(ms / 1000):
            event.clear()  # auto-reset
            return 0
        return 258

    def CloseHandle(self, handle):
        self.handles.pop(handle, None)
        return 1


def test_single_instance_windows_uses_session_objects():
    from tpn.gui.single_instance import EVENT_NAME, MUTEX_NAME, SingleInstance

    assert MUTEX_NAME.startswith("Local\\") and EVENT_NAME.startswith("Local\\")
    k32 = FakeKernel32()
    shown = threading.Event()
    first = SingleInstance(shown.set, windows=True, kernel32=k32)
    second = SingleInstance(lambda: None, windows=True, kernel32=k32)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert second.signal_other() is True
        assert shown.wait(5)
    finally:
        first.close()
        second.close()


def test_single_instance_failure_never_blocks_the_app(tmp_path):
    from tpn.gui.single_instance import SingleInstance

    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    assert SingleInstance(lambda: None, windows=False, directory=blocker).acquire() is True


# -- the app's thread hand-off -------------------------------------------------------------------


class FakeRoot:
    def __init__(self):
        self.after_calls: list = []

    def after(self, ms, callback):
        self.after_calls.append((threading.current_thread().name, callback))


def test_wake_from_another_thread_runs_on_the_tk_thread():
    from tpn.gui.app import DesktopApp

    app = DesktopApp(client=object(), single=types.SimpleNamespace(close=lambda: None))
    shown: list[str] = []
    app.window = types.SimpleNamespace(show=lambda: shown.append(threading.current_thread().name))
    app._wake()  # before the root exists: a no-op
    app._root = FakeRoot()
    thread = threading.Thread(target=app._wake, name="wake-thread")
    thread.start()
    thread.join(5)
    assert shown == [] and app._root.after_calls == []  # nothing touched Tk from that thread
    app._pump()  # what the Tk thread does every PUMP_MS
    assert shown == [threading.current_thread().name]
    assert [name for name, _ in app._root.after_calls] == [threading.current_thread().name]
    app._closing.set()
    app._wake()
    app._pump()
    assert len(shown) == 1


def test_pump_survives_a_failing_callback():
    from tpn.gui.app import DesktopApp

    app = DesktopApp(client=object(), single=types.SimpleNamespace(close=lambda: None))
    app._root = FakeRoot()
    ran: list[int] = []
    app._marshal(lambda: 1 / 0)
    app._marshal(lambda: ran.append(1))
    app._pump()
    assert ran == [1]


def test_gui_modules_import_without_a_display():
    import tpn.gui.app
    import tpn.gui.tray
    import tpn.gui.window

    assert callable(tpn.gui.app.main)


# -- icons -------------------------------------------------------------------------------------


def test_icons_render_with_distinct_colours():
    pytest.importorskip("PIL")
    from tpn.gui.icons import ICON_KINDS, render_icon

    for size in (16, 32, 256):
        dominant = set()
        for kind in ICON_KINDS:
            image = render_icon(kind, size)
            assert image.size == (size, size)
            opaque = [px[:3] for px in image.getdata() if px[3] > 200 and px[:3] != (255, 255, 255)]
            # The most common opaque, non-white colour is the state's fill.
            dominant.add(max(set(opaque), key=opaque.count))
        assert len(dominant) == len(ICON_KINDS), size


def test_ico_has_every_size():
    image_mod = pytest.importorskip("PIL.Image")
    ico = image_mod.open(ROOT / "packaging" / "icons" / "tpn.ico")
    assert sorted(ico.info["sizes"]) == [(s, s) for s in (16, 24, 32, 48, 64, 256)]


def test_poll_thread_stops_on_close():
    from tpn.gui.model import AppModel

    class Client:
        def status(self):
            raise __import__("tpn.control").control.DaemonNotRunning()

    model = AppModel(Client(), idle_interval=0.01)
    model.start()
    time.sleep(0.05)
    assert model.state.icon_kind == "failed" and "not running" in model.state.headline
    model.close()
