"""Thin wrappers around subprocess so everything above them can be tested with fakes."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

# Every spelling tailscaled (Go's net/http) or a child process might honour.
PROXY_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
)


class CommandNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CmdResult: ...


class SubprocessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CmdResult:
        try:
            proc = subprocess.run(
                list(args),
                capture_output=True,
                # The CLI writes UTF-8; Windows' default code page would garble hostnames.
                encoding="utf-8",
                errors="replace",
                env=dict(env) if env is not None else None,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise CommandNotFound(f"{args[0]}: not found in PATH") from e
        return CmdResult(proc.returncode, proc.stdout, proc.stderr)


def build_env(proxy: str | None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment with any inherited proxy settings removed, plus `proxy` if given.

    Stripping matters as much as setting: a stray HTTPS_PROXY inherited from the shell would
    silently keep tailscaled on a proxy after we decided to go direct.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if k not in PROXY_VARS}
    if proxy:
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[key] = proxy
    return env


def wait_for(
    predicate: Callable[[], bool],
    timeout: float,
    *,
    interval: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll `predicate` until it returns True or `timeout` seconds pass."""
    deadline = clock() + timeout
    while True:
        if predicate():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)
