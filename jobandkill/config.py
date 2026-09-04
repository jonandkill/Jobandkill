from __future__ import annotations

import importlib.util
import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import environment


def _check(name: str, ready: bool, message: str) -> dict[str, Any]:
    return {"name": name, "ready": ready, "message": message}


def configuration_report(
    production: bool = False, require_api: bool = False, collector_only: bool = False
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    database_url = os.getenv("JOBNKILL_DATABASE_URL", os.getenv("DATABASE_URL", "")).strip()
    postgres = database_url.startswith(("postgresql://", "postgres://"))
    sslmode = parse_qs(urlsplit(database_url).query).get("sslmode", [os.getenv("PGSSLMODE", "")])[-1] if postgres else ""
    tls_ready = not production or sslmode in {"require", "verify-ca", "verify-full"}
    database_ready = postgres and tls_ready if production else (postgres or not database_url)
    checks.append(_check(
        "database",
        database_ready,
        ("PostgreSQL TLS 설정됨" if tls_ready else "PostgreSQL sslmode=require 이상 필요") if postgres
        else ("SQLite 개발 모드" if not production else "운영 PostgreSQL URL 필요"),
    ))
    if postgres:
        checks.append(_check(
            "postgres_driver", importlib.util.find_spec("psycopg") is not None,
            "psycopg 설치됨" if importlib.util.find_spec("psycopg") else "운영 의존성 psycopg 필요",
        ))

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
        parsed = urlsplit(public) if public else None
        public_ready = bool(parsed and parsed.hostname and parsed.scheme == "https") if production else True
        checks.append(_check("public_url", public_ready, "HTTPS 공개 주소 설정됨" if public_ready and public else (
            "개발 기본 주소 사용" if not production else "운영 HTTPS 공개 주소 필요"
        )))
        rate_ready = bool(os.getenv("JOBNKILL_AUTH_RATE_SECRET", "").strip()) if production else True
        checks.append(_check("auth_rate_limit", rate_ready, "인증 속도 제한 키 설정됨" if rate_ready and production else (
            "개발용 제한 사용" if not production else "인증 속도 제한 키 필요"
        )))
        smtp_security = os.getenv("JOBNKILL_SMTP_SECURITY", "starttls").strip().lower()
        smtp_ready = bool(
            os.getenv("JOBNKILL_SMTP_HOST", "").strip() and os.getenv("JOBNKILL_SMTP_FROM", "").strip()
            and (not production or smtp_security in {"starttls", "ssl"})
        )
        if not production and os.getenv("JOBNKILL_AUTH_DEV_SHOW_LINK", "0") == "1":
            smtp_ready = True
        checks.append(_check("login_mail", smtp_ready, "로그인 메일 전송 설정됨" if smtp_ready else "암호화된 SMTP 호스트·발신 주소 필요"))

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
    report = configuration_report(production=True)
    missing = [item["name"] for item in report["checks"] if not item["ready"]]
    if missing:
        raise RuntimeError("운영 필수 설정이 준비되지 않았습니다: " + ", ".join(missing))
