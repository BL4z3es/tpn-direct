"""Find tagged VPN servers in the tailnet and pick the healthiest one."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from tpn.tailscale import Peer, TailscaleClient

log = logging.getLogger(__name__)


class NoServerAvailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerCandidate:
    peer: Peer
    latency_ms: float
    health: dict[str, Any]


class ServerSelector:
    def __init__(
        self,
        *,
        tag: str = "tag:vpn-server",
        api_port: int = 8443,
        timeout: float = 10.0,
        preferred: str | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.tag = tag
        self._port = api_port
        self._timeout = timeout
        self._preferred = preferred
        self._transport = transport
        self._clock = clock

    def candidates(self, status: dict[str, Any]) -> list[Peer]:
        peers = [
            p
            for p in TailscaleClient.peers_with_tag(status, self.tag)
            if p.online and p.exit_node_option and p.ips
        ]
        if self._preferred:
            wanted = self._preferred.lower()
            peers = [
                p
                for p in peers
                if wanted in (p.hostname.lower(), p.dns_name.lower(), p.dns_name.split(".")[0])
            ]
        return peers

    def select(self, status: dict[str, Any]) -> ServerCandidate:
        peers = self.candidates(status)
        if not peers:
            which = f" named {self._preferred!r}" if self._preferred else ""
            raise NoServerAvailable(f"no online exit node{which} tagged {self.tag} in the tailnet")
        healthy: list[ServerCandidate] = []
        failures: list[str] = []
        # trust_env=False: tailnet traffic goes over the TUN device, never through tor's proxy.
        with httpx.Client(
            trust_env=False, timeout=self._timeout, transport=self._transport
        ) as client:
            for peer in peers:
                host = f"[{peer.ipv4}]" if ":" in peer.ipv4 else peer.ipv4
                started = self._clock()
                try:
                    response = client.get(f"http://{host}:{self._port}/v1/health")
                    response.raise_for_status()
                    body = response.json()
                except (httpx.HTTPError, ValueError) as e:
                    failures.append(f"{peer.hostname}: {e}")
                    continue
                if body.get("status") != "ok":
                    failures.append(f"{peer.hostname}: reported {body.get('status')!r}")
                    continue
                latency = (self._clock() - started) * 1000
                healthy.append(ServerCandidate(peer, latency, body))
        if not healthy:
            raise NoServerAvailable("no VPN server passed its health check: " + "; ".join(failures))
        best = min(healthy, key=lambda c: c.latency_ms)
        log.info("selected %s (%.0f ms)", best.peer.hostname, best.latency_ms)
        return best
