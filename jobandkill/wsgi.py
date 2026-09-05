"""Production WSGI transport for the shared local HTTP request handlers.

Gunicorn owns HTTP parsing, sockets, process limits and SIGTERM draining. This
adapter only maps an already-parsed WSGI request onto the existing routes; it
never constructs a second HTTP server or reparses a raw HTTP request. Responses
are buffered, matching the existing JSON/static-file implementation.
"""
from __future__ import annotations

from email.message import Message
from http import HTTPStatus
from io import BytesIO
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import quote_from_bytes

from .auth import environment
from .config import require_production_settings
from .db import DatabaseTarget, initialize
from .server import JobAndKillHandler, MAX_REQUEST_BYTES


class _WSGIHandler(JobAndKillHandler):
    """Reuse routing and security headers, replacing only HTTP transport hooks."""

    def __init__(self, environ: dict[str, Any], db_target: DatabaseTarget) -> None:
        self.server = SimpleNamespace(db_target=db_target)
        self.command = environ.get("REQUEST_METHOD", "GET")
        # RAW_URI preserves the local handler's URL-escaping semantics. Standard
        # WSGI PATH_INFO is Latin-1 decoded bytes and needs URL-encoding again.
        path = quote_from_bytes(environ.get("PATH_INFO", "/").encode("latin-1"), safe="/")
        query = environ.get("QUERY_STRING", "")
        self.path = environ.get("RAW_URI") or path + ("?" + query if query else "")
        self.client_address = (environ.get("REMOTE_ADDR", ""), 0)
        self.headers = Message()
        for key, value in environ.items():
            if key.startswith("HTTP_") and key not in {"HTTP_CONTENT_LENGTH", "HTTP_CONTENT_TYPE"}:
                self.headers[key[5:].replace("_", "-")] = value
        if environ.get("CONTENT_TYPE"):
            self.headers["Content-Type"] = environ["CONTENT_TYPE"]
        self.request_version = "HTTP/1.1"
        self._headers_buffer: list[bytes] = []
        self.response_headers: list[tuple[str, str]] = []
        self.status = HTTPStatus.INTERNAL_SERVER_ERROR
        self.rfile = BytesIO()
        self.wfile = BytesIO()

    def send_response(self, code: int, message: str | None = None) -> None:
        self.status = HTTPStatus(code)
        self.response_headers = []

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers.append((keyword, value))

    def flush_headers(self) -> None:
        # Shared end_headers adds security policy through send_header. Discard
        # BaseHTTPRequestHandler's framing-only CRLF; WSGI owns wire framing.
        self._headers_buffer = []

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log arbitrary request paths, query strings, headers or bodies.
        pass

    def read_body(self, environ: dict[str, Any]) -> bool:
        raw_length = environ.get("CONTENT_LENGTH", "")
        if raw_length:
            if not raw_length.isascii() or not raw_length.isdecimal():
                self._error(400, "request_size", "올바른 Content-Length가 필요합니다.")
                return False
            normalized_length = raw_length.lstrip("0") or "0"
            if len(normalized_length) > len(str(MAX_REQUEST_BYTES)) or int(normalized_length) > MAX_REQUEST_BYTES:
                self._error(413, "request_size", "요청 본문은 64KB 이하여야 합니다.")
                return False
            size = int(normalized_length)
            body = environ["wsgi.input"].read(size) if size else b""
            if len(body) != size:
                self._error(400, "request_size", "요청 본문이 완전하지 않습니다.")
                return False
        elif environ.get("wsgi.input_terminated"):
            # Gunicorn supplies a safely terminated stream for chunked bodies.
            body = environ["wsgi.input"].read(MAX_REQUEST_BYTES + 1)
            if len(body) > MAX_REQUEST_BYTES:
                self._error(413, "request_size", "요청 본문은 64KB 이하여야 합니다.")
                return False
        else:
            # Unbounded reads can block on ordinary WSGI socket streams.
            body = b""
        self.headers["Content-Length"] = str(len(body))
        self.rfile = BytesIO(body)
        return True


class WSGIApplication:
    def __init__(self, db_target: DatabaseTarget) -> None:
        self.db_target = db_target

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        handler = _WSGIHandler(environ, self.db_target)
        try:
            browser_only_compose = (
                environment() == "production"
                and handler.command == "POST"
                and environ.get("PATH_INFO") == "/api/drafts/compose"
            )
            if browser_only_compose:
                handler._error(
                    404,
                    "browser_compose_only",
                    "초안 작성은 브라우저 안에서만 처리됩니다.",
                )
            elif handler.read_body(environ):
                method = handler.command
                if method == "HEAD":
                    handler.do_GET()
                elif method in {"GET", "POST", "PUT", "DELETE"}:
                    getattr(handler, "do_" + method)()
                else:
                    handler._error(405, "method_not_allowed", "지원하지 않는 요청 방식입니다.",
                                   headers=[("Allow", "GET, HEAD, POST, PUT, DELETE")])
        except Exception:
            # The response has not started, so discard any partial response and
            # prevent exceptions/DSNs/provider details reaching clients or logs.
            handler.wfile = BytesIO()
            handler._error(500, "request_failed", "요청을 처리하지 못했습니다.")
        body = handler.wfile.getvalue()
        if os.getenv("JOBNKILL_QUIET", "0") != "1":
            method = handler.command if handler.command in {"GET", "HEAD", "POST", "PUT", "DELETE"} else "OTHER"
            print(json.dumps({"event": "http_request", "method": method,
                              "status": str(int(handler.status))}), file=sys.stderr)
        start_response(f"{int(handler.status)} {handler.status.phrase}", handler.response_headers)
        return [b"" if handler.command == "HEAD" else body]


def create_app(db_target: DatabaseTarget | None = None) -> WSGIApplication:
    """Validate and migrate before accepting traffic; safe for Gunicorn preload.

    initialize closes its database connection before fork. Request connections
    remain scoped to the shared handlers and are never inherited by workers.
    """
    if environment() == "production":
        require_production_settings()
    try:
        target = initialize(db_target)
    except Exception:
        raise RuntimeError("Application database initialization failed; check configuration and connectivity.") from None
    return WSGIApplication(target)
