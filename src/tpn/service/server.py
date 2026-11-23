"""The control API's HTTP layer: stdlib ThreadingHTTPServer on 127.0.0.1, nothing else.

Every request goes through control.check_request() before routing (Host, Origin, token), then
a route table maps it onto Service. Shapes and status codes: docs/control-api.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tpn.control import API, check_request
from tpn.service.core import LOGS_DEFAULT, Service, ServiceError

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024

# (status, body) for a request; body None means no content.
Reply = tuple[int, Any]


def _object(body: Any, what: str) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ServiceError(400, f"{what}: the body must be a JSON object")
    return body


def _connect(service: Service, body: Any, query: dict[str, list[str]]) -> Reply:
    args = _object(body, "connect")
    unknown = set(args) - {"server", "use_tor", "keep_tor"}
    if unknown:
        raise ServiceError(400, f"connect: unknown fields {sorted(unknown)}")
    if not isinstance(args.get("server"), str | None):
        raise ServiceError(400, "connect: server must be a string")
    for flag in ("use_tor", "keep_tor"):
        if not isinstance(args.get(flag), bool | None):
            raise ServiceError(400, f"connect: {flag} must be true or false")
    return 202, service.connect(**args)


def _patch_prefs(service: Service, body: Any, query: dict[str, list[str]]) -> Reply:
    return 200, service.update_prefs(_object(body, "prefs")).model_dump()


def _put_key(service: Service, body: Any, query: dict[str, list[str]]) -> Reply:
    service.set_auth_key(_object(body, "auth-key").get("auth_key"))
    return 204, None


def _delete_key(service: Service, body: Any, query: dict[str, list[str]]) -> Reply:
    service.clear_auth_key()
    return 204, None


def _logs(service: Service, body: Any, query: dict[str, list[str]]) -> Reply:
    try:
        lines = int(query.get("lines", [LOGS_DEFAULT])[0])
    except ValueError:
        raise ServiceError(400, "lines must be an integer") from None
    return 200, {"lines": service.logs(lines)}


Handler = Callable[[Service, Any, dict[str, list[str]]], Reply]

ROUTES: dict[str, dict[str, Handler]] = {
    "/status": {"GET": lambda s, b, q: (200, s.status())},
    "/connect": {"POST": _connect},
    "/disconnect": {"POST": lambda s, b, q: (200, s.disconnect())},
    "/servers": {"GET": lambda s, b, q: (200, {"servers": s.servers()})},
    "/prefs": {"GET": lambda s, b, q: (200, s.read_prefs().model_dump()), "PATCH": _patch_prefs},
    "/auth-key": {"PUT": _put_key, "DELETE": _delete_key},
    "/logs": {"GET": _logs},
}


class ControlServer:
    """Binds at construction (so port 0 works in tests); serve_forever() blocks until shutdown()."""

    def __init__(self, service: Service, *, token: str, port: int, host: str = "127.0.0.1") -> None:
        self.service = service
        self.token = token
        self.httpd = ThreadingHTTPServer((host, port), _handler_for(self))
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def serve_forever(self) -> None:
        log.info("control API listening on 127.0.0.1:%d", self.port)
        self.httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        """Stop serve_forever() (call from another thread) and close the socket."""
        self.httpd.shutdown()
        self.httpd.server_close()


def _handler_for(control: ControlServer) -> type[BaseHTTPRequestHandler]:
    class RequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tpnd"

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("control: " + format, *args)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def _handle(self, method: str) -> None:
            try:
                status, body = self._route(method)
            except ServiceError as e:
                status, body = e.status, {"error": e.message}
            except ValueError as e:  # bad JSON; pydantic's ValidationError for prefs
                status, body = 400, {"error": str(e)}
            except Exception:
                log.exception("control API: %s %s failed", method, self.path)
                status, body = 500, {"error": "internal error; see the daemon log"}
            self._send(status, body)

        def _route(self, method: str) -> Reply:
            refused = check_request(self.headers, token=control.token, port=control.port)
            if refused is not None:
                raise ServiceError(*refused)
            url = urlsplit(self.path)
            if not url.path.startswith(API + "/"):
                raise ServiceError(404, "not found")
            methods = ROUTES.get(url.path[len(API) :])
            if methods is None:
                raise ServiceError(404, "not found")
            handler = methods.get(method)
            if handler is None:
                raise ServiceError(405, f"{method} is not allowed here")
            return handler(control.service, self._body(), parse_qs(url.query))

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ServiceError(400, "request body too large")
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw.strip() else None

        def _send(self, status: int, body: Any) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return RequestHandler
