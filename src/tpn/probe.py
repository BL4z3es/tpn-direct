"""Check whether Tailscale's infrastructure is reachable without tor.

Only meaningful while no exit node is active: once one is, this process's traffic goes through
the VPN server and every probe would pass. tailscaled's own sockets bypass the exit node, so
after connecting, the watchdog relies on tailscaled's health report instead of probing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx

DEFAULT_TARGETS = (
    "https://controlplane.tailscale.com/key?v=71",
    "https://derp1.tailscale.com/derp/probe",
)


@dataclass(frozen=True)
class ProbeResult:
    target: str
    ok: bool
    detail: str


class DirectProbe:
    def __init__(
        self,
        targets: Sequence[str] = DEFAULT_TARGETS,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.targets = tuple(targets)
        self._timeout = timeout
        self._transport = transport

    def run(self) -> list[ProbeResult]:
        results = []
        # trust_env=False: the point is to test the direct path, so ignore any proxy settings.
        with httpx.Client(
            trust_env=False, timeout=self._timeout, transport=self._transport
        ) as client:
            for target in self.targets:
                try:
                    response = client.get(target)
                except httpx.HTTPError as e:
                    results.append(ProbeResult(target, False, f"{type(e).__name__}: {e}"))
                    continue
                # Any non-5xx answer over verified TLS means the host is reachable.
                ok = response.status_code < 500
                results.append(ProbeResult(target, ok, f"HTTP {response.status_code}"))
        return results
