"""Windows service host and service-registration helpers for tpn.

pywin32 is imported lazily so this module remains importable on other platforms. The SCM entry
point is ``tpn.exe service-host``; installation is deliberately kept in ``sc.exe`` commands so
registration also works in a frozen executable and is straightforward to test.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
from typing import Any

from tpn.proc import Runner, SubprocessRunner

SERVICE_NAME = "tpn"
SERVICE_DISPLAY_NAME = "tpn-direct VPN"
SERVICE_DESCRIPTION = (
    "Connects this device to a Tailscale tailnet through Tor and routes traffic through a "
    "tagged exit node. Owns Tor and configures the Tailscale Windows service."
)
STOP_WAIT_HINT_MS = 90_000

try:  # pywin32 is only a dependency on Windows, but tests may provide stubs on any OS.
    import win32serviceutil
except ImportError:  # pragma: no cover - the normal path on non-Windows hosts
    win32serviceutil = None  # type: ignore[assignment]

_ServiceFramework = win32serviceutil.ServiceFramework if win32serviceutil is not None else object
log = logging.getLogger(__name__)


def _win32service() -> Any:
    try:
        import win32service
    except ImportError as exc:  # pragma: no cover - exercised by real Windows installations
        raise RuntimeError("The tpn Windows service requires pywin32") from exc
    return win32service


def _log_event(message: str, *, error: bool = False) -> None:
    """Write to the Windows event log, retaining a logging fallback for test/development hosts."""
    try:
        import servicemanager

        if error:
            servicemanager.LogErrorMsg(message)
        else:
            servicemanager.LogInfoMsg(message)
    except Exception:
        # Event-log setup is best effort; a logging failure must not prevent SCM stop handling.
        if error:
            log.exception("Could not write the tpn error to the Windows event log: %s", message)
        else:
            log.info("%s", message)


class TpnService(_ServiceFramework):
    """Owns the daemon's lifetime on behalf of the Windows Service Control Manager."""

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args: Any) -> None:
        super().__init__(args)
        self.stop_event = threading.Event()
        self._failure: Exception | None = None

    def SvcRun(self) -> None:
        # The real pywin32 dispatcher catches an exception from SvcRun and reports a failed stop.
        # Keep SvcDoRun itself easy to exercise directly while preserving that native behavior.
        self.SvcDoRun()
        if self._failure is not None:
            raise self._failure

    def SvcStop(self) -> None:
        # Teardown can restart Tailscale and take about a minute. Ask the SCM not to time out
        # after its usual 30 seconds, then let the daemon perform its normal clean disconnect.
        self.ReportServiceStatus(
            _win32service().SERVICE_STOP_PENDING,
            waitHint=STOP_WAIT_HINT_MS,
        )
        self.stop_event.set()
        _log_event("tpn service stop requested; disconnecting the VPN")

    def SvcDoRun(self) -> None:
        # Import here: pywin32 loads this module while constructing its service host, and the
        # daemon's own imports/logging setup must happen only after the SCM has dispatched us.
        from tpn.config import Settings
        from tpn.service import run

        win32service = _win32service()
        _log_event("tpn service starting")
        try:
            self.ReportServiceStatus(
                win32service.SERVICE_RUNNING,
                waitHint=STOP_WAIT_HINT_MS,
            )
            _log_event("tpn service running")
            run(Settings(), self.stop_event)
        except Exception as exc:
            self._failure = exc
            self.stop_event.set()
            _log_event(
                "tpn service failed; the daemon has stopped:\n" + traceback.format_exc(),
                error=True,
            )
            # SvcRun re-raises this for pywin32's native dispatcher. The direct call remains
            # bounded and observable, which is useful for tests and diagnostic callers.
            self.ReportServiceStatus(
                win32service.SERVICE_STOPPED,
                win32ExitCode=1,
                svcExitCode=1,
            )
        finally:
            self.stop_event.set()

        _log_event("tpn service stopped")


def dispatch() -> None:
    """Dispatch the single tpn service when the SCM starts ``tpn.exe service-host``."""
    if win32serviceutil is None:
        raise RuntimeError("The tpn Windows service requires pywin32")
    try:
        import servicemanager
    except ImportError as exc:  # pragma: no cover - depends on the host's pywin32 installation
        raise RuntimeError("The tpn Windows service requires pywin32") from exc

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(TpnService)
    servicemanager.StartServiceCtrlDispatcher()


def _sc(runner: Runner, args: list[str], *, ignore_codes: tuple[int, ...] = ()) -> None:
    result = runner.run(["sc.exe", *args], timeout=120)
    if result.returncode not in (0, *ignore_codes):
        detail = result.stderr.strip() or result.stdout.strip() or "unknown sc.exe error"
        raise RuntimeError(f"sc.exe {' '.join(args[:2])} failed ({result.returncode}): {detail}")


def install(exe_path: str | os.PathLike[str], *, runner: Runner | None = None) -> None:
    """Register tpn as a delayed-auto-start LocalSystem service using an absolute exe path."""
    path = os.fspath(exe_path)
    if not path or any(char in path for char in ('"', "\r", "\n")):
        raise ValueError("exe_path must be a non-empty path without quotes or newlines")
    commands = runner or SubprocessRunner()
    # subprocess passes each list item as one argv value. The embedded quotes therefore survive
    # in binPath, which is essential for paths such as C:\Program Files\tpn\tpn.exe.
    binary = f'"{path}" service-host'
    _sc(
        commands,
        [
            "create",
            SERVICE_NAME,
            "binPath=",
            binary,
            "start=",
            "delayed-auto",
            "depend=",
            "Tailscale",
            "DisplayName=",
            SERVICE_DISPLAY_NAME,
            "obj=",
            "LocalSystem",
        ],
        ignore_codes=(1073,),
    )
    # Re-running service-install is a supported upgrade/repair operation.
    _sc(
        commands,
        [
            "config",
            SERVICE_NAME,
            "binPath=",
            binary,
            "start=",
            "delayed-auto",
            "depend=",
            "Tailscale",
            "DisplayName=",
            SERVICE_DISPLAY_NAME,
            "obj=",
            "LocalSystem",
        ],
    )
    _sc(commands, ["description", SERVICE_NAME, SERVICE_DESCRIPTION])
    _sc(
        commands,
        [
            "failure",
            SERVICE_NAME,
            "reset=",
            "86400",
            "actions=",
            "restart/10000/restart/30000/restart/60000",
        ],
    )


def uninstall(*, runner: Runner | None = None) -> None:
    """Stop and remove the tpn service; missing/already-stopped services are harmless."""
    commands = runner or SubprocessRunner()
    _sc(commands, ["stop", SERVICE_NAME], ignore_codes=(1060, 1062))
    _sc(commands, ["delete", SERVICE_NAME], ignore_codes=(1060,))


__all__ = [
    "SERVICE_DESCRIPTION",
    "SERVICE_DISPLAY_NAME",
    "SERVICE_NAME",
    "STOP_WAIT_HINT_MS",
    "TpnService",
    "dispatch",
    "install",
    "uninstall",
]
