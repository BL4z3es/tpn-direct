"""Control-plane API that runs on a `tag:vpn-server` node, bound to its tailnet IP.

Who may call it is decided by the tailnet policy. Each request is still checked with
`tailscale whois`, so a mis-bound socket never serves anyone outside the tailnet.
"""

# No `from __future__ import annotations`: FastAPI must resolve the local `Peer` alias at runtime.
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

from tpn import __version__
from tpn.tailscale import Identity, TailscaleClient, TailscaleError


class WhoisCache:
    def __init__(
        self, ts: TailscaleClient, ttl: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ts = ts
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, tuple[Identity, float]] = {}
        self._lock = threading.Lock()

    def get(self, ip: str) -> Identity:
        now = self._clock()
        with self._lock:
            hit = self._entries.get(ip)
            if hit and hit[1] > now:
                return hit[0]
        identity = self._ts.whois(ip)
        with self._lock:
            self._entries[ip] = (identity, now + self._ttl)
        return identity


def lookup_egress_ip() -> str | None:
    try:
        return httpx.get("https://api.ipify.org", timeout=5).text.strip() or None
    except httpx.HTTPError:
        return None


class _Cached:
    def __init__(self, fn: Callable[[], Any], ttl: float, clock: Callable[[], float]) -> None:
        self._fn, self._ttl, self._clock = fn, ttl, clock
        self._value: Any = None
        self._expires = 0.0

    def __call__(self) -> Any:
        if self._clock() >= self._expires:
            self._value = self._fn()
            self._expires = self._clock() + self._ttl
        return self._value


def create_app(
    ts: TailscaleClient,
    *,
    whois_ttl: float = 60.0,
    egress_ip: Callable[[], str | None] = lookup_egress_ip,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    whois = WhoisCache(ts, whois_ttl, clock)
    cached_egress = _Cached(egress_ip, 300.0, clock)

    def require_peer(request: Request) -> Identity:
        host = request.client.host if request.client else None
        if not host:
            raise HTTPException(status_code=403, detail="unknown caller")
        try:
            return whois.get(host)
        except TailscaleError:
            raise HTTPException(status_code=403, detail="caller is not a tailnet peer") from None

    Peer = Annotated[Identity, Depends(require_peer)]
    app = FastAPI(title="tpn-server", version=__version__, dependencies=[Depends(require_peer)])

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        try:
            status = ts.status()
        except TailscaleError as e:
            return {"status": "degraded", "detail": str(e), "version": __version__}
        running = status.get("BackendState") == "Running"
        return {"status": "ok" if running else "degraded", "version": __version__}

    @app.get("/v1/info")
    def info() -> dict[str, Any]:
        status = ts.status()
        me = status.get("Self") or {}
        return {
            "hostname": me.get("HostName"),
            "tailscale_ips": TailscaleClient.self_ips(status),
            "egress_ip": cached_egress(),
            "load": os.getloadavg(),
            "version": __version__,
        }

    @app.get("/v1/whoami")
    def whoami(caller: Peer) -> dict[str, Any]:
        return asdict(caller)

    return app
