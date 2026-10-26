"""Run a private tor instance that exposes an HTTP CONNECT proxy for tailscaled.

tailscaled speaks HTTP proxies (via HTTPS_PROXY), not SOCKS, so we use tor's HTTPTunnelPort.
Bootstrap progress is read from tor's own log on stdout, which avoids a control-port dependency.
"""

from __future__ import annotations

import collections
import logging
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_BOOTSTRAP_RE = re.compile(r"Bootstrapped (\d+)%")
_EOF = object()


class TorError(RuntimeError):
    pass


@dataclass
class TorConfig:
    data_dir: Path
    http_tunnel_port: int = 9080
    bridges: list[str] = field(default_factory=list)
    obfs4proxy: str = "obfs4proxy"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.http_tunnel_port}"

    def render(self) -> str:
        lines = [
            f"DataDirectory {self.data_dir}",
            "SocksPort 0",
            f"HTTPTunnelPort 127.0.0.1:{self.http_tunnel_port}",
            "Log notice stdout",
            "ClientOnly 1",
            "AvoidDiskWrites 1",
        ]
        if self.bridges:
            lines += ["UseBridges 1", f"ClientTransportPlugin obfs4 exec {self.obfs4proxy}"]
            for bridge in self.bridges:
                # A newline would let a bridge string inject arbitrary torrc directives.
                if "\n" in bridge or "\r" in bridge:
                    raise TorError(f"invalid bridge line: {bridge!r}")
                lines.append(f"Bridge {bridge.strip()}")
        return "\n".join(lines) + "\n"


class TorManager:
    def __init__(
        self,
        config: TorConfig,
        *,
        tor_binary: str = "tor",
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._binary = tor_binary
        self._popen = popen
        self._clock = clock
        self._proc: Any = None
        self._lines: queue.Queue[object] = queue.Queue()
        self._tail: collections.deque[str] = collections.deque(maxlen=20)

    @property
    def proxy_url(self) -> str:
        return self.config.proxy_url

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout: float = 180.0) -> None:
        """Start tor and block until it reports 100% bootstrap."""
        if self.is_running():
            return
        self.config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        torrc = self.config.data_dir.parent / "torrc"
        torrc.write_text(self.config.render())

        log.info("starting tor (HTTP tunnel on %s)", self.proxy_url)
        try:
            self._proc = self._popen(
                [self._binary, "-f", str(torrc)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise TorError(f"{self._binary}: not found in PATH") from e

        self._lines = queue.Queue()
        # The reader keeps draining stdout for tor's whole life so its pipe never fills up.
        threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()

        deadline = self._clock() + timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                self.stop()
                raise TorError(f"tor did not bootstrap within {timeout:.0f}s{self._tail_text()}")
            try:
                item = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if item is _EOF:
                self._proc.wait()
                raise TorError(f"tor exited during bootstrap{self._tail_text()}")
            match = _BOOTSTRAP_RE.search(str(item))
            if match:
                log.info("tor bootstrap %s%%", match.group(1))
                if match.group(1) == "100":
                    return

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            log.info("stopping tor")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None

    def _pump(self, proc: Any) -> None:
        for line in proc.stdout:
            line = line.rstrip("\n")
            self._tail.append(line)
            self._lines.put(line)
        self._lines.put(_EOF)

    def _tail_text(self) -> str:
        return ("; last log lines:\n  " + "\n  ".join(self._tail)) if self._tail else ""
