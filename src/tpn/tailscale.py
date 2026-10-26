"""Wrapper around the `tailscale` CLI's JSON output.

We go through the CLI rather than the LocalAPI socket directly: the CLI is the stable,
documented interface, and its --json output is what the LocalAPI returns anyway.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tpn.proc import Runner

# Substrings of tailscaled health warnings that mean "can't reach the coordination server".
# These are human-readable strings, not a stable API: confirm them during the M0 runbook.
CONTROL_HEALTH_MARKERS = ("coordination server", "map poll", "not connected to control")


class TailscaleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Peer:
    hostname: str
    dns_name: str
    ips: tuple[str, ...]
    online: bool
    tags: tuple[str, ...]
    exit_node_option: bool

    @property
    def ipv4(self) -> str:
        return next((ip for ip in self.ips if ":" not in ip), self.ips[0])

    @classmethod
    def from_status(cls, raw: dict[str, Any]) -> Peer:
        return cls(
            hostname=raw.get("HostName", ""),
            dns_name=raw.get("DNSName", "").rstrip("."),
            ips=tuple(raw.get("TailscaleIPs") or ()),
            online=bool(raw.get("Online")),
            tags=tuple(raw.get("Tags") or ()),
            exit_node_option=bool(raw.get("ExitNodeOption")),
        )


@dataclass(frozen=True)
class Identity:
    node_name: str
    tags: tuple[str, ...]
    login_name: str


def control_healthy(status: dict[str, Any]) -> bool:
    """True when tailscaled is running and not reporting a lost coordination server."""
    if status.get("BackendState") != "Running":
        return False
    for message in status.get("Health") or ():
        lowered = message.lower()
        if any(marker in lowered for marker in CONTROL_HEALTH_MARKERS):
            return False
    return True


class TailscaleClient:
    def __init__(
        self, runner: Runner, *, binary: str = "tailscale", socket: Path | None = None
    ) -> None:
        self._runner = runner
        self._binary = binary
        self._socket = socket

    def _run(self, *args: str, timeout: float = 30.0) -> str:
        cmd = [self._binary]
        if self._socket is not None:
            cmd.append(f"--socket={self._socket}")
        cmd.extend(args)
        result = self._runner.run(cmd, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise TailscaleError(f"tailscale {args[0]} failed ({result.returncode}): {detail}")
        return result.stdout

    def status(self) -> dict[str, Any]:
        try:
            return json.loads(self._run("status", "--json"))
        except json.JSONDecodeError as e:
            raise TailscaleError(f"tailscale status returned invalid JSON: {e}") from e

    def up(
        self,
        *,
        auth_key_file: Path | None = None,
        hostname: str | None = None,
        tags: Sequence[str] = (),
        advertise_exit_node: bool = False,
        timeout: float = 120.0,
    ) -> None:
        # --reset makes the call idempotent and clears any exit node left over from a previous
        # session, so nothing (including our direct-path probe) is routed through it too early.
        args = ["up", "--reset", f"--timeout={int(timeout)}s"]
        if auth_key_file is not None:
            # file: keeps the key out of the process list.
            args.append(f"--auth-key=file:{auth_key_file}")
        if hostname:
            args.append(f"--hostname={hostname}")
        if tags:
            args.append(f"--advertise-tags={','.join(tags)}")
        if advertise_exit_node:
            args.append("--advertise-exit-node")
        self._run(*args, timeout=timeout + 15)

    def set_exit_node(self, ip: str | None, *, allow_lan: bool = True) -> None:
        lan = "true" if ip and allow_lan else "false"
        self._run("set", f"--exit-node={ip or ''}", f"--exit-node-allow-lan-access={lan}")

    def whois(self, ip: str) -> Identity:
        try:
            raw = json.loads(self._run("whois", "--json", ip, timeout=10))
        except json.JSONDecodeError as e:
            raise TailscaleError(f"tailscale whois returned invalid JSON: {e}") from e
        node = raw.get("Node") or {}
        profile = raw.get("UserProfile") or {}
        return Identity(
            node_name=(node.get("ComputedName") or node.get("Name") or "").rstrip("."),
            tags=tuple(node.get("Tags") or ()),
            login_name=profile.get("LoginName", ""),
        )

    @staticmethod
    def peers(status: dict[str, Any]) -> list[Peer]:
        return [Peer.from_status(raw) for raw in (status.get("Peer") or {}).values()]

    @classmethod
    def peers_with_tag(cls, status: dict[str, Any], tag: str) -> list[Peer]:
        return [p for p in cls.peers(status) if tag in p.tags]

    @staticmethod
    def self_ips(status: dict[str, Any]) -> list[str]:
        return list(
            (status.get("Self") or {}).get("TailscaleIPs") or status.get("TailscaleIPs") or ()
        )
