from __future__ import annotations

import json
import mimetypes
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .db import ROOT, connect, get_profile, initialize, list_sources, public_stats, search_profiles
from .writer import DraftValidationError, compose


WEB_ROOT = (ROOT / "web").resolve()
MAX_REQUEST_BYTES = 64 * 1024


class JobAndKillHandler(BaseHTTPRequestHandler):
    server_version = "JobAndKill/0.1"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        if os.getenv("JOBNKILL_QUIET", "0") != "1":
            super().log_message(format, *args)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, details: Any = None) -> None:
        payload: dict[str, Any] = {"error": {"code": code, "message": message}}
        if details is not None:
            payload["error"]["details"] = details
        self._json(status, payload)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            self._api_get(parsed.path, parse_qs(parsed.query))
        else:
            self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/api/drafts/compose":
            self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "application/json 요청만 허용됩니다.")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if size <= 0 or size > MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_size", "요청 본문은 1~64KB여야 합니다.")
            return
        try:
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "올바른 JSON 객체를 보내 주세요.")
            return
        try:
            self._json(HTTPStatus.OK, compose(payload))
        except DraftValidationError as error:
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation", str(error), error.errors)
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "compose_failed", "문서 생성 중 오류가 발생했습니다.")

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        with connect(self.server.db_path) as connection:  # type: ignore[attr-defined]
            if path == "/api/health":
                self._json(HTTPStatus.OK, {"status": "ok", "version": "0.1.0", "stats": public_stats(connection)})
                return
            if path == "/api/sources":
                sources = list_sources(connection)
                for source in sources:
                    source["enabled"] = bool(source["enabled"])
                    source["has_error"] = bool(source.pop("last_error", ""))
                self._json(
                    HTTPStatus.OK,
                    {
                        "sources": sources,
                        "copyright_policy_url": "https://www.ncs.go.kr/unity/th01/selectPolicyPopView.do",
                    },
                )
                return
            if path == "/api/jobs":
                try:
                    limit = int(query.get("limit", ["20"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    self._error(HTTPStatus.BAD_REQUEST, "pagination", "limit과 offset은 숫자여야 합니다.")
                    return
                items = search_profiles(
                    connection,
                    query.get("q", [""])[0],
                    query.get("institution", [""])[0],
                    limit,
                    offset,
                )
                self._json(HTTPStatus.OK, {"items": items, "count": len(items), "offset": max(offset, 0)})
                return
            match = re.fullmatch(r"/api/jobs/(\d+)", path)
            if match:
                profile = get_profile(connection, int(match.group(1)))
                if not profile:
                    self._error(HTTPStatus.NOT_FOUND, "job_not_found", "직무 정보를 찾을 수 없습니다.")
                    return
                self._json(HTTPStatus.OK, profile)
                return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")

    def _static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in candidate.parents and candidate != WEB_ROOT:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "파일을 찾을 수 없습니다.")
            return
        if not candidate.is_file():
            candidate = WEB_ROOT / "index.html"
        try:
            body = candidate.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "화면 파일을 찾을 수 없습니다.")
            return
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8" if media_type.startswith("text/") or media_type.endswith("javascript") else media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)


class JobAndKillServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], db_path: Path | str | None = None) -> None:
        self.db_path = initialize(db_path)
        super().__init__(address, JobAndKillHandler)


def serve(host: str | None = None, port: int | None = None, db_path: Path | str | None = None) -> None:
    host = host or os.getenv("JOBNKILL_HOST", "127.0.0.1")
    port = port or int(os.getenv("JOBNKILL_PORT", "8787"))
    server = JobAndKillServer((host, port), db_path)
    print(f"Job&Kill: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
