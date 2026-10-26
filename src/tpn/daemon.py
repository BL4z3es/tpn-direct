"""Control how tailscaled reaches the outside world: through tor's proxy, or directly.

tailscaled reads HTTPS_PROXY only at startup, so switching transport means restarting it. The
node key lives in the state directory, so a restart does not need a new login.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from tpn.proc import PROXY_VARS, Runner, build_env, wait_for
from tpn.tailscale import TailscaleClient, TailscaleError

log = logging.getLogger(__name__)


class DaemonError(RuntimeError):
    pass


class DaemonController(Protocol):
    def apply(self, proxy: str | None) -> None:
        """Make sure tailscaled is running, using `proxy` (or no proxy when None)."""

    def shutdown(self) -> None:
        """Undo what apply() did."""


def _responds(ts: TailscaleClient) -> Callable[[], bool]:
    def check() -> bool:
        try:
            ts.status()
        except TailscaleError:
            return False
        return True

    return check


class ManagedTailscaled:
    """A tailscaled owned by tpn, with its own state dir, socket and TUN device.

    Using a separate TUN name and socket means it does not fight with a system tailscaled.
    On Windows the socket is a named pipe and the TUN a Wintun adapter; there the Tailscale
    service should be stopped first, since both daemons manage the same firewall rules.
    """

    def __init__(
        self,
        ts: TailscaleClient,
        *,
        state_dir: Path,
        socket: str | Path,
        binary: str = "tailscaled",
        tun_name: str = "tpn0",
        popen: Callable[..., Any] = subprocess.Popen,
        ready_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ts = ts
        self._state_dir = state_dir
        self._socket = socket
        self._binary = binary
        self._tun = tun_name
        self._popen = popen
        self._ready_timeout = ready_timeout
        self._clock = clock
        self._sleep = sleep
        self._proc: Any = None
        self._proxy: str | None = None

    def apply(self, proxy: str | None) -> None:
        if self._proc is not None and self._proc.poll() is None:
            if self._proxy == proxy:
                return
            self.shutdown()
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log.info("starting tailscaled (%s)", f"proxy {proxy}" if proxy else "direct")
        logfile = (self._state_dir / "tailscaled.log").open("ab")
        try:
            self._proc = self._popen(
                [
                    self._binary,
                    f"--statedir={self._state_dir}",
                    f"--socket={self._socket}",
                    f"--tun={self._tun}",
                    "--port=0",
                ],
                env=build_env(proxy),
                stdout=logfile,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            raise DaemonError(f"{self._binary}: not found in PATH") from e
        finally:
            logfile.close()
        self._proxy = proxy
        ready = wait_for(
            _responds(self._ts),
            self._ready_timeout,
            interval=0.5,
            clock=self._clock,
            sleep=self._sleep,
        )
        if not ready:
            raise DaemonError(f"tailscaled did not answer on {self._socket}")

    def shutdown(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
        self._proxy = None


class SystemdTailscaled:
    """The distro's tailscaled.service, steered with a systemd drop-in that sets the proxy."""

    def __init__(
        self,
        ts: TailscaleClient,
        runner: Runner,
        *,
        dropin: Path,
        unit: str = "tailscaled.service",
        ready_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ts = ts
        self._runner = runner
        self._dropin = dropin
        self._unit = unit
        self._ready_timeout = ready_timeout
        self._clock = clock
        self._sleep = sleep

    @staticmethod
    def render_dropin(proxy: str) -> str:
        return (
            "# Managed by tpn. Removed automatically when tpn goes direct.\n"
            "[Service]\n"
            f'Environment="HTTPS_PROXY={proxy}" "HTTP_PROXY={proxy}"\n'
        )

    def apply(self, proxy: str | None) -> None:
        if proxy:
            self._dropin.parent.mkdir(parents=True, exist_ok=True)
            self._dropin.write_text(self.render_dropin(proxy))
        else:
            self._dropin.unlink(missing_ok=True)
        for cmd in (["systemctl", "daemon-reload"], ["systemctl", "restart", self._unit]):
            result = self._runner.run(cmd, timeout=60)
            if result.returncode != 0:
                raise DaemonError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
        ready = wait_for(
            _responds(self._ts),
            self._ready_timeout,
            interval=0.5,
            clock=self._clock,
            sleep=self._sleep,
        )
        if not ready:
            raise DaemonError(f"{self._unit} did not come back after restart")

    def shutdown(self) -> None:
        # Leave the system service running, but never pointed at a tor that is about to stop.
        if self._dropin.exists():
            self.apply(None)


class ServiceEnvironment(Protocol):
    """The environment block Windows passes to a service: its REG_MULTI_SZ `Environment` value."""

    def read(self) -> list[str]: ...
    def write(self, entries: list[str]) -> None:
        """Replace the block; an empty list removes the value."""


class RegistryEnvironment:
    """The service key's `Environment` value under HKEY_LOCAL_MACHINE, via winreg (Windows only)."""

    def __init__(self, service: str) -> None:
        self._key = "SYSTEM\\CurrentControlSet\\Services\\" + service

    def read(self) -> list[str]:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self._key) as key:
                value, kind = winreg.QueryValueEx(key, "Environment")
        except FileNotFoundError:
            return []
        return list(value) if kind == winreg.REG_MULTI_SZ else []

    def write(self, entries: list[str]) -> None:
        import winreg

        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self._key, 0, winreg.KEY_SET_VALUE)
        except FileNotFoundError as e:
            raise DaemonError(f"Windows service key {self._key} not found") from e
        with key:
            if entries:
                winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, entries)
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, "Environment")


class WindowsServiceTailscaled:
    """The Tailscale Windows service, steered through its environment block in the registry.

    The Windows counterpart of SystemdTailscaled: the Service Control Manager hands the
    `Environment` value to the service process, which passes it on to its child tailscaled.
    Entries that tpn did not write are kept.
    """

    def __init__(
        self,
        ts: TailscaleClient,
        runner: Runner,
        *,
        environment: ServiceEnvironment,
        service: str = "Tailscale",
        ready_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ts = ts
        self._runner = runner
        self._env = environment
        self._service = service
        self._ready_timeout = ready_timeout
        self._clock = clock
        self._sleep = sleep

    @staticmethod
    def _is_proxy(entry: str) -> bool:
        return entry.split("=", 1)[0] in PROXY_VARS

    def apply(self, proxy: str | None) -> None:
        entries = [e for e in self._env.read() if not self._is_proxy(e)]
        if proxy:
            entries += [f"HTTPS_PROXY={proxy}", f"HTTP_PROXY={proxy}"]
        self._env.write(entries)
        # Restart-Service waits for the stop and the start, and starts a stopped service.
        # sc.exe would return before either has happened.
        name = self._service.replace("'", "''")
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Restart-Service -Name '{name}' -Force -ErrorAction Stop",
        ]
        result = self._runner.run(cmd, timeout=90)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DaemonError(f"restarting the {self._service} service failed: {detail}")
        ready = wait_for(
            _responds(self._ts),
            self._ready_timeout,
            interval=0.5,
            clock=self._clock,
            sleep=self._sleep,
        )
        if not ready:
            raise DaemonError(f"the {self._service} service did not come back after restart")

    def shutdown(self) -> None:
        # Same rule as systemd: leave the service running, never pointed at a stopped tor.
        if any(self._is_proxy(e) for e in self._env.read()):
            self.apply(None)
