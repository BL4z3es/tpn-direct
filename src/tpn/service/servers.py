"""List and health-check tagged VPN servers from the tailnet.

The daemon uses this to answer ``GET /servers``. It re-uses the same candidate selection
logic as ``tpn.discovery.ServerSelector`` and adds per-peer health checks against the
server's own ``/v1/health`` endpoint.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from tpn.tailscale import TailscaleClient

log = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    tag: str = "tag:vpn-server"
    api_port: int = 8443
    timeout: float = 10.0


class ServerInventory:
    """Query tailscale for peers and check their health endpoints."""

    def __init__(
        self,
        *,
        tag: str = "tag:vpn-server",
        api_port: int = 8443,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.perf_counter,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = ServerConfig(tag=tag, api_port=api_port, timeout=timeout)
        self._clock = clock
        self._transport = transport

    def list(
        self, tailscale: TailscaleClient, selected_ip: str | None = None
    ) -> list[dict[str, Any]]:
        status = tailscale.status()
        peers = [
            p
            for p in TailscaleClient.peers_with_tag(status, self.config.tag)
            if p.exit_node_option and p.ips
        ]
        servers: list[dict[str, Any]] = []
        for peer in peers:
            healthy: bool | None = None
            latency: float | None = None
            detail = ""
            if peer.online:
                host = f"[{peer.ipv4}]" if ":" in peer.ipv4 else peer.ipv4
                try:
                    with httpx.Client(
                        trust_env=False,
                        timeout=self.config.timeout,
                        transport=self._transport,
                    ) as client:
                        start = self._clock()
                        response = client.get(f"http://{host}:{self.config.api_port}/v1/health")
                        response.raise_for_status()
                        body = response.json()
                        latency = (self._clock() - start) * 1000
                        if body.get("status") == "ok":
                            healthy = True
                        else:
                            healthy = False
                            detail = f"reported {body.get('status')!r}"
                except (httpx.HTTPError, ValueError) as e:
                    healthy = False
                    detail = str(e)
            selected = peer.ipv4 == selected_ip
            servers.append(
                {
                    "hostname": peer.hostname,
                    "dns_name": peer.dns_name,
                    "ipv4": peer.ipv4,
                    "online": peer.online,
                    "healthy": healthy,
                    "latency_ms": latency,
                    "detail": detail,
                    "selected": selected,
                }
            )
        return servers
