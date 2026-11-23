"""File permissions for the daemon's secrets, and log redaction.

The control token and the auth key are credentials: POSIX modes and Windows ACLs are part of
the protocol (docs/control-api.md), not cosmetic hardening. On Windows the token must stay
readable by BUILTIN\\Users so an unelevated tray app can drive the VPN; everything else in
the state dir stays SYSTEM/Administrators only. The runner is injectable so the icacls calls
can be tested anywhere.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from tpn import host
from tpn.control import write_token
from tpn.proc import Runner, SubprocessRunner

# Well-known SIDs: SYSTEM, Administrators, Users. '*S-...' avoids language-dependent names.
_SYSTEM = "*S-1-5-18:F"
_ADMINS = "*S-1-5-32-544:F"
_USERS_READ = "*S-1-5-32-545:R"

_KEY_RE = re.compile(r"tskey-\S+")


class FileSecurity:
    """Applies the contract's state-directory and secret-file permissions."""

    def __init__(self, runner: Runner | None = None, *, is_windows: bool | None = None) -> None:
        self._runner = runner or SubprocessRunner()
        self._windows = host.IS_WINDOWS if is_windows is None else is_windows

    def state_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._windows:
            # Inheritance is removed before any credentials are written, so files created
            # later inherit SYSTEM+Administrators only; only the token is widened to Users.
            self._restrict(path, (_SYSTEM, _ADMINS))
        else:
            path.chmod(0o700)

    def token(self, path: Path, token: str) -> None:
        write_token(path, token)
        self._secret(path, users_read=True)

    def private_file(self, path: Path, contents: str) -> None:
        """Atomically replace a secret, resetting its permissions on every write."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(contents)
        os.replace(tmp, path)
        self._secret(path)

    def delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def redact(self, text: str) -> str:
        """Replace any auth key that leaked into a message with a placeholder."""
        return _KEY_RE.sub("[redacted]", text or "")

    def _secret(self, path: Path, *, users_read: bool = False) -> None:
        if self._windows:
            grants = (_SYSTEM, _ADMINS, _USERS_READ) if users_read else (_SYSTEM, _ADMINS)
            self._restrict(path, grants)
        else:
            # chmod after replace also fixes a pre-existing file with looser modes.
            path.chmod(0o600)

    def _restrict(self, path: Path, grants: tuple[str, ...]) -> None:
        result = self._runner.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", *grants], timeout=30
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PermissionError(f"could not restrict {path}: {detail}")
