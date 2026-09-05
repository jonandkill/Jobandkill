from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
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
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .db import Connection, json_text, json_value
from .writer import DraftValidationError, contains_resident_registration_number, sanitize_draft


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SESSION_DAYS = 30
IDLE_DAYS = 7
MAGIC_MINUTES = 15
ACCOUNT_DELETE_REAUTH_MINUTES = 15
ACCOUNT_DELETE_CONFIRMATION = "DELETE MY ACCOUNT"
RESEND_EMAILS_URL = "https://api.resend.com/emails"
MAGIC_LINK_SUBJECT = "Job&Kill 로그인 링크"
# Operational records are retained briefly for troubleshooting, not as user-data policy.
TOKEN_OPERATIONAL_GRACE_DAYS = 1
UNVERIFIED_ACCOUNT_GRACE_DAYS = 7
AUTH_REQUEST_EVENT_RETENTION_DAYS = 1
MAX_DRAFTS_PER_USER = 50


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
    created_at: datetime


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


def _rate_limit_ip_value(value: Any) -> str:
    """Return a stable, non-secret rate-limit key for a client address.

    IPv6 privacy addresses commonly rotate their host bits, so valid IPv6
    addresses share a key at the /64 prefix boundary. IPv4-mapped IPv6 values
    remain host-specific to avoid grouping the entire mapped IPv4 space into a
    single IPv6 /64. Malformed values keep deterministic legacy-style identity
    behind an explicit prefix; only the HMAC of this value is persisted.
    """
    raw = str(value or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return f"invalid:{raw or '<empty>'}"
    if isinstance(address, ipaddress.IPv4Address):
        return f"ipv4:{address.compressed}"
    if address.ipv4_mapped is not None:
        return f"ipv4:{address.ipv4_mapped.compressed}"
    network = ipaddress.IPv6Network((address, 64), strict=False)
    return f"ipv6:{network.network_address.compressed}/64"


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
        "DELETE FROM auth_request_events WHERE created_at < ?",
        (iso(now - timedelta(days=AUTH_REQUEST_EVENT_RETENTION_DAYS)),),
    )


def public_url() -> str:
    configured = os.getenv("JOBNKILL_PUBLIC_URL", "").strip().rstrip("/")
    if not configured and environment() != "production":
        configured = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
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


def _mail_transport() -> str:
    transport = os.getenv("JOBNKILL_MAIL_TRANSPORT", "smtp").strip().lower()
    if transport not in {"smtp", "resend"}:
        raise AuthError("auth_config", "로그인 메일 전송 방식을 확인해 주세요.", 500)
    return transport


def _magic_link_body(link: str) -> str:
    return (
        "아래 링크로 Job&Kill에 로그인하세요. 링크는 15분 동안 한 번만 사용할 수 있습니다.\n\n"
        + link
        + "\n\n요청하지 않았다면 이 메일을 무시하세요."
    )


def _send_with_resend(email: str, link: str) -> None:
    api_key = os.getenv("JOBNKILL_RESEND_API_KEY", "").strip()
    sender = os.getenv("JOBNKILL_RESEND_FROM", "").strip()
    if not api_key or not sender:
        raise AuthError("mail_unavailable", "로그인 메일 설정이 완료되지 않았습니다.", 503)
    payload = json.dumps({
        "from": sender,
        "to": [email],
        "subject": MAGIC_LINK_SUBJECT,
        "text": _magic_link_body(link),
    }).encode("utf-8")
    request = urllib.request.Request(
        RESEND_EMAILS_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise OSError("Resend returned an unsuccessful response")
    except (OSError, urllib.error.URLError) as error:
        raise AuthError("mail_failed", "로그인 메일을 보내지 못했습니다.", 503) from error


def _send_magic_link(email: str, link: str) -> None:
    if environment() != "production" and os.getenv("JOBNKILL_AUTH_DEV_SHOW_LINK", "0") == "1":
        return
    if _mail_transport() == "resend":
        _send_with_resend(email, link)
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
    message["Subject"] = MAGIC_LINK_SUBJECT
    message["From"] = sender
    message["To"] = email
    message.set_content(_magic_link_body(link))
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


def request_magic_link(
    connection: Connection,
    raw_email: Any,
    request_ip: str,
    intent_token: str,
    before_commit: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Create a login token and commit any caller-provided audit work atomically.

    The callback runs after the user and token are staged but before their first
    commit. If it fails, the caller's connection context rolls back all staged
    account, token, rate-limit, and audit rows and no email is sent. A later
    email-delivery failure invalidates the committed token; the valid consent
    audit remains as a record of the accepted login request.
    """
    email = normalize_email(raw_email)
    if not 20 <= len(intent_token) <= 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", intent_token):
        raise AuthError("auth_config", "로그인 요청 보안값을 만들지 못했습니다.", 500)
    email_subject = _rate_digest("email", email)
    ip_subject = _rate_digest("ip", _rate_limit_ip_value(request_ip))
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
    token = secrets.token_urlsafe(32)
    token_hash = _digest(token)
    connection.execute(
        """
        INSERT INTO login_tokens(
          token_hash, user_id, expires_at, request_subject_hash, intent_hash
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            token_hash,
            user_id,
            iso(now + timedelta(minutes=MAGIC_MINUTES)),
            email_subject,
            _digest(intent_token),
        ),
    )
    if before_commit:
        before_commit(user_id, token_hash)
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


def verify_magic_link(
    connection: Connection,
    token: Any,
    intent_token: Any,
    after_verify: Callable[[str, str], None] | None = None,
) -> LoginResult:
    raw = str(token or "")
    if not 20 <= len(raw) <= 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise AuthError("invalid_token", "로그인 링크가 올바르지 않거나 만료되었습니다.", 401)
    intent = str(intent_token or "")
    if not 20 <= len(intent) <= 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", intent):
        raise AuthError(
            "login_intent_required",
            "로그인을 요청한 같은 브라우저에서 링크를 열어 주세요.",
            401,
        )
    now = utcnow()
    token_hash = _digest(raw)
    row = connection.execute(
        """
        UPDATE login_tokens SET used_at=?
        WHERE token_hash=? AND intent_hash=? AND used_at IS NULL AND expires_at>?
        RETURNING user_id
        """,
        (iso(now), token_hash, _digest(intent), iso(now)),
    ).fetchone()
    if not row:
        raise AuthError("invalid_token", "로그인 링크가 올바르지 않거나 만료되었습니다.", 401)
    user = connection.execute(
        "SELECT id, email FROM users WHERE id=? AND status='active'", (str(row["user_id"]),)
    ).fetchone()
    if not user:
        raise AuthError("invalid_token", "로그인 링크가 올바르지 않거나 만료되었습니다.", 401)
    if after_verify:
        after_verify(str(user["id"]), token_hash)
    # A public login request must not be able to invalidate a link that the
    # account owner already requested. Retire sibling links only after this
    # token, its browser intent, and its consent audit have all been verified.
    # The caller's connection context commits this together with the new
    # session, or rolls every change back if any later step fails.
    connection.execute(
        """
        UPDATE login_tokens SET used_at=?
        WHERE user_id=? AND token_hash<>? AND used_at IS NULL
        """,
        (iso(now), str(user["id"]), token_hash),
    )
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
        SELECT s.token_hash, s.user_id, s.csrf_hash, s.expires_at, s.idle_expires_at, s.created_at,
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
    return Session(
        str(row["user_id"]), str(row["email"]), token_hash, str(row["csrf_hash"]),
        _datetime(row["created_at"]),
    )


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


def login_intent_cookie_name() -> str:
    return "__Host-jobandkill_login_intent" if environment() == "production" else "jobandkill_login_intent"


def login_intent_cookie_header(intent_token: str) -> str:
    secure_part = "; Secure" if environment() == "production" else ""
    return (
        f"{login_intent_cookie_name()}={intent_token}; Path=/; HttpOnly; SameSite=Strict; "
        f"Max-Age={MAGIC_MINUTES * 60}{secure_part}"
    )


def clear_login_intent_cookie_header() -> str:
    secure_part = "; Secure" if environment() == "production" else ""
    return (
        f"{login_intent_cookie_name()}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        f"{secure_part}"
    )


def login_intent_from_cookie(header: str | None) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(header or "")
    except Exception:
        return ""
    morsel = cookie.get(login_intent_cookie_name())
    return morsel.value if morsel else ""


def _count(connection: Connection, statement: str, params: tuple[Any, ...]) -> int:
    return int(connection.execute(statement, params).fetchone()["count"])


def _delete_count(connection: Connection, statement: str, params: tuple[Any, ...]) -> int:
    return int(connection.execute(statement, params).rowcount)


def cleanup_personal_data(
    connection: Connection,
    retention_days: int,
    *,
    execute: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Count or atomically delete personal data that has crossed its retention boundary.

    Verified active accounts are never selected: inactivity is not an account-deletion
    signal. An old unverified account is selected only after it has no drafts (including
    drafts removed by this run) and no live, unused login token.
    """
    if retention_days <= 0:
        raise ValueError("draft retention days must be a positive integer")
    moment = now or utcnow()
    draft_cutoff = iso(moment - timedelta(days=retention_days))
    token_cutoff = iso(moment - timedelta(days=TOKEN_OPERATIONAL_GRACE_DAYS))
    rate_event_cutoff = iso(moment - timedelta(days=AUTH_REQUEST_EVENT_RETENTION_DAYS))
    account_cutoff = iso(moment - timedelta(days=UNVERIFIED_ACCOUNT_GRACE_DAYS))
    current = iso(moment)
    result: dict[str, int] = {
        "drafts": 0,
        "login_tokens": 0,
        "sessions": 0,
        "pending_consents": 0,
        "auth_request_events": 0,
        "unverified_accounts": 0,
    }

    if connection.dialect == "postgres":
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("jobandkill:personal-data-cleanup",))
    elif not connection.raw.in_transaction:
        connection.execute("BEGIN IMMEDIATE")

    if not execute:
        result["drafts"] = _count(
            connection, "SELECT COUNT(*) AS count FROM user_drafts WHERE updated_at < ?", (draft_cutoff,)
        )
        result["login_tokens"] = _count(
            connection,
            """
            SELECT COUNT(*) AS count FROM login_tokens
            WHERE expires_at < ? OR (used_at IS NOT NULL AND used_at < ?)
            """,
            (token_cutoff, token_cutoff),
        )
        result["sessions"] = _count(
            connection,
            """
            SELECT COUNT(*) AS count FROM sessions
            WHERE expires_at <= ? OR idle_expires_at <= ? OR revoked_at IS NOT NULL
            """,
            (current, current),
        )
        result["pending_consents"] = _count(
            connection,
            """
            SELECT COUNT(*) AS count FROM user_consents
            WHERE verified_at IS NULL AND accepted_at < ?
            """,
            (token_cutoff,),
        )
        result["auth_request_events"] = _count(
            connection,
            "SELECT COUNT(*) AS count FROM auth_request_events WHERE created_at < ?",
            (rate_event_cutoff,),
        )
        result["unverified_accounts"] = _count(
            connection,
            """
            SELECT COUNT(*) AS count FROM users
            WHERE users.email_verified_at IS NULL AND users.created_at < ?
              AND NOT EXISTS (
                SELECT 1 FROM user_drafts d
                WHERE d.user_id=users.id AND d.updated_at >= ?
              )
              AND NOT EXISTS (
                SELECT 1 FROM login_tokens t
                WHERE t.user_id=users.id AND t.used_at IS NULL AND t.expires_at > ?
              )
            """,
            (account_cutoff, draft_cutoff, current),
        )
        return result

    result["drafts"] = _delete_count(
        connection, "DELETE FROM user_drafts WHERE updated_at < ?", (draft_cutoff,)
    )
    result["login_tokens"] = _delete_count(
        connection,
        """
        DELETE FROM login_tokens
        WHERE expires_at < ? OR (used_at IS NOT NULL AND used_at < ?)
        """,
        (token_cutoff, token_cutoff),
    )
    result["sessions"] = _delete_count(
        connection,
        """
        DELETE FROM sessions
        WHERE expires_at <= ? OR idle_expires_at <= ? OR revoked_at IS NOT NULL
        """,
        (current, current),
    )
    result["pending_consents"] = _delete_count(
        connection,
        """
        DELETE FROM user_consents
        WHERE verified_at IS NULL AND accepted_at < ?
        """,
        (token_cutoff,),
    )
    result["auth_request_events"] = _delete_count(
        connection,
        "DELETE FROM auth_request_events WHERE created_at < ?",
        (rate_event_cutoff,),
    )
    result["unverified_accounts"] = _delete_count(
        connection,
        """
        DELETE FROM users
        WHERE users.email_verified_at IS NULL AND users.created_at < ?
          AND NOT EXISTS (SELECT 1 FROM user_drafts d WHERE d.user_id=users.id)
          AND NOT EXISTS (
            SELECT 1 FROM login_tokens t
            WHERE t.user_id=users.id AND t.used_at IS NULL AND t.expires_at > ?
          )
        """,
        (account_cutoff, current),
    )
    return result


def export_user_data(connection: Connection, session: Session) -> dict[str, Any]:
    """Return only the requesting user's portable account data."""
    user = connection.execute(
        """
        SELECT email, email_verified_at, last_login_at, created_at, updated_at
        FROM users WHERE id=?
        """,
        (session.user_id,),
    ).fetchone()
    if not user:
        raise AuthError("authentication_required", "로그인이 필요합니다.", 401)
    drafts = connection.execute(
        "SELECT * FROM user_drafts WHERE user_id=? ORDER BY created_at ASC",
        (session.user_id,),
    ).fetchall()
    consents = connection.execute(
        """
        SELECT consent_type, policy_version, notice_url, notice_sha256,
               accepted_at, verified_at
        FROM user_consents WHERE user_id=? ORDER BY accepted_at ASC, id ASC
        """,
        (session.user_id,),
    ).fetchall()
    return {
        "account": {
            "email": str(user["email"]),
            "email_verified_at": user["email_verified_at"],
            "last_login_at": user["last_login_at"],
            "created_at": user["created_at"],
            "updated_at": user["updated_at"],
        },
        "consents": [dict(consent) for consent in consents],
        "drafts": [_draft_response(draft) for draft in drafts],
    }


def delete_account(connection: Connection, session: Session, confirmation: Any) -> None:
    """Permanently delete an account after an explicit recent-auth confirmation."""
    if not isinstance(confirmation, str) or confirmation != ACCOUNT_DELETE_CONFIRMATION:
        raise AuthError("delete_confirmation", "계정을 삭제하려면 확인 문구를 정확히 입력해 주세요.", 422)
    if session.created_at < utcnow() - timedelta(minutes=ACCOUNT_DELETE_REAUTH_MINUTES):
        raise AuthError("recent_auth_required", "계정 삭제 전 다시 로그인해 주세요.", 403)
    deleted = connection.execute("DELETE FROM users WHERE id=?", (session.user_id,))
    if deleted.rowcount != 1:
        raise AuthError("authentication_required", "로그인이 필요합니다.", 401)


def _draft_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AuthError("invalid_draft", "초안 데이터 형식이 올바르지 않습니다.", 422)
    try:
        return sanitize_draft(raw)
    except DraftValidationError as error:
        raise AuthError("invalid_draft", "초안 입력값을 확인해 주세요.", 422) from error


def _draft_title(request: dict[str, Any], payload: dict[str, Any]) -> str:
    title = " ".join(
        str(request.get("title") or payload.get("experience_title") or "제목 없는 초안").split()
    )
    if len(title) > 300:
        raise AuthError("invalid_title", "초안 제목은 300자 이하여야 합니다.", 422)
    if contains_resident_registration_number(title):
        raise AuthError("invalid_title", "초안 제목에 주민등록번호를 입력할 수 없습니다.", 422)
    return title


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
    title = _draft_title(request, payload)
    if connection.dialect == "postgres":
        connection.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (session.user_id,))
    elif not connection.raw.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    draft_count = int(connection.execute(
        "SELECT COUNT(*) AS count FROM user_drafts WHERE user_id=?", (session.user_id,)
    ).fetchone()["count"])
    if draft_count >= MAX_DRAFTS_PER_USER:
        raise AuthError(
            "draft_limit",
            f"계정에는 초안을 최대 {MAX_DRAFTS_PER_USER}개 저장할 수 있습니다. 기존 초안을 정리해 주세요.",
            409,
        )
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
    title = _draft_title(request, payload)
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


def delete_all_drafts(connection: Connection, session: Session) -> int:
    """Delete every server-side draft owned by the requesting user."""
    cursor = connection.execute("DELETE FROM user_drafts WHERE user_id=?", (session.user_id,))
    return int(cursor.rowcount)
