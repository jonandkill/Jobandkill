from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from .db import Connection, json_text, json_value
from .writer import DraftValidationError, sanitize_draft


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SESSION_DAYS = 30
IDLE_DAYS = 7
MAGIC_MINUTES = 15


class AuthError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Session:
    user_id: str
    email: str
    token_hash: str
    csrf_hash: str


@dataclass(frozen=True)
class LoginResult:
    session_token: str
    csrf_token: str
    user_id: str
    email: str


def environment() -> str:
    value = os.getenv("JOBNKILL_ENV", "development").strip().lower()
    if value not in {"development", "test", "production"}:
        raise AuthError("auth_config", "JOBNKILL_ENV 설정을 확인해 주세요.", 500)
    return value


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email) or any(ord(char) < 32 for char in email):
        raise AuthError("invalid_email", "올바른 이메일 주소를 입력해 주세요.")
    return email


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rate_digest(kind: str, value: str) -> str:
    secret = os.getenv("JOBNKILL_AUTH_RATE_SECRET", "").strip()
    if not secret:
        if environment() == "production":
            raise AuthError("auth_config", "운영 인증 속도 제한 비밀키가 설정되지 않았습니다.", 500)
        secret = "jobandkill-development-rate-secret"
    return hmac.new(secret.encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def _record_rate_event(connection: Connection, subject_hash: str, maximum: int) -> None:
    now = utcnow()
    cutoff = iso(now - timedelta(minutes=15))
    count = connection.execute(
        "SELECT COUNT(*) AS count FROM auth_request_events WHERE subject_hash=? AND created_at>=?",
        (subject_hash, cutoff),
    ).fetchone()["count"]
    if int(count) >= maximum:
        raise AuthError("rate_limited", "잠시 후 다시 시도해 주세요.", 429)
    connection.execute(
        "INSERT INTO auth_request_events(subject_hash, created_at) VALUES (?, ?)",
        (subject_hash, iso(now)),
    )
    connection.execute(
        "DELETE FROM auth_request_events WHERE created_at < ?", (iso(now - timedelta(days=1)),)
    )


def public_url() -> str:
    configured = os.getenv("JOBNKILL_PUBLIC_URL", "").strip().rstrip("/")
    if not configured:
        if environment() == "production":
            raise AuthError("auth_config", "JOBNKILL_PUBLIC_URL이 설정되지 않았습니다.", 500)
        configured = "http://127.0.0.1:8787"
    parsed = urlsplit(configured)
    try:
        parsed.port
    except ValueError as error:
        raise AuthError("auth_config", "JOBNKILL_PUBLIC_URL 포트 형식이 올바르지 않습니다.", 500) from error
    if not parsed.hostname or parsed.query or parsed.fragment:
        raise AuthError("auth_config", "JOBNKILL_PUBLIC_URL 형식이 올바르지 않습니다.", 500)
    if environment() == "production" and parsed.scheme != "https":
        raise AuthError("auth_config", "운영 공개 주소는 HTTPS여야 합니다.", 500)
    if parsed.scheme not in {"http", "https"}:
        raise AuthError("auth_config", "공개 주소는 HTTP 또는 HTTPS여야 합니다.", 500)
    return configured


def origin_is_allowed(origin: str | None) -> bool:
    if not origin:
        return environment() != "production"
    expected = urlsplit(public_url())
    actual = urlsplit(origin)
    try:
        return (actual.scheme, actual.hostname, actual.port) == (expected.scheme, expected.hostname, expected.port)
    except ValueError:
        return False


def _send_magic_link(email: str, link: str) -> None:
    if environment() != "production" and os.getenv("JOBNKILL_AUTH_DEV_SHOW_LINK", "0") == "1":
        return
    host = os.getenv("JOBNKILL_SMTP_HOST", "").strip()
    sender = os.getenv("JOBNKILL_SMTP_FROM", "").strip()
    if not host or not sender:
        raise AuthError("mail_unavailable", "로그인 메일 설정이 완료되지 않았습니다.", 503)
    try:
        port = int(os.getenv("JOBNKILL_SMTP_PORT", "587"))
    except ValueError as error:
        raise AuthError("auth_config", "SMTP 포트 설정을 확인해 주세요.", 500) from error
    security = os.getenv("JOBNKILL_SMTP_SECURITY", "starttls").strip().lower()
    if security not in {"starttls", "ssl", "plain"}:
        raise AuthError("auth_config", "SMTP 보안 설정을 확인해 주세요.", 500)
    if environment() == "production" and security == "plain":
        raise AuthError("auth_config", "운영 SMTP는 암호화 연결을 사용해야 합니다.", 500)
    username = os.getenv("JOBNKILL_SMTP_USERNAME", "").strip()
    password = os.getenv("JOBNKILL_SMTP_PASSWORD", "")
    message = EmailMessage()
    message["Subject"] = "Job&Kill 로그인 링크"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "아래 링크로 Job&Kill에 로그인하세요. 링크는 15분 동안 한 번만 사용할 수 있습니다.\n\n"
        + link
        + "\n\n요청하지 않았다면 이 메일을 무시하세요."
    )
    try:
        context = ssl.create_default_context()
        client_class = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        client_kwargs: dict[str, Any] = {"timeout": 15}
        if security == "ssl":
            client_kwargs["context"] = context
        with client_class(host, port, **client_kwargs) as client:
            if security == "starttls":
                client.starttls(context=context)
            if username:
                client.login(username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise AuthError("mail_failed", "로그인 메일을 보내지 못했습니다.", 503) from error


def request_magic_link(connection: Connection, raw_email: Any, request_ip: str) -> dict[str, Any]:
    email = normalize_email(raw_email)
    email_subject = _rate_digest("email", email)
    ip_subject = _rate_digest("ip", request_ip)
    global_subject = _rate_digest("global", "magic-link")
    subjects = sorted((email_subject, ip_subject, global_subject))
    if connection.dialect == "postgres":
        for subject in subjects:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"auth-rate:{subject}",))
    else:
        connection.execute("BEGIN IMMEDIATE")
    _record_rate_event(connection, email_subject, 5)
    _record_rate_event(connection, ip_subject, 20)
    _record_rate_event(connection, global_subject, 500)
    user_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO users(id, email) VALUES (?, ?) ON CONFLICT(email) DO NOTHING",
        (user_id, email),
    )
    user = connection.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    user_id = str(user["id"])
    now = utcnow()
    connection.execute(
        "UPDATE login_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
        (iso(now), user_id),
    )
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO login_tokens(token_hash, user_id, expires_at, request_subject_hash) VALUES (?, ?, ?, ?)",
        (_digest(token), user_id, iso(now + timedelta(minutes=MAGIC_MINUTES)), email_subject),
    )
    connection.commit()
    link = f"{public_url()}/#login_token={token}"
    try:
        _send_magic_link(email, link)
    except AuthError:
        connection.execute(
            "UPDATE login_tokens SET used_at=? WHERE token_hash=?", (iso(utcnow()), _digest(token))
        )
        connection.commit()
        raise
    response: dict[str, Any] = {
        "accepted": True,
        "message": "입력한 주소로 로그인 링크를 보냈습니다. 링크는 15분 동안 유효합니다.",
    }
    if environment() != "production" and os.getenv("JOBNKILL_AUTH_DEV_SHOW_LINK", "0") == "1":
        response["development_magic_link"] = link
    return response


def verify_magic_link(connection: Connection, token: Any) -> LoginResult:
    raw = str(token or "")
    if not 20 <= len(raw) <= 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise AuthError("invalid_token", "로그인 링크가 올바르지 않거나 만료되었습니다.", 401)
    now = utcnow()
    row = connection.execute(
        """
        UPDATE login_tokens SET used_at=?
        WHERE token_hash=? AND used_at IS NULL AND expires_at>?
        RETURNING user_id
        """,
        (iso(now), _digest(raw), iso(now)),
    ).fetchone()
    if not row:
        raise AuthError("invalid_token", "로그인 링크가 올바르지 않거나 만료되었습니다.", 401)
    user = connection.execute(
        "SELECT id, email FROM users WHERE id=? AND status='active'", (str(row["user_id"]),)
    ).fetchone()
    if not user:
        raise AuthError("invalid_token", "로그인 링크가 올바르지 않거나 만료되었습니다.", 401)
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    connection.execute(
        """
        INSERT INTO sessions(
          token_hash, user_id, csrf_hash, expires_at, idle_expires_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _digest(session_token), str(user["id"]), _digest(csrf_token),
            iso(now + timedelta(days=SESSION_DAYS)), iso(now + timedelta(days=IDLE_DAYS)), iso(now),
        ),
    )
    connection.execute(
        "UPDATE users SET email_verified_at=COALESCE(email_verified_at, ?), last_login_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (iso(now), iso(now), str(user["id"])),
    )
    return LoginResult(session_token, csrf_token, str(user["id"]), str(user["email"]))


def session_cookie_name() -> str:
    return "__Host-jobandkill_session" if environment() == "production" else "jobandkill_session"


def session_from_cookie(connection: Connection, header: str | None) -> Session | None:
    cookie = SimpleCookie()
    try:
        cookie.load(header or "")
    except Exception:
        return None
    morsel = cookie.get(session_cookie_name())
    if not morsel:
        return None
    token_hash = _digest(morsel.value)
    row = connection.execute(
        """
        SELECT s.token_hash, s.user_id, s.csrf_hash, s.expires_at, s.idle_expires_at,
               u.email, u.status
        FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.revoked_at IS NULL
        """,
        (token_hash,),
    ).fetchone()
    if not row or row["status"] != "active":
        return None
    now = utcnow()
    if _datetime(row["expires_at"]) <= now or _datetime(row["idle_expires_at"]) <= now:
        connection.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (iso(now), token_hash))
        return None
    idle = min(_datetime(row["expires_at"]), now + timedelta(days=IDLE_DAYS))
    connection.execute(
        "UPDATE sessions SET last_seen_at=?, idle_expires_at=? WHERE token_hash=?",
        (iso(now), iso(idle), token_hash),
    )
    return Session(str(row["user_id"]), str(row["email"]), token_hash, str(row["csrf_hash"]))


def verify_csrf(session: Session, token: str | None) -> None:
    supplied = _digest(str(token or ""))
    if not token or not hmac.compare_digest(supplied, session.csrf_hash):
        raise AuthError("csrf", "보안 확인값이 없거나 올바르지 않습니다.", 403)


def revoke_session(connection: Connection, session: Session) -> None:
    connection.execute(
        "UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
        (iso(utcnow()), session.token_hash),
    )


def cookie_headers(result: LoginResult) -> list[str]:
    secure = environment() == "production"
    secure_part = "; Secure" if secure else ""
    return [
        f"{session_cookie_name()}={result.session_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}{secure_part}",
        f"jobandkill_csrf={result.csrf_token}; Path=/; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure_part}",
    ]


def clear_cookie_headers() -> list[str]:
    secure_part = "; Secure" if environment() == "production" else ""
    return [
        f"{session_cookie_name()}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure_part}",
        f"jobandkill_csrf=; Path=/; SameSite=Strict; Max-Age=0{secure_part}",
    ]


def _draft_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AuthError("invalid_draft", "초안 데이터 형식이 올바르지 않습니다.", 422)
    try:
        return sanitize_draft(raw)
    except DraftValidationError as error:
        raise AuthError("invalid_draft", "초안 입력값을 확인해 주세요.", 422) from error


def _draft_response(row: Any, include_payload: bool = True) -> dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "client_key": str(row["client_key"]),
        "title": str(row["title"]),
        "current_step": int(row["current_step"]),
        "revision": int(row["revision"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_payload:
        result["payload"] = json_value(row["payload_json"], {})
    return result


def list_drafts(connection: Connection, session: Session) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, client_key, title, current_step, revision, created_at, updated_at
        FROM user_drafts WHERE user_id=? ORDER BY updated_at DESC LIMIT 50
        """,
        (session.user_id,),
    ).fetchall()
    return [_draft_response(row, include_payload=False) for row in rows]


def get_draft(connection: Connection, session: Session, draft_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM user_drafts WHERE id=? AND user_id=?", (draft_id, session.user_id)
    ).fetchone()
    if not row:
        raise AuthError("draft_not_found", "초안을 찾을 수 없습니다.", 404)
    return _draft_response(row)


def create_draft(connection: Connection, session: Session, request: dict[str, Any]) -> dict[str, Any]:
    payload = _draft_payload(request.get("payload"))
    client_key = str(request.get("client_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", client_key):
        raise AuthError("invalid_client_key", "초안 식별값이 올바르지 않습니다.", 422)
    current_step = request.get("current_step", 0)
    if type(current_step) is not int or not 0 <= current_step <= 7:
        raise AuthError("invalid_step", "작성 단계가 올바르지 않습니다.", 422)
    title = str(request.get("title") or payload.get("experience_title") or "제목 없는 초안").strip()[:300]
    draft_id = str(uuid.uuid4())
    try:
        connection.execute(
            """
            INSERT INTO user_drafts(id, user_id, client_key, title, payload_json, current_step)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (draft_id, session.user_id, client_key, title, json_text(payload), current_step),
        )
    except Exception as error:
        connection.rollback()
        existing = connection.execute(
            "SELECT id FROM user_drafts WHERE user_id=? AND client_key=?", (session.user_id, client_key)
        ).fetchone()
        if existing:
            raise AuthError("draft_conflict", "같은 브라우저 초안이 이미 계정에 있습니다.", 409) from error
        raise
    return get_draft(connection, session, draft_id)


def update_draft(
    connection: Connection, session: Session, draft_id: str, request: dict[str, Any]
) -> dict[str, Any]:
    payload = _draft_payload(request.get("payload"))
    client_key = str(request.get("client_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", client_key):
        raise AuthError("invalid_client_key", "초안 식별값이 올바르지 않습니다.", 422)
    revision = request.get("revision")
    current_step = request.get("current_step", 0)
    if type(revision) is not int or revision < 1:
        raise AuthError("invalid_revision", "초안 버전이 올바르지 않습니다.", 422)
    if type(current_step) is not int or not 0 <= current_step <= 7:
        raise AuthError("invalid_step", "작성 단계가 올바르지 않습니다.", 422)
    title = str(request.get("title") or payload.get("experience_title") or "제목 없는 초안").strip()[:300]
    row = connection.execute(
        """
        UPDATE user_drafts SET title=?, payload_json=?, current_step=?, revision=revision+1,
          updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND user_id=? AND client_key=? AND revision=?
        RETURNING id
        """,
        (title, json_text(payload), current_step, draft_id, session.user_id, client_key, revision),
    ).fetchone()
    if not row:
        exists = connection.execute(
            "SELECT revision, client_key FROM user_drafts WHERE id=? AND user_id=?",
            (draft_id, session.user_id),
        ).fetchone()
        if exists:
            if str(exists["client_key"]) != client_key:
                raise AuthError(
                    "draft_identity_conflict", "다른 기기의 초안은 덮어쓸 수 없습니다.", 409
                )
            raise AuthError("draft_conflict", "다른 화면에서 초안이 변경되었습니다.", 409)
        raise AuthError("draft_not_found", "초안을 찾을 수 없습니다.", 404)
    return get_draft(connection, session, draft_id)


def delete_draft(connection: Connection, session: Session, draft_id: str) -> None:
    cursor = connection.execute(
        "DELETE FROM user_drafts WHERE id=? AND user_id=?", (draft_id, session.user_id)
    )
    if cursor.rowcount == 0:
        raise AuthError("draft_not_found", "초안을 찾을 수 없습니다.", 404)
