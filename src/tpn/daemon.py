"""Control how tailscaled reaches the outside world: through tor's proxy, or directly.

tailscaled reads HTTPS_PROXY only at startup, so switching transport means restarting it. The
node key lives in the state directory, so a restart does not need a new login.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from tpn.proc import Runner, build_env, wait_for
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
