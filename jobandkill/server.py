from __future__ import annotations

import json
import ipaddress
import mimetypes
import os
import re
import secrets
import sys
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import (
    AuthError,
    Session,
    clear_cookie_headers,
    clear_login_intent_cookie_header,
    cookie_headers,
    create_draft,
    delete_account,
    delete_all_drafts,
    delete_draft,
    export_user_data,
    get_draft,
    list_drafts,
    login_intent_cookie_header,
    login_intent_from_cookie,
    normalize_email,
    origin_is_allowed,
    request_magic_link,
    revoke_session,
    session_from_cookie,
    update_draft,
    verify_csrf,
    verify_magic_link,
)
from .db import (
    ROOT,
    Connection,
    DatabaseTarget,
    connect,
    get_profile,
    initialize,
    list_sources,
    public_stats,
    search_profiles,
)
from .catalog import get_catalog_item, list_catalog
from .data_sources import data_coverage
from .privacy import (
    PrivacyError,
    login_consents,
    public_privacy_config,
    record_login_consents,
    require_current_consents,
    verify_login_consents,
)
from .writer import DraftValidationError, compose


WEB_ROOT = (ROOT / "web").resolve()
MAX_REQUEST_BYTES = 64 * 1024
MAX_SEARCH_OFFSET = 1_000
MAX_CATALOG_OFFSET = 2_000_000


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"JSON으로 변환할 수 없는 값입니다: {type(value).__name__}")


class JobAndKillHandler(BaseHTTPRequestHandler):
    server_version = "JobAndKill/0.2"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if os.getenv("JOBNKILL_ENV", "development").strip().lower() == "production":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        if os.getenv("JOBNKILL_QUIET", "0") == "1":
            return
        path = urlsplit(self.path).path
        path = re.sub(r"(/api/user/drafts/)[0-9a-f-]{36}$", r"\1:id", path)
        status = str(args[1]) if len(args) > 1 else "-"
        print(
            json.dumps({"event": "http_request", "method": self.command, "path": path, "status": status}),
            file=sys.stderr,
        )

    def _json(self, status: int, payload: Any, headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=_json_default
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(
        self, status: int, code: str, message: str, details: Any = None,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"error": {"code": code, "message": message}}
        if details is not None:
            payload["error"]["details"] = details
        self._json(status, payload, headers)

    def _read_json(self) -> dict[str, Any] | None:
        if self.headers.get_content_type() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "application/json 요청만 허용됩니다.")
            return None
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if size <= 0 or size > MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_size", "요청 본문은 1~64KB여야 합니다.")
            return None
        try:
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "올바른 JSON 객체를 보내 주세요.")
            return None

    def _origin_ok(self) -> bool:
        if origin_is_allowed(self.headers.get("Origin")):
            return True
        self._error(HTTPStatus.FORBIDDEN, "origin", "허용되지 않은 요청 출처입니다.")
        return False

    def _request_ip(self) -> str:
        peer_text = self.client_address[0]
        try:
            peer = ipaddress.ip_address(peer_text)
            networks = [
                ipaddress.ip_network(item.strip(), strict=False)
                for item in os.getenv("JOBNKILL_TRUSTED_PROXY_CIDRS", "").split(",") if item.strip()
            ]
        except ValueError:
            return peer_text
        render_private_proxy = (
            os.getenv("RENDER", "").strip().lower() == "true" and peer.is_private
        )

        def trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return any(address in network for network in networks) or (
                render_private_proxy and address == peer
            )

        if not trusted(peer):
            return str(peer)
        forwarded = self.headers.get("X-Forwarded-For", "")
        try:
            chain = [ipaddress.ip_address(item.strip()) for item in forwarded.split(",") if item.strip()]
        except ValueError:
            return str(peer)
        for address in reversed([*chain, peer]):
            if not trusted(address):
                return str(address)
        return str(peer)

    def _session(self, connection: Connection, csrf: bool = False) -> Session:
        session = session_from_cookie(connection, self.headers.get("Cookie"))
        if not session:
            raise AuthError("authentication_required", "로그인이 필요합니다.", 401)
        if csrf:
            verify_csrf(session, self.headers.get("X-CSRF-Token"))
        return session

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            self._api_get(parsed.path, parse_qs(parsed.query))
        else:
            self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/drafts/compose" and os.getenv("JOBNKILL_ENV", "development").strip().lower() == "production":
            self._error(HTTPStatus.NOT_FOUND, "browser_compose_only", "초안 작성은 브라우저 안에서만 처리됩니다.")
            return
        payload = self._read_json()
        if payload is None:
            return
        if parsed.path == "/api/drafts/compose":
            self._compose(payload)
            return
        if parsed.path.startswith("/api/auth/") or parsed.path in {
            "/api/user/drafts", "/api/user/export", "/api/user/delete-account",
        }:
            self._api_post(parsed.path, payload)
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/api/user/drafts/([0-9a-f-]{36})", path)
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")
            return
        payload = self._read_json()
        if payload is None or not self._origin_ok():
            return
        try:
            with connect(self.server.db_target) as connection:  # type: ignore[attr-defined]
                session = self._session(connection, csrf=True)
                require_current_consents(connection, session.user_id)
                response = update_draft(connection, session, match.group(1), payload)
        except AuthError as error:
            self._error(error.status, error.code, str(error))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "request_failed", "요청을 처리하지 못했습니다.")
            return
        self._json(HTTPStatus.OK, response)

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/api/user/drafts/([0-9a-f-]{36})", path)
        delete_all = path == "/api/user/drafts"
        if not match and not delete_all:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")
            return
        if not self._origin_ok():
            return
        try:
            with connect(self.server.db_target) as connection:  # type: ignore[attr-defined]
                session = self._session(connection, csrf=True)
                deleted = (
                    delete_all_drafts(connection, session)
                    if delete_all else (delete_draft(connection, session, match.group(1)) or 1)
                )
        except AuthError as error:
            self._error(error.status, error.code, str(error))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "request_failed", "요청을 처리하지 못했습니다.")
            return
        response: dict[str, Any] = {"deleted": True}
        if delete_all:
            response["count"] = deleted
        self._json(HTTPStatus.OK, response)

    def _compose(self, payload: dict[str, Any]) -> None:
        try:
            self._json(HTTPStatus.OK, compose(payload))
        except DraftValidationError as error:
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation", str(error), error.errors)
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "compose_failed", "문서 생성 중 오류가 발생했습니다.")

    def _api_post(self, path: str, payload: dict[str, Any]) -> None:
        if not self._origin_ok():
            return
        if path not in {
            "/api/auth/request", "/api/auth/verify", "/api/auth/logout", "/api/user/drafts",
            "/api/user/export", "/api/user/delete-account",
        }:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")
            return

        status = HTTPStatus.OK
        response: Any = None
        headers: list[tuple[str, str]] = []
        try:
            with connect(self.server.db_target) as connection:  # type: ignore[attr-defined]
                if path == "/api/auth/request":
                    # This check happens before request_magic_link can create a user or persist an email.
                    consents = login_consents(payload)
                    email = normalize_email(payload.get("email"))
                    intent_token = secrets.token_urlsafe(32)
                    response = request_magic_link(
                        connection, email, self._request_ip(), intent_token,
                        before_commit=lambda user_id, token_hash: record_login_consents(
                            connection, user_id, token_hash, consents
                        ),
                    )
                    headers = [("Set-Cookie", login_intent_cookie_header(intent_token))]
                    status = HTTPStatus.ACCEPTED
                elif path == "/api/auth/verify":
                    result = verify_magic_link(
                        connection,
                        payload.get("token"),
                        login_intent_from_cookie(self.headers.get("Cookie")),
                        after_verify=lambda user_id, token_hash: verify_login_consents(
                            connection, user_id, token_hash
                        ),
                    )
                    headers = [("Set-Cookie", value) for value in cookie_headers(result)]
                    headers.append(("Set-Cookie", clear_login_intent_cookie_header()))
                    response = {
                        "authenticated": True,
                        "user": {"id": result.user_id, "email": result.email},
                    }
                elif path == "/api/auth/logout":
                    session = self._session(connection, csrf=True)
                    revoke_session(connection, session)
                    headers = [("Set-Cookie", value) for value in clear_cookie_headers()]
                    response = {"authenticated": False}
                elif path == "/api/user/export":
                    session = self._session(connection, csrf=True)
                    response = export_user_data(connection, session)
                elif path == "/api/user/delete-account":
                    session = self._session(connection, csrf=True)
                    delete_account(connection, session, payload.get("confirmation"))
                    headers = [("Set-Cookie", value) for value in clear_cookie_headers()]
                    response = {"deleted": True}
                else:
                    session = self._session(connection, csrf=True)
                    require_current_consents(connection, session.user_id)
                    response = create_draft(connection, session, payload)
                    status = HTTPStatus.CREATED
        except (AuthError, PrivacyError) as error:
            self._error(error.status, error.code, str(error))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "request_failed", "요청을 처리하지 못했습니다.")
            return
        self._json(status, response, headers)

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path in {"/api/jobs", "/api/catalog"}:
            try:
                limit = int(query.get("limit", ["20"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "pagination", "limit과 offset은 숫자여야 합니다.")
                return
            offset_max = MAX_CATALOG_OFFSET if path == "/api/catalog" else MAX_SEARCH_OFFSET
            if offset < 0 or offset > offset_max:
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "pagination",
                    f"offset은 0~{offset_max:,} 범위여야 합니다.",
                )
                return
            if path == "/api/catalog" and (not 1 <= limit <= 100 or len(query.get("q", [""])[0]) > 200):
                self._error(HTTPStatus.BAD_REQUEST, "catalog_query", "검색어는 200자, limit은 1~100 범위여야 합니다.")
                return

        response: Any = None
        route_found = False
        try:
            with connect(self.server.db_target) as connection:  # type: ignore[attr-defined]
                if path == "/api/health":
                    route_found = True
                    response = {"status": "ok"}
                elif path == "/api/stats":
                    route_found = True
                    response = {"stats": public_stats(connection)}
                elif path == "/api/data-coverage":
                    route_found = True
                    response = data_coverage(connection)
                elif path == "/api/catalog":
                    route_found = True
                    if connection.dialect == "postgres":
                        connection.execute("SET LOCAL statement_timeout = '2000ms'")
                    items = list_catalog(connection, query.get("q", [""])[0], limit, offset)
                    response = {"items": items, "count": len(items), "offset": offset}
                elif catalog_match := re.fullmatch(r"/api/catalog/([0-9a-f]{64})", path):
                    route_found = True
                    item = get_catalog_item(connection, catalog_match.group(1))
                    if not item:
                        raise AuthError("catalog_not_found", "수집된 NCS 참고정보를 찾을 수 없습니다.", 404)
                    item.pop("raw", None)
                    response = {"item": item}
                elif path == "/api/privacy-config":
                    route_found = True
                    response = public_privacy_config()
                elif path == "/api/me":
                    route_found = True
                    session = session_from_cookie(connection, self.headers.get("Cookie"))
                    response = {"authenticated": False} if not session else {
                        "authenticated": True,
                        "user": {"id": session.user_id, "email": session.email},
                    }
                elif path == "/api/sources":
                    route_found = True
                    sources = list_sources(connection)
                    for source in sources:
                        source["enabled"] = bool(source["enabled"])
                        source["has_error"] = bool(source.pop("last_error", ""))
                    response = {
                        "sources": sources,
                        "copyright_policy_url": "https://www.ncs.go.kr/unity/th01/selectPolicyPopView.do",
                    }
                elif path == "/api/jobs":
                    route_found = True
                    items = search_profiles(
                        connection, query.get("q", [""])[0], query.get("institution", [""])[0], limit, offset
                    )
                    response = {"items": items, "count": len(items), "offset": max(offset, 0)}
                elif path == "/api/user/drafts":
                    route_found = True
                    session = self._session(connection)
                    response = {"items": list_drafts(connection, session)}
                elif draft_match := re.fullmatch(r"/api/user/drafts/([0-9a-f-]{36})", path):
                    route_found = True
                    session = self._session(connection)
                    response = get_draft(connection, session, draft_match.group(1))
                elif match := re.fullmatch(r"/api/jobs/(\d+)", path):
                    route_found = True
                    response = get_profile(connection, int(match.group(1)))
                    if not response:
                        raise AuthError("job_not_found", "직무 정보를 찾을 수 없습니다.", 404)
        except (AuthError, PrivacyError) as error:
            self._error(error.status, error.code, str(error))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "request_failed", "요청을 처리하지 못했습니다.")
            return
        if not route_found:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "요청한 API를 찾을 수 없습니다.")
            return
        self._json(HTTPStatus.OK, response)

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
        self.send_header(
            "Content-Type",
            f"{media_type}; charset=utf-8" if media_type.startswith("text/") or media_type.endswith("javascript") else media_type,
        )
        self.send_header("Content-Length", str(len(body)))
        revalidate = candidate.name == "index.html" or candidate.suffix in {".js", ".css"}
        self.send_header("Cache-Control", "no-cache" if revalidate else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)


class JobAndKillServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], db_path: DatabaseTarget | None = None) -> None:
        initialize(db_path)
        self.db_target = db_path
        super().__init__(address, JobAndKillHandler)


def serve(host: str | None = None, port: int | None = None, db_path: DatabaseTarget | None = None) -> None:
    host = host or os.getenv("JOBNKILL_HOST", "127.0.0.1")
    port = port or int(os.getenv("PORT", os.getenv("JOBNKILL_PORT", "8787")))
    server = JobAndKillServer((host, port), db_path)
    print(f"Job&Kill: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
