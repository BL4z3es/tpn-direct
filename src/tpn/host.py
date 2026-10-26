"""The few things that differ between Linux and Windows: paths, binaries, processes, ACLs.

Everything here is pure Python (ctypes on Windows), so it works the same on x86-64 and ARM64.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Read at call time (not cached in callers) so tests can monkeypatch it.
IS_WINDOWS = os.name == "nt"

# Only Administrators can create pipes under ProtectedPrefix\Administrators, so no other user can
# squat on the name before tailscaled does. Tailscale's own service uses the same prefix.
MANAGED_PIPE = r"\\.\pipe\ProtectedPrefix\Administrators\tpn\tailscaled"

# Well-known SIDs: they resolve on every Windows language edition, unlike account names.
_SYSTEM_SID = "*S-1-5-18"
_ADMINISTRATORS_SID = "*S-1-5-32-544"


def default_state_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "tpn"
    return Path("/var/lib/tpn")


def find_binary(name: str) -> str:
    """`name` if it is on PATH, else Tailscale's install directory on Windows, else `name`.

    The Tailscale MSI usually puts itself on PATH, but not in every shell (e.g. one opened
    before the install), so look in Program Files too rather than fail with "not found".
    """
    if shutil.which(name):
        return name
    if IS_WINDOWS:
        # PROGRAMW6432 is the native Program Files even from 32-bit Python on 64-bit Windows.
        for root in (os.environ.get("PROGRAMW6432"), os.environ.get("PROGRAMFILES")):
            if root:
                candidate = Path(root) / "Tailscale" / f"{name}.exe"
                if candidate.is_file():
                    return str(candidate)
    return name


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0) here: on Windows that is TerminateProcess with exit code 0.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Explicit types: the default int return would truncate 64-bit handles on x64 and ARM64.
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone we can't inspect.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def secure_dir(path: Path) -> None:
    """Create `path` readable only by its owner (POSIX) or SYSTEM and Administrators (Windows).

    On Windows, mkdir's mode is ignored and folders under ProgramData inherit read access for
    every local user, which would expose the auth key written there.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not IS_WINDOWS:
        return
    grant = "(OI)(CI)F"
    cmd = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{_SYSTEM_SID}:{grant}",
        f"{_ADMINISTRATORS_SID}:{grant}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        log.warning("icacls not found; %s keeps its inherited permissions", path)
        return
    if result.returncode != 0:
        log.warning("could not restrict %s: %s", path, (result.stderr or result.stdout).strip())
