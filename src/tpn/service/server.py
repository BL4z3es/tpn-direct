"""The local control API: stdlib ThreadingHTTPServer bound to 127.0.0.1 only.

Every request passes `control.check_request()` before routing, so a web page in a browser
cannot reach the API (bad Host/Origin -> 403) and callers without the token get 401. The
contract's status codes and shapes live in docs/control-api.md; this module only dispatches
and serializes. The real work lives in `service.core.Service`.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tpn.control import API, check_request
from tpn.service.core import Service, ServiceError

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
LOG_DEFAULT_LINES = 200
LOG_MAX_LINES = 2000


class ControlServer:
    """Owns the socket; blocks in serve_forever() until shutdown() is called."""

    def __init__(self, service: Service, *, port: int, token: str) -> None:
        self.service = service
        self.port = port
        self.token = token

    def make_server(self) -> ThreadingHTTPServer:
        handler = _make_handler(self)
        return ThreadingHTTPServer(("127.0.0.1", self.port), handler)

    def serve_forever(self) -> None:
        server = self.make_server()
        self._server = server
        log.info("control API listening on 127.0.0.1:%d", self.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()

    _server: ThreadingHTTPServer | None = None

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()


def _make_handler(control: ControlServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: Any = None) -> None:
            data = None if body is None else json.dumps(body).encode()
            self.send_response(status)
            if data is not None:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
            elif status == 204:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if data is not None:
                self.wfile.write(data)

        def _fail(self, status: int, message: str) -> None:
            self._send(status, {"error": message})

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return None
            return json.loads(raw.decode())

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            if not path.startswith(API):
                self._fail(404, "unknown path")
                return
            refused = check_request(self.headers, token=control.token, port=control.port)
            if refused is not None:
                self._fail(*refused)
                return
            service = control.service
            try:
                if path == "/v1/status" and method == "GET":
                    self._send(200, service.status())
                elif path == "/v1/connect" and method == "POST":
                    body = self._body() or {}
                    if not isinstance(body, dict):
                        raise ServiceError(400, "the request body must be a JSON object")
                    unknown = set(body) - {"server", "use_tor", "keep_tor"}
                    if unknown:
                        raise ServiceError(400, f"unknown connect fields: {sorted(unknown)}")
                    status = service.connect(
                        server=body.get("server"),
                        use_tor=body.get("use_tor"),
                        keep_tor=body.get("keep_tor"),
                    )
                    self._send(202, status)
                elif path == "/v1/disconnect" and method == "POST":
                    self._send(200, service.disconnect())
                elif path == "/v1/servers" and method == "GET":
                    self._send(200, {"servers": service.servers()})
                elif path == "/v1/prefs" and method == "GET":
                    self._send(200, service.read_prefs().model_dump())
                elif path == "/v1/prefs" and method == "PATCH":
                    body = self._body() or {}
                    if not isinstance(body, dict) or not body:
                        raise ServiceError(400, "PATCH /prefs needs a non-empty JSON object")
                    self._send(200, service.update_prefs(body).model_dump())
                elif path == "/v1/auth-key" and method == "PUT":
                    body = self._body()
                    if not isinstance(body, dict) or "auth_key" not in body:
                        raise ServiceError(400, "auth_key is required")
                    service.set_auth_key(body["auth_key"])
                    self._send(204)
                elif path == "/v1/auth-key" and method == "DELETE":
                    service.clear_auth_key()
                    self._send(204)
                else:
                    self._fail(405, f"{method} is not supported on {path}")
            except ServiceError as e:
                self._fail(e.status, e.message)
            except ValueError as e:  # bad JSON body, bad query
                self._fail(400, str(e))
            except Exception:
                log.exception("%s %s failed", method, path)
                self._fail(500, "internal error")

    Handler.__name__ = f"ControlHandler_{id(control):x}"
    return Handler
