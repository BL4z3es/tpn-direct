"""One tpn-app per login session; a second launch asks the first to show its window.

Windows: a named mutex and a named auto-reset event in the session's `Local\\` namespace. Both
are per login session, so two signed-in users each get their own app, and no network port is
involved that another process could take.

POSIX: flock() on a lock file plus a Unix socket, both in a per-user 0700 directory
($XDG_RUNTIME_DIR, else a tpn-app-<uid> directory in the temp dir). flock is released by the
kernel when the process dies, so a crash never leaves a stale lock.

`on_show` runs on a background thread: the caller must marshal it to its UI thread.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\tpn-app"
EVENT_NAME = "Local\\tpn-app-show"
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_EVENT_MODIFY_STATE = 0x0002


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime)
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"tpn-app-{uid}"


class SingleInstance:
    """acquire() -> True if we are the first instance (and will receive on_show calls)."""

    def __init__(
        self,
        on_show: Callable[[], None],
        *,
        windows: bool | None = None,
        directory: Path | None = None,
        kernel32: Any = None,
    ) -> None:
        self._on_show = on_show
        self._windows = sys.platform == "win32" if windows is None else windows
        self._dir = directory or runtime_dir()
        self._kernel32 = kernel32
        self._closed = threading.Event()
        self._handles: list[Any] = []
        self._lock_fd: int | None = None
        self._server: socket.socket | None = None

    # -- public ------------------------------------------------------------------------------

    def acquire(self) -> bool:
        try:
            return self._acquire_windows() if self._windows else self._acquire_posix()
        except OSError as e:
            # Never block the app over this: run without single-instance protection.
            log.warning("single-instance check failed, continuing without it: %s", e)
            return True

    def signal_other(self) -> bool:
        """Ask the running instance to show its window."""
        try:
            return self._signal_windows() if self._windows else self._signal_posix()
        except OSError as e:
            log.warning("could not reach the running tpn-app: %s", e)
            return False

    def close(self) -> None:
        self._closed.set()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
            with contextlib.suppress(OSError):
                (self._dir / "tpn-app.sock").unlink()
            self._server = None
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None
        for handle in self._handles:
            with contextlib.suppress(Exception):
                self._k32().CloseHandle(handle)
        self._handles.clear()

    # -- Windows -----------------------------------------------------------------------------

    def _k32(self) -> Any:
        if self._kernel32 is None:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateEventW.argtypes = [
                ctypes.c_void_p,
                wintypes.BOOL,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            k32.CreateEventW.restype = wintypes.HANDLE
            k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            k32.OpenEventW.restype = wintypes.HANDLE
            k32.SetEvent.argtypes = [wintypes.HANDLE]
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.get_last_error = ctypes.get_last_error
            self._kernel32 = k32
        return self._kernel32

    def _acquire_windows(self) -> bool:
        k32 = self._k32()
        mutex = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not mutex:
            raise OSError("CreateMutexW failed")
        if k32.get_last_error() == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(mutex)
            return False
        self._handles.append(mutex)
        event = k32.CreateEventW(None, False, False, EVENT_NAME)  # auto-reset, unsignalled
        if not event:
            raise OSError("CreateEventW failed")
        self._handles.append(event)
        threading.Thread(
            target=self._wait_event, args=(event,), name="tpn-app-wake", daemon=True
        ).start()
        return True

    def _wait_event(self, event: Any) -> None:
        k32 = self._k32()
        while not self._closed.is_set():
            if k32.WaitForSingleObject(event, 500) == _WAIT_OBJECT_0 and not self._closed.is_set():
                self._show()

    def _signal_windows(self) -> bool:
        k32 = self._k32()
        event = k32.OpenEventW(_EVENT_MODIFY_STATE, False, EVENT_NAME)
        if not event:
            return False
        try:
            return bool(k32.SetEvent(event))
        finally:
            k32.CloseHandle(event)

    # -- POSIX -------------------------------------------------------------------------------

    def _acquire_posix(self) -> bool:
        import fcntl

        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._dir / "tpn-app.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._lock_fd = fd
        path = self._dir / "tpn-app.sock"
        path.unlink(missing_ok=True)  # left by a crashed owner; we hold the lock now
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(4)
        server.settimeout(0.5)
        self._server = server
        threading.Thread(target=self._serve, name="tpn-app-wake", daemon=True).start()
        return True

    def _serve(self) -> None:
        server = self._server
        while server is not None and not self._closed.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                with contextlib.suppress(OSError):
                    if conn.recv(16).startswith(b"show"):
                        self._show()
                        conn.sendall(b"ok")

    def _signal_posix(self) -> bool:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2.0)
            conn.connect(str(self._dir / "tpn-app.sock"))
            conn.sendall(b"show")
            return conn.recv(16) == b"ok"

    def _show(self) -> None:
        try:
            self._on_show()
        except Exception:
            log.exception("showing the window failed")
