"""The local control API between the tpn daemon and its clients (`tpn up/down`, the Windows app).

The daemon (tpn/service/) listens on http://127.0.0.1:<TPN_CONTROL_PORT> (default 9081) and
owns tailscaled and tor. Clients never touch them directly. The full protocol, with every
request and response, is in docs/control-api.md; this module is its single source of truth:
the shapes, the client, and the request checks the server must apply.

Who may call it: whoever can read the token file (state_dir/control.token). The daemon
rewrites the token at every start. On Windows the file is readable by the machine's users
(BUILTIN\\Users), so an unelevated tray app can drive the VPN, as Tailscale's own UI can; on
Linux it is root-only. The Host and Origin checks below keep a web page in a local browser
from reaching the API through DNS rebinding or a cross-site request, token or not.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx

DEFAULT_PORT = 9081
API = "/v1"

PhaseName = Literal[
    "init",
    "tor_starting",
    "tor_ready",
    "tailnet_up",
    "probing",
    "going_direct",
    "connected",
    "failed",
    "disconnected",
]
# Phases during which a connection attempt is in progress (not yet connected, not idle).
CONNECTING_PHASES: frozenset[str] = frozenset(
    {"init", "tor_starting", "tor_ready", "tailnet_up", "probing", "going_direct"}
)


class DaemonInfo(TypedDict):
    version: str
    pid: int
    started_at: float  # unix time


class Status(TypedDict):
    daemon: DaemonInfo
    phase: PhaseName
    transport: Literal["tor", "direct"] | None
    server: str | None
    server_ip: str | None
    detail: str
    connected_since: float | None  # unix time phase became "connected", else None
    retry_at: float | None  # unix time of the next automatic retry after "failed", else None
    has_auth_key: bool  # an auth key is stored for the next login
    logged_in: bool | None  # tailscaled has a node key (not NeedsLogin); None if unknown


class ServerInfo(TypedDict):
    hostname: str
    dns_name: str
    ipv4: str
    online: bool
    healthy: bool | None  # None: not health-checked (offline, or the check was skipped)
    latency_ms: float | None
    detail: str  # why unhealthy, or ""
    selected: bool  # the current exit node


class PrefsDict(TypedDict):
    """tpn.prefs.Prefs as JSON."""

    use_tor: bool
    keep_tor: bool
    server: str | None
    hostname: str | None
    tor_bridges: list[str]
    auto_connect: bool


# -- errors ----------------------------------------------------------------------------------


class ControlError(RuntimeError):
    """The daemon answered with an error. `status` is the HTTP status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DaemonNotRunning(ControlError):
    """Nothing is listening, or the token file is missing: the daemon is not running."""

    def __init__(self, message: str = "the tpn daemon is not running") -> None:
        super().__init__(0, message)


class Unauthorized(ControlError):
    """The token was refused (stale token file, or the caller may not read it)."""


# -- token -----------------------------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_token(path: Path, token: str) -> None:
    """Write `token` to `path`, mode 0600. The daemon then widens it on Windows (see above)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(tmp, path)


def read_token(path: Path) -> str | None:
    """The token, or None when the file is missing. PermissionError propagates."""
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        return None
    return token or None


# -- server-side checks ----------------------------------------------------------------------


def check_request(headers: Mapping[str, str], *, token: str, port: int) -> tuple[int, str] | None:
    """Return (status, message) to refuse a request, or None to let it through.

    The server must call this on every request before routing it. `headers` is looked up
    case-insensitively by the caller's mapping (http.server's message object is).
    """
    host = (headers.get("Host") or "").strip().lower()
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
        return 403, "bad Host header"
    if headers.get("Origin") is not None:
        return 403, "browser requests are not allowed"
    auth = headers.get("Authorization") or ""
    scheme, _, supplied = auth.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), token):
        return 401, "bad or missing token"
    return None


# -- client ----------------------------------------------------------------------------------


class ControlClient:
    """Talks to the daemon. Every call raises DaemonNotRunning, Unauthorized or ControlError."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        token_file: Path,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._port = port
        self._token_file = token_file
        self._timeout = timeout
        self._transport = transport

    # The token is re-read on every call: the daemon rotates it when it restarts.
    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        try:
            token = read_token(self._token_file)
        except PermissionError as e:
            raise Unauthorized(401, f"cannot read {self._token_file}: {e}") from e
        if token is None:
            raise DaemonNotRunning()
        try:
            # trust_env=False: a system proxy must never see the token or intercept localhost.
            with httpx.Client(
                base_url=f"http://127.0.0.1:{self._port}",
                trust_env=False,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method,
                    API + path,
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.ConnectError as e:
            raise DaemonNotRunning() from e
        except httpx.HTTPError as e:
            raise ControlError(0, f"talking to the tpn daemon failed: {e}") from e
        if response.status_code == 204:
            return None
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text.strip() or response.reason_phrase}
        if response.status_code == 401:
            raise Unauthorized(401, body.get("error", "unauthorized"))
        if response.status_code >= 400:
            message = body.get("error") if isinstance(body, dict) else None
            raise ControlError(response.status_code, message or response.reason_phrase)
        return body

    def is_running(self) -> bool:
        try:
            self.status()
        except DaemonNotRunning:
            return False
        except ControlError:
            return True
        return True

    def status(self) -> Status:
        return self._request("GET", "/status")

    def connect(
        self,
        *,
        server: str | None = None,
        use_tor: bool | None = None,
        keep_tor: bool | None = None,
    ) -> Status:
        """Start connecting; returns at once. Overrides apply to this connection only."""
        body = {
            k: v
            for k, v in {"server": server, "use_tor": use_tor, "keep_tor": keep_tor}.items()
            if v is not None
        }
        return self._request("POST", "/connect", json=body)

    def disconnect(self) -> Status:
        """Tear the connection down; returns once it is down (phase "disconnected")."""
        return self._request("POST", "/disconnect")

    def servers(self) -> list[ServerInfo]:
        return self._request("GET", "/servers")["servers"]

    def prefs(self) -> PrefsDict:
        return self._request("GET", "/prefs")

    def update_prefs(self, **patch: Any) -> PrefsDict:
        return self._request("PATCH", "/prefs", json=patch)

    def set_auth_key(self, key: str) -> None:
        self._request("PUT", "/auth-key", json={"auth_key": key})

    def clear_auth_key(self) -> None:
        self._request("DELETE", "/auth-key")

    def logs(self, lines: int = 200) -> list[str]:
        return self._request("GET", "/logs", params={"lines": lines})["lines"]
