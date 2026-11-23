"""One app per login session, with an explicit wake-up channel.

A Windows mutex alone can prevent a second process but cannot ask the first process to show its
window. Each owner therefore also runs a tiny localhost socket, with a capability token in the
lock file (or beside the mutex) and a private wake-up event. No daemon traffic goes over this
socket; it accepts only a single, timed, authenticated `show` message.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

WAKE_PORT = 39081  # The app instance's wake-up port, never the daemon's control port (9081).


class WakeServer:
    """A short-lived localhost listener, injectable for deterministic single-instance tests."""

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        port: int = WAKE_PORT,
        host: str = "127.0.0.1",
        backlog: int = 4,
    ) -> None:
        self.callback = callback
        self.port = port
        self.host = host
        self.backlog = backlog
        self.token = secrets.token_urlsafe(32)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.bind((host, port))
            self._socket.listen(backlog)
            self._socket.settimeout(0.5)
        except Exception:
            self._socket.close()
            raise
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name="tpn-app-wake", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(1.0)
                try:
                    request = connection.recv(1024).decode("utf-8")
                    data = json.loads(request)
                    if (
                        isinstance(data, dict)
                        and data.get("action") == "show"
                        and hmac.compare_digest(str(data.get("token", "")), self.token)
                    ):
                        self.callback()
                        connection.sendall(b"ok")
                except (OSError, UnicodeError, ValueError):
                    # Wake-up is best effort; an invalid localhost datagram cannot stop the app.
                    continue

    def signal(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=1.5) as connection:
                connection.settimeout(1.5)
                connection.sendall(
                    json.dumps({"action": "show", "token": self.token}).encode("utf-8")
                )
                return connection.recv(16) == b"ok"
        except OSError:
            return False

    def close(self) -> None:
        self._closed.set()
        try:
            self._socket.close()
        except OSError:
            pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)


class SingleInstance:
    """Acquire a per-session lock and expose `signal_other` / `close` to the app.

    Pass `platform` and `lock_path` in tests rather than relying on the host OS. On Windows the
    mutex is created through ctypes and kept alive until close; the event makes focus immediate
    when the lock metadata is available. On other platforms the lock file uses `O_EXCL`, with
    the PID and wake-up token so a second launch can authenticate to the first instance.
    """

    def __init__(
        self,
        on_show: Callable[[], None],
        *,
        platform: str | None = None,
        lock_path: Path | None = None,
        wake_server: Any | None = None,
        mutex_name: str = r"Local\tpn-app",
        event_name: str = r"Local\tpn-app-show",
    ) -> None:
        self.on_show = on_show
        self.platform = platform or sys.platform
        self.lock_path = lock_path or self.default_lock_path()
        self.mutex_name = mutex_name
        self.event_name = event_name
        self.wake_server = wake_server
        self.acquired = False
        self._mutex: int | None = None
        self._event: int | None = None
        self._metadata_written = False
        self._kernel32: Any | None = None
        self._lock_file: Any | None = None
        self._closed = False

    @staticmethod
    def default_lock_path(platform: str | None = None) -> Path:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        directory = Path(runtime) if runtime else Path(tempfile.gettempdir())
        platform = platform or sys.platform
        if platform == "win32":
            # Isolate metadata as well as the mutex: local sessions may share a temp directory.
            directory /= f"tpn-app-{os.getpid()}"
        return directory / f"tpn-app-{os.getuid() if hasattr(os, 'getuid') else 'user'}.json"

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if self.platform == "win32":
            if not self._acquire_windows_mutex():
                return False
        elif not self._acquire_file_lock():
            return False

        try:
            if self.wake_server is None:
                self.wake_server = WakeServer(self._show, port=WAKE_PORT)
            self.wake_server.start()
            self._write_metadata()
            if self.platform == "win32":
                self._start_windows_event()
        except Exception:
            self.close()
            return False
        self.acquired = True
        return True

    def _show(self) -> None:
        # WakeServer calls this on its own thread; DesktopApp marshals it to Tk with `after`.
        self.on_show()

    def _acquire_file_lock(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            self._lock_file = os.fdopen(fd, "r+", encoding="utf-8")
            return True
        except FileExistsError:
            if self._stale_lock():
                try:
                    self.lock_path.unlink()
                except OSError:
                    return False
                try:
                    fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                    self._lock_file = os.fdopen(fd, "r+", encoding="utf-8")
                    return True
                except FileExistsError:
                    return False
            return False

    def _stale_lock(self) -> bool:
        try:
            metadata = json.loads(self.lock_path.read_text(encoding="utf-8"))
            pid = int(metadata["pid"])
        except (OSError, ValueError, TypeError, KeyError):
            return True
        if pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return False
        return False

    def _write_metadata(self) -> None:
        metadata = {
            "pid": os.getpid(),
            "port": self.wake_server.port,
            "token": self.wake_server.token,
        }
        self._lock_file.seek(0)
        self._lock_file.truncate()
        json.dump(metadata, self._lock_file)
        self._lock_file.flush()
        os.fsync(self._lock_file.fileno())
        self._metadata_written = True

    def _acquire_windows_mutex(self) -> bool:
        try:
            import ctypes

            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            self._kernel32.CreateMutexW.restype = ctypes.c_void_p
            self._kernel32.GetLastError.restype = ctypes.c_ulong
            mutex = self._kernel32.CreateMutexW(None, False, self.mutex_name)
            if not mutex:
                return False
            if self._kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                self._kernel32.CloseHandle(ctypes.c_void_p(mutex))
                return False
            self._mutex = mutex
            return True
        except (AttributeError, OSError):
            return False

    def _start_windows_event(self) -> None:
        if self._kernel32 is None:
            return
        import ctypes

        self._kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        self._event = self._kernel32.CreateEventW(None, False, False, self.event_name)
        if self._event:
            threading.Thread(target=self._wait_for_event, name="tpn-app-event", daemon=True).start()

    def _wait_for_event(self) -> None:
        if self._kernel32 is None or not self._event:
            return
        import ctypes

        while not self._closed and self._event:
            result = self._kernel32.WaitForSingleObject(ctypes.c_void_p(self._event), 500)
            if result == 0 and not self._closed:
                self._show()
            elif result != 258:  # WAIT_TIMEOUT
                break

    def signal_other(self) -> bool:
        """Ask the owner to focus its window; prefer the event, then use the authenticated socket."""
        if self.platform == "win32":
            if self._signal_windows_event():
                return True
        try:
            metadata = json.loads(self.lock_path.read_text(encoding="utf-8"))
            port, token = int(metadata["port"]), str(metadata["token"])
        except (OSError, ValueError, TypeError, KeyError):
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5) as connection:
                connection.settimeout(1.5)
                connection.sendall(json.dumps({"action": "show", "token": token}).encode())
                return connection.recv(16) == b"ok"
        except OSError:
            return False

    def _signal_windows_event(self) -> bool:
        if self._kernel32 is None:
            return False
        import ctypes

        self._kernel32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel32.OpenEventW.restype = ctypes.c_void_p
        event = self._kernel32.OpenEventW(0x0002, False, self.event_name)  # EVENT_MODIFY_STATE
        if not event:
            return False
        try:
            return bool(self._kernel32.SetEvent(ctypes.c_void_p(event)))
        finally:
            self._kernel32.CloseHandle(ctypes.c_void_p(event))

    def close(self) -> None:
        self._closed = True
        if self.wake_server is not None:
            self.wake_server.close()
        if self._lock_file is not None:
            try:
                self._lock_file.close()
            except OSError:
                pass
            self._lock_file = None
        if self._metadata_written:
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._kernel32 is not None:
            import ctypes

            for handle in (self._mutex, self._event):
                if handle:
                    self._kernel32.CloseHandle(ctypes.c_void_p(handle))
            self._mutex = self._event = None
        self.acquired = False


class AlreadyRunning:
    """Compatibility-friendly result for callers that prefer a named second-instance outcome."""
