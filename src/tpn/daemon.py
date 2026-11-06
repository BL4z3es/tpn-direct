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
    """

    def __init__(
        self,
        ts: TailscaleClient,
        *,
        state_dir: Path,
        socket: Path,
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
    """Read and write a Windows service's `Environment` value (REG_MULTI_SZ)."""

    def get(self, service: str) -> list[str]: ...
    def set(self, service: str, values: list[str]) -> None: ...


class RegistryServiceEnvironment:
    """HKLM\\SYSTEM\\CurrentControlSet\\Services\\<name>\\Environment, read at service start."""

    def _key(self, service: str, access: int) -> Any:
        import winreg  # Windows only

        path = rf"SYSTEM\CurrentControlSet\Services\{service}"
        return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, access)

    def get(self, service: str) -> list[str]:
        import winreg

        try:
            with self._key(service, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, "Environment")
        except FileNotFoundError:
            return []
        return list(value) if kind == winreg.REG_MULTI_SZ else []

    def set(self, service: str, values: list[str]) -> None:
        import winreg

        try:
            with self._key(service, winreg.KEY_SET_VALUE) as key:
                if values:
                    winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, values)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        winreg.DeleteValue(key, "Environment")
        except FileNotFoundError as e:
            raise DaemonError(f"service {service!r} not found: is Tailscale installed?") from e
        except PermissionError as e:
            raise DaemonError("changing the Tailscale service needs an elevated prompt") from e


class WindowsServiceTailscaled:
    """The Tailscale Windows service, pointed at tor by its registry Environment value.

    Go's net/http (and so tailscaled) reads HTTPS_PROXY from the environment before falling
    back to the system's WinHTTP settings, so this steers only tailscaled, not the whole machine.
    """

    def __init__(
        self,
        ts: TailscaleClient,
        runner: Runner,
        *,
        service: str = "Tailscale",
        env: ServiceEnvironment | None = None,
        ready_timeout: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ts = ts
        self._runner = runner
        self._service = service
        self._env = env or RegistryServiceEnvironment()
        self._ready_timeout = ready_timeout
        self._clock = clock
        self._sleep = sleep

    def _wanted(self, proxy: str | None) -> list[str]:
        keep = [v for v in self._env.get(self._service) if v.split("=", 1)[0] not in PROXY_VARS]
        if proxy:
            keep += [f"HTTPS_PROXY={proxy}", f"HTTP_PROXY={proxy}"]
        return keep

    def apply(self, proxy: str | None) -> None:
        wanted = self._wanted(proxy)
        changed = wanted != self._env.get(self._service)
        if changed:
            self._env.set(self._service, wanted)
        if changed or not _responds(self._ts)():
            self._restart()
        ready = wait_for(
            _responds(self._ts),
            self._ready_timeout,
            interval=0.5,
            clock=self._clock,
            sleep=self._sleep,
        )
        if not ready:
            raise DaemonError(f"the {self._service} service did not come back after restart")

    def _restart(self) -> None:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Restart-Service -Name '{self._service}' -Force -ErrorAction Stop",
        ]
        result = self._runner.run(cmd, timeout=90)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DaemonError(f"restarting the {self._service} service failed: {detail}")

    def shutdown(self) -> None:
        # Leave the service running for the user, but never pointed at a tor that is stopping.
        if self._wanted(None) != self._env.get(self._service):
            self.apply(None)
