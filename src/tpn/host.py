"""What differs between the operating systems the client runs on.

Linux and Windows (x64 and ARM64) run this Python client. Android is a separate app built from
tailscale-android (see android/); nothing here runs there.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# Where the official Windows installer puts the CLI. It is not always on PATH.
_WINDOWS_TAILSCALE = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Tailscale"


def default_state_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "tpn"
    return Path("/var/lib/tpn")


def default_daemon_mode() -> str:
    # On Windows tailscaled is always the "Tailscale" service: it owns the Wintun adapter and
    # the named pipe the CLI talks to, so there is no private instance to run.
    return "windows" if IS_WINDOWS else "managed"


def bundle_dir() -> Path:
    """The directory holding the files shipped next to the program (tor/, on Windows)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_binary(name: str, *extra_dirs: Path) -> str:
    """Return a path to `name`, looking in `extra_dirs` before PATH; else `name` unchanged."""
    exe = f"{name}.exe" if IS_WINDOWS and not name.lower().endswith(".exe") else name
    for directory in extra_dirs:
        candidate = directory / exe
        if candidate.is_file():
            return str(candidate)
    return shutil.which(exe) or shutil.which(name) or exe


def tailscale_binary() -> str:
    return find_binary("tailscale", _WINDOWS_TAILSCALE) if IS_WINDOWS else "tailscale"


def tor_binary() -> str:
    return find_binary("tor", bundle_dir() / "tor")


def pt_binary() -> str:
    """The obfs4 pluggable transport: lyrebird in current tor bundles, obfs4proxy on distros."""
    if IS_WINDOWS:
        return find_binary("lyrebird", bundle_dir() / "tor" / "pluggable_transports")
    found = shutil.which("lyrebird") or shutil.which("obfs4proxy")
    return found or "obfs4proxy"


def short_path(path: str) -> str:
    """On Windows, the 8.3 form of `path`, so it survives torrc's space-separated syntax."""
    if not IS_WINDOWS or " " not in path:
        return path
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))  # type: ignore[attr-defined]
    return buf.value if 0 < n < len(buf) else path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # Never os.kill(pid, 0) on Windows: it calls TerminateProcess.
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except OSError:
            return False
    return os.geteuid() == 0
