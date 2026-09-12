from __future__ import annotations

import importlib.util
import os
from typing import Any
from urllib.parse import urlsplit

from .auth import environment
from .db import (
    POSTGRES_TLS_MODES,
    postgres_sslmode,
    render_private_postgres_url,
    validate_production_database_target,
)
from .privacy import privacy_configuration_check


def _check(name: str, ready: bool, message: str) -> dict[str, Any]:
    return {"name": name, "ready": ready, "message": message}


def draft_retention_days(production: bool | None = None) -> int:
    """Return the configured draft retention period without guessing in production."""
    is_production = environment() == "production" if production is None else production
    raw = os.getenv("JOBNKILL_DRAFT_RETENTION_DAYS", "").strip()
    if not raw:
        if is_production:
            raise RuntimeError("운영에는 JOBNKILL_DRAFT_RETENTION_DAYS를 설정해야 합니다.")
        return 30
    try:
        days = int(raw)
    except ValueError as error:
        raise RuntimeError("JOBNKILL_DRAFT_RETENTION_DAYS는 양의 정수여야 합니다.") from error
    if days <= 0 or str(days) != raw:
        raise RuntimeError("JOBNKILL_DRAFT_RETENTION_DAYS는 양의 정수여야 합니다.")
    return days


def configuration_report(
    production: bool = False,
    require_api: bool = False,
    collector_only: bool = False,
    require_storage: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    database_url = (
        os.getenv("JOBNKILL_DATABASE_URL", "").strip()
        if production
        else os.getenv("JOBNKILL_DATABASE_URL", os.getenv("DATABASE_URL", "")).strip()
    )
    postgres = database_url.startswith(("postgresql://", "postgres://"))
    try:
        sslmode = postgres_sslmode(database_url) if postgres else ""
        render_private_database = bool(
            production
            and postgres
            and render_private_postgres_url(database_url)
        )
    except ValueError:
        sslmode = ""
        render_private_database = False
    tls_ready = not production or sslmode in POSTGRES_TLS_MODES
    role_ready = True
    role_message = ""
    if production:
        expected_role = "collector" if collector_only else "web"
        try:
            validate_production_database_target(
                database_url, expected_role=expected_role,
            )
        except RuntimeError as error:
            role_ready = False
            role_message = str(error)
    database_ready = (
        postgres and tls_ready and role_ready
        if production else (postgres or not database_url)
    )
    checks.append(_check(
        "database",
        database_ready,
        role_message if production and not role_ready else (
            (
                "Render 비공개 PostgreSQL TLS 연결 설정됨"
                if render_private_database and tls_ready
                else (
                    "PostgreSQL TLS 설정됨"
                    if tls_ready else "PostgreSQL sslmode=require 이상 필요"
                )
            ) if postgres
            else ("SQLite 개발 모드" if not production else "운영 PostgreSQL URL 필요")
        ),
    ))
    if production:
        auto_migrate = os.getenv("JOBNKILL_AUTO_MIGRATE", "").strip()
        migrations_ready = auto_migrate == "0"
        checks.append(_check(
            "automatic_migrations",
            migrations_ready,
            (
                "운영 자동 마이그레이션 명시적으로 비활성화됨"
                if migrations_ready
                else "운영에는 JOBNKILL_AUTO_MIGRATE=0을 명시해야 함"
            ),
        ))
    if postgres:
        checks.append(_check(
            "postgres_driver", importlib.util.find_spec("psycopg") is not None,
            "psycopg 설치됨" if importlib.util.find_spec("psycopg") else "운영 의존성 psycopg 필요",
        ))

    if require_storage:
        backend = os.getenv("JOBNKILL_STORAGE_BACKEND", "local").strip().lower()
        storage_ready = backend == "s3" and bool(os.getenv("JOBNKILL_S3_BUCKET", "").strip()) if production else backend in {"local", "s3"}
        checks.append(_check(
            "document_storage", storage_ready,
            "S3 비공개 저장소 설정됨" if backend == "s3" and storage_ready else (
                "로컬 개발 저장소" if backend == "local" and not production else "운영 S3 버킷 설정 필요"
            ),
        ))
        if backend == "s3":
            checks.append(_check(
                "s3_driver", importlib.util.find_spec("boto3") is not None,
                "boto3 설치됨" if importlib.util.find_spec("boto3") else "운영 의존성 boto3 필요",
            ))

    if not collector_only:
        public = os.getenv("JOBNKILL_PUBLIC_URL", "").strip()
        if not public and not production:
            public = os.getenv("RENDER_EXTERNAL_URL", "").strip()
        parsed = urlsplit(public) if public else None
        public_ready = bool(parsed and parsed.hostname and parsed.scheme == "https") if production else True
        checks.append(_check("public_url", public_ready, "HTTPS 공개 주소 설정됨" if public_ready and public else (
            "개발 기본 주소 사용" if not production else "운영 HTTPS 공개 주소 필요"
        )))
        privacy_ready, privacy_message = privacy_configuration_check(production)
        checks.append(_check("privacy_notices", privacy_ready, privacy_message))
        rate_ready = bool(os.getenv("JOBNKILL_AUTH_RATE_SECRET", "").strip()) if production else True
        checks.append(_check("auth_rate_limit", rate_ready, "인증 속도 제한 키 설정됨" if rate_ready and production else (
            "개발용 제한 사용" if not production else "인증 속도 제한 키 필요"
        )))
        transport = os.getenv("JOBNKILL_MAIL_TRANSPORT", "smtp").strip().lower()
        if transport == "resend":
            mail_ready = bool(
                os.getenv("JOBNKILL_RESEND_API_KEY", "").strip()
                and os.getenv("JOBNKILL_RESEND_FROM", "").strip()
            )
            mail_message = "Resend API 키와 발신 주소 설정됨" if mail_ready else "Resend API 키와 발신 주소 필요"
        elif transport == "smtp":
            smtp_security = os.getenv("JOBNKILL_SMTP_SECURITY", "starttls").strip().lower()
            mail_ready = bool(
                os.getenv("JOBNKILL_SMTP_HOST", "").strip() and os.getenv("JOBNKILL_SMTP_FROM", "").strip()
                and (not production or smtp_security in {"starttls", "ssl"})
            )
            mail_message = "로그인 메일 전송 설정됨" if mail_ready else "암호화된 SMTP 호스트·발신 주소 필요"
        else:
            mail_ready = False
            mail_message = "JOBNKILL_MAIL_TRANSPORT는 smtp 또는 resend여야 함"
        if (
            transport in {"smtp", "resend"}
            and not production
            and os.getenv("JOBNKILL_AUTH_DEV_SHOW_LINK", "0") == "1"
        ):
            mail_ready = True
            mail_message = "개발용 로그인 링크 표시 사용"
        checks.append(_check("login_mail", mail_ready, mail_message))
    if not collector_only:
        try:
            draft_retention_days(production)
            retention_ready = True
        except RuntimeError:
            retention_ready = False
        checks.append(_check(
            "draft_retention",
            retention_ready,
            "초안 보존 기간 설정됨" if retention_ready else "JOBNKILL_DRAFT_RETENTION_DAYS 양의 정수 필요",
        ))

    if require_api:
        template = os.getenv("JOBNKILL_ALIO_API_URL_TEMPLATE", "").strip()
        key_present = bool(os.getenv("JOBNKILL_ALIO_SERVICE_KEY", "").strip())
        placeholders = all("{" + item + "}" in template for item in ("service_key", "page", "page_size"))
        parsed_api = urlsplit(template) if template else None
        api_ready = bool(
            key_present and placeholders and parsed_api and parsed_api.scheme == "https" and
            parsed_api.hostname and (
                parsed_api.hostname == "data.go.kr" or parsed_api.hostname.endswith(".data.go.kr")
            )
        )
        checks.append(_check(
            "official_api", api_ready,
            "공식 API 키와 URL 템플릿 설정됨" if api_ready else "공식 API 승인 키와 정확한 URL 템플릿 필요",
        ))

    return {
        "ready": all(item["ready"] for item in checks),
        "environment": "production" if production else environment(),
        "profile": "collector" if collector_only else "web",
        "checks": checks,
        "secrets_redacted": True,
    }


def require_production_settings() -> None:
    report = configuration_report(production=True, require_storage=False)
    missing = [item["name"] for item in report["checks"] if not item["ready"]]
    if missing:
        raise RuntimeError("운영 필수 설정이 준비되지 않았습니다: " + ", ".join(missing))
