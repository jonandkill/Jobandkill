"""Privacy notice configuration and login-consent audit helpers.

This module intentionally exposes only public notice URLs and versions.  It does
not infer an operator, recipient, country, or vendor from deployment settings.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .db import Connection


class PrivacyError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ConsentRequirement:
    consent_type: str
    version: str
    notice_url: str
    notice_sha256: str


@dataclass(frozen=True)
class PrivacyConfig:
    policy_url: str
    policy_version: str
    policy_sha256: str
    overseas_transfer_required: bool
    overseas_transfer_url: str
    overseas_transfer_version: str
    overseas_transfer_sha256: str
    required: bool


def _environment() -> str:
    return os.getenv("JOBNKILL_ENV", "development").strip().lower()


def _flag(name: str, *, required: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        if required:
            raise PrivacyError(
                "privacy_config", f"운영에는 {name}을 0 또는 1로 명시해야 합니다.", 500
            )
        value = "0"
    else:
        value = raw.strip()
    if value not in {"0", "1"}:
        raise PrivacyError("privacy_config", f"{name} 설정은 0 또는 1이어야 합니다.", 500)
    return value == "1"


def _public_url(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PrivacyError("privacy_config", f"{name}은 HTTPS 공개 URL이어야 합니다.", 500)
    return value


def _notice_sha256(name: str, *, required: bool) -> str:
    value = os.getenv(name, "").strip().lower()
    if not value and not required:
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PrivacyError(
            "privacy_config", f"{name}은 공개 안내문 본문의 SHA-256 64자리여야 합니다.", 500
        )
    return value


def _read_privacy_config(required: bool) -> PrivacyConfig:
    policy_url = _public_url("JOBNKILL_PRIVACY_POLICY_URL")
    policy_version = os.getenv("JOBNKILL_PRIVACY_POLICY_VERSION", "").strip()
    policy_sha256 = _notice_sha256("JOBNKILL_PRIVACY_POLICY_SHA256", required=required)
    overseas_required = _flag("JOBNKILL_OVERSEAS_TRANSFER_REQUIRED", required=required)
    overseas_url = _public_url("JOBNKILL_OVERSEAS_TRANSFER_POLICY_URL")
    overseas_version = os.getenv("JOBNKILL_OVERSEAS_TRANSFER_VERSION", "").strip()
    overseas_sha256 = _notice_sha256(
        "JOBNKILL_OVERSEAS_TRANSFER_SHA256", required=required and overseas_required
    )

    if required and (not policy_url or not policy_version):
        raise PrivacyError(
            "privacy_config",
            "운영 로그인에는 개인정보 처리방침 URL과 버전 설정이 필요합니다.",
            500,
        )
    if required and overseas_required and (not overseas_url or not overseas_version):
        raise PrivacyError(
            "privacy_config",
            "운영 국외 이전 동의에는 안내 URL과 버전 설정이 필요합니다.",
            500,
        )
    return PrivacyConfig(
        policy_url,
        policy_version,
        policy_sha256,
        overseas_required,
        overseas_url,
        overseas_version,
        overseas_sha256,
        required,
    )


def privacy_config() -> PrivacyConfig:
    """Read the public, deployment-provided notice configuration.

    Consent is enforced for login requests only in production. Development and
    test installs stay usable without pretending that placeholder text is a
    deployed policy.
    """
    return _read_privacy_config(_environment() == "production")


def privacy_configuration_check(production: bool) -> tuple[bool, str]:
    """Return startup-safe readiness without exposing notice configuration."""
    try:
        config = _read_privacy_config(production)
    except PrivacyError as error:
        return False, str(error)
    if not production:
        return True, "개발 환경에서는 로그인 동의 강제를 사용하지 않음"
    if config.overseas_transfer_required:
        return True, "개인정보 처리방침과 국외 이전 안내 설정됨"
    return True, "개인정보 처리방침 안내 설정됨"


def public_privacy_config() -> dict[str, Any]:
    config = privacy_config()
    return {
        "privacy_policy": {
            "required": config.required,
            "url": config.policy_url,
            "version": config.policy_version,
        },
        "overseas_transfer": {
            "required": config.required and config.overseas_transfer_required,
            "url": config.overseas_transfer_url,
            "version": config.overseas_transfer_version,
        },
    }


def login_consents(payload: dict[str, Any]) -> list[ConsentRequirement]:
    """Validate explicit, version-bound consent before account creation."""
    config = privacy_config()
    if not config.required:
        return []
    requirements = [
        ("privacy_policy", "privacy_policy_accepted", "privacy_policy_version", config.policy_version,
         config.policy_url, config.policy_sha256,
         "개인정보 처리방침을 확인하고 동의해 주세요."),
    ]
    if config.overseas_transfer_required:
        requirements.append(
            ("overseas_transfer", "overseas_transfer_accepted", "overseas_transfer_version",
             config.overseas_transfer_version, config.overseas_transfer_url,
             config.overseas_transfer_sha256, "국외 이전 안내를 확인하고 동의해 주세요.")
        )
    accepted: list[ConsentRequirement] = []
    for consent_type, accepted_key, version_key, version, notice_url, notice_sha256, message in requirements:
        if payload.get(accepted_key) is not True:
            raise PrivacyError("consent_required", message, 422)
        if payload.get(version_key) != version:
            raise PrivacyError("consent_version", "최신 안내를 다시 확인하고 동의해 주세요.", 422)
        accepted.append(ConsentRequirement(consent_type, version, notice_url, notice_sha256))
    return accepted


def record_login_consents(
    connection: Connection,
    user_id: str,
    request_token_hash: str,
    consents: list[ConsentRequirement],
) -> None:
    """Stage IP-free consent records pending proof of email ownership."""
    if not consents:
        return
    metadata = json.dumps({"channel": "login_request"}, separators=(",", ":"))
    for consent in consents:
        connection.execute(
            """
            INSERT INTO user_consents(
              user_id, consent_type, policy_version, notice_url, notice_sha256,
              request_token_hash, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                consent.consent_type,
                consent.version,
                consent.notice_url,
                consent.notice_sha256,
                request_token_hash,
                metadata,
            ),
        )


def verify_login_consents(connection: Connection, user_id: str, request_token_hash: str) -> None:
    """Mark only the consents attached to the verified one-time link as confirmed."""
    connection.execute(
        """
        UPDATE user_consents SET verified_at=CURRENT_TIMESTAMP
        WHERE user_id=? AND request_token_hash=? AND verified_at IS NULL
        """,
        (user_id, request_token_hash),
    )
    require_current_consents(connection, user_id)


def require_current_consents(connection: Connection, user_id: str) -> None:
    """Block new server-side draft writes after a notice identity changes."""
    config = privacy_config()
    if not config.required:
        return
    required = {(
        "privacy_policy",
        config.policy_version,
        config.policy_url,
        config.policy_sha256,
    )}
    if config.overseas_transfer_required:
        required.add((
            "overseas_transfer",
            config.overseas_transfer_version,
            config.overseas_transfer_url,
            config.overseas_transfer_sha256,
        ))
    rows = connection.execute(
        """
        SELECT consent_type, policy_version, notice_url, notice_sha256
        FROM user_consents
        WHERE user_id=? AND verified_at IS NOT NULL
        """,
        (user_id,),
    ).fetchall()
    verified = {
        (
            str(row["consent_type"]),
            str(row["policy_version"]),
            str(row["notice_url"]),
            str(row["notice_sha256"]),
        )
        for row in rows
    }
    if not required <= verified:
        raise PrivacyError(
            "consent_refresh_required",
            "개인정보 안내가 변경되었습니다. 새 안내를 확인한 뒤 다시 로그인해 주세요.",
            403,
        )
