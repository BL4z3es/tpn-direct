from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from tpn.proc import CmdResult


class FakeRunner:
    """Answers commands from a handler; records every call."""

    def __init__(self, handler: Callable[[list[str]], CmdResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.handler = handler or (lambda args: CmdResult(0, "", ""))

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CmdResult:
        self.calls.append(list(args))
        self.envs.append(env)
        return self.handler(list(args))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def peer(
    hostname: str,
    ip: str,
    *,
    tags: Sequence[str] = ("tag:vpn-server",),
    online: bool = True,
    exit_node: bool = True,
) -> dict[str, Any]:
    return {
        "HostName": hostname,
        "DNSName": f"{hostname}.tail1234.ts.net.",
        "TailscaleIPs": [ip, "fd7a:115c:a1e0::1"],
        "Online": online,
        "Tags": list(tags),
        "ExitNodeOption": exit_node,
    }


def status(
    *,
    backend: str = "Running",
    health: Sequence[str] = (),
    peers: Sequence[dict[str, Any]] = (),
    exit_node_online: bool | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "BackendState": backend,
        "Health": list(health),
        "Self": {"HostName": "me", "TailscaleIPs": ["100.64.0.2"], "Tags": ["tag:vpn-client"]},
        "Peer": {f"nodekey:{i}": p for i, p in enumerate(peers)},
    }
    if exit_node_online is not None:
        out["ExitNodeStatus"] = {"Online": exit_node_online, "TailscaleIPs": ["100.64.0.10/32"]}
    return out


def ok_json(payload: Any) -> CmdResult:
    return CmdResult(0, json.dumps(payload), "")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
