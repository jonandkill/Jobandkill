from __future__ import annotations

import os
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .db import ROOT


@dataclass(frozen=True)
class StoredDocument:
    backend: str
    key: str
    display_path: str


class DocumentStore(Protocol):
    backend: str
    location_id: str

    def key_for(self, digest: str, suffix: str) -> str: ...

    def put(self, payload: bytes, digest: str, suffix: str, content_type: str) -> StoredDocument: ...

    def delete(self, key: str) -> None: ...


def _safe_suffix(suffix: str) -> str:
    normalized = suffix.lower()
    return normalized if re.fullmatch(r"\.[a-z0-9]{1,8}", normalized) else ".bin"


def object_key(digest: str, suffix: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("문서 SHA-256 형식이 올바르지 않습니다.")
    return f"{digest[:2]}/{digest}{_safe_suffix(suffix)}"


def document_directory() -> Path:
    configured = Path(os.getenv("JOBNKILL_DOCUMENT_DIR", "data/documents"))
    directory = configured if configured.is_absolute() else ROOT / configured
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


class LocalDocumentStore:
    backend = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or document_directory()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.location_id = "local:" + hashlib.sha256(str(self.root).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root not in candidate.parents:
            raise ValueError("허용되지 않은 문서 저장 키입니다.")
        return candidate

    def key_for(self, digest: str, suffix: str) -> str:
        return object_key(digest, suffix)

    def put(self, payload: bytes, digest: str, suffix: str, content_type: str) -> StoredDocument:
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != digest:
            raise ValueError("문서 내용과 SHA-256 값이 일치하지 않습니다.")
        key = self.key_for(digest, suffix)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_is_valid = path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        staging = path.with_suffix(path.suffix + ".staging")
        if not existing_is_valid:
            # The deterministic sibling is covered by the durable final-key
            # upload intent, so a crash can be reconciled by delete(key).
            staging.write_bytes(payload)
            os.replace(staging, path)
        elif staging.exists():
            staging.unlink()
        display = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        return StoredDocument(self.backend, key, display)

    def delete(self, key: str) -> None:
        path = self._path(key)
        staging = path.with_suffix(path.suffix + ".staging")
        if staging.is_file():
            staging.unlink()
        if path.is_file():
            path.unlink()


class S3DocumentStore:
    backend = "s3"

    def __init__(self) -> None:
        self.bucket = os.getenv("JOBNKILL_S3_BUCKET", "").strip()
        self.prefix = (os.getenv("JOBNKILL_S3_PREFIX", "").strip("/") or "documents")
        endpoint = os.getenv("JOBNKILL_S3_ENDPOINT_URL", "").strip() or None
        if not self.bucket:
            raise ValueError("S3 저장에는 JOBNKILL_S3_BUCKET이 필요합니다.")
        if any(part in {"", ".", ".."} for part in self.prefix.split("/")):
            raise ValueError("S3 문서 prefix 형식이 올바르지 않습니다.")
        if endpoint:
            parsed_endpoint = urlsplit(endpoint)
            valid_endpoint = bool(
                parsed_endpoint.hostname
                and not parsed_endpoint.username
                and not parsed_endpoint.password
                and not parsed_endpoint.query
                and not parsed_endpoint.fragment
            )
            is_https = valid_endpoint and parsed_endpoint.scheme == "https"
            is_local_dev = (
                valid_endpoint
                and os.getenv("JOBNKILL_ENV", "development") == "development"
                and parsed_endpoint.scheme == "http"
                and parsed_endpoint.hostname in {"127.0.0.1", "::1", "localhost"}
            )
            if not (is_https or is_local_dev):
                raise ValueError("S3 엔드포인트는 HTTPS여야 합니다.")
        try:
            import boto3
        except ImportError as error:
            raise RuntimeError("S3 운영에는 requirements-production.txt의 boto3가 필요합니다.") from error
        kwargs: dict[str, str] = {}
        region = os.getenv("JOBNKILL_S3_REGION", "").strip()
        namespace = "|".join((self.bucket, endpoint or "aws-default", region or "default-region"))
        self.location_id = "s3:" + hashlib.sha256(namespace.encode()).hexdigest()
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        self.client = boto3.client("s3", **kwargs)
        try:
            versioning = self.client.get_bucket_versioning(Bucket=self.bucket)
        except Exception as error:
            raise RuntimeError(
                "S3 버킷 버전 관리 상태를 확인할 권한과 연결이 필요합니다."
            ) from error
        if versioning.get("Status") in {"Enabled", "Suspended"}:
            raise ValueError(
                "원문을 완전히 삭제할 수 있도록 전용 S3 버킷의 버전 관리를 비활성화해야 합니다."
            )

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def key_for(self, digest: str, suffix: str) -> str:
        return self._full_key(object_key(digest, suffix))

    def put(self, payload: bytes, digest: str, suffix: str, content_type: str) -> StoredDocument:
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("문서 내용과 SHA-256 값이 일치하지 않습니다.")
        full_key = self.key_for(digest, suffix)
        encryption = os.getenv("JOBNKILL_S3_ENCRYPTION", "AES256").strip()
        if encryption not in {"AES256", "aws:kms"}:
            raise ValueError("S3 암호화 방식은 AES256 또는 aws:kms여야 합니다.")
        arguments = {
            "Bucket": self.bucket,
            "Key": full_key,
            "Body": payload,
            "ContentType": content_type.split(";", 1)[0] or "application/octet-stream",
            "Metadata": {"sha256": digest},
            "ServerSideEncryption": encryption,
        }
        kms_key = os.getenv("JOBNKILL_S3_KMS_KEY_ID", "").strip()
        if kms_key:
            arguments["ServerSideEncryption"] = "aws:kms"
            arguments["SSEKMSKeyId"] = kms_key
        self.client.put_object(**arguments)
        return StoredDocument(self.backend, full_key, f"s3://{self.bucket}/{full_key}")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_document_store(backend: str | None = None) -> DocumentStore:
    selected = (backend or os.getenv("JOBNKILL_STORAGE_BACKEND", "local")).strip().lower()
    if selected == "local":
        return LocalDocumentStore()
    if selected == "s3":
        return S3DocumentStore()
    raise ValueError("JOBNKILL_STORAGE_BACKEND은 local 또는 s3여야 합니다.")
