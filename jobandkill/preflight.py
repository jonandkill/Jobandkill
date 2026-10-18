"""Read-only diagnostics for the GitHub production collector. Never print values."""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
from string import Formatter
from urllib.parse import parse_qs, quote, urlsplit

from .storage import validate_s3_bucket, validate_s3_endpoint


REQUIRED_SECRETS = {
    "JOBNKILL_DATABASE_URL": "JOBNKILL_DATABASE_URL",
    "JOBNKILL_S3_BUCKET": "JOBNKILL_S3_BUCKET",
    "JOBNKILL_S3_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
    "JOBNKILL_S3_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
    "JOBNKILL_ALIO_API_URL_TEMPLATE": "JOBNKILL_ALIO_API_URL_TEMPLATE",
    "JOBNKILL_ALIO_SERVICE_KEY": "JOBNKILL_ALIO_SERVICE_KEY",
}


def validate_api_template(template: str) -> bool:
    try:
        fields = list(Formatter().parse(template))
        if {field for _, field, _, _ in fields if field is not None} != {"service_key", "page", "page_size"}:
            return False
        if any(spec or conversion for _, _, spec, conversion in fields):
            return False
        parsed = urlsplit(template)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        official_list = (
            parsed.hostname == "apis.data.go.kr"
            and parsed.path == "/1051000/recruitment/list"
            and query.get("serviceKey") == ["{service_key}"]
            and query.get("pageNo") == ["{page}"]
            and query.get("numOfRows") == ["{page_size}"]
            and query.get("resultType", ["json"]) in (["json"], ["xml"])
            and "type" not in query
        )
        return bool(
            official_list and parsed.scheme == "https" and parsed.hostname
            and (parsed.hostname == "data.go.kr" or parsed.hostname.endswith(".data.go.kr"))
            and parsed.port in {None, 443}
            and not parsed.username and not parsed.password and not parsed.fragment
            and parsed.path not in {"", "/"} and "..." not in template
            and "{" not in parsed.netloc and "{" not in parsed.path
            and not any(c.isspace() for c in template)
            and all("{" + key + "}" in parsed.query for key in ("service_key", "page", "page_size"))
        )
    except (ValueError, TypeError):
        return False


def validate_database_url(database: str) -> bool:
    try:
        parsed = urlsplit(database)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        # libpq query options override URI components. Validate the actual
        # target, rejecting ambiguous TLS and alternate connection routing.
        if any(len(v) != 1 for v in query.values()) or set(query) & {
            "host", "hostaddr", "port", "dbname", "service", "servicefile",
        }:
            return False
        host = parsed.hostname or ""
        try:
            public_host = ipaddress.ip_address(host).is_global
        except ValueError:
            public_host = "." in host and not host.endswith((".localhost", ".local", ".internal"))
        return bool(
            parsed.scheme in {"postgresql", "postgres"} and public_host
            and parsed.path.strip("/") and parsed.port != 0 and not parsed.fragment
            and query.get("sslmode") in (["require"], ["verify-ca"], ["verify-full"])
            and not any(c.isspace() for c in database)
        )
    except (ValueError, TypeError):
        return False


def _database_probe() -> None:
    import psycopg
    # No schema initialization, DDL, data import, or writes. DSN never logged.
    with psycopg.connect(
        os.environ["JOBNKILL_DATABASE_URL"].strip(), connect_timeout=10,
        options="-c default_transaction_read_only=on -c statement_timeout=10000",
    ) as connection:
        if connection.execute("SHOW transaction_read_only").fetchone() != ("on",):
            raise RuntimeError("DB read-only transaction not established")
        if connection.execute("SELECT 1").fetchone() != (1,):
            raise RuntimeError("DB query contract not verified")


def _storage_probe() -> None:
    from .storage import S3DocumentStore
    # Constructor only reads GetBucketVersioning; rejects Enabled/Suspended.
    S3DocumentStore()


def _api_probe() -> None:
    from urllib.request import Request
    from .ingest import _fetch_api_response, normalize_api_record, parse_api_payload
    url = os.environ["JOBNKILL_ALIO_API_URL_TEMPLATE"].strip().format(
        service_key=quote(os.environ["JOBNKILL_ALIO_SERVICE_KEY"].strip(), safe="%"),
        page=1, page_size=1,
    )
    payload, content_type = _fetch_api_response(Request(url, headers={"Accept": "application/json, application/xml"}))
    if len(payload) > 10 * 1024 * 1024:
        raise ValueError("API response too large")
    records, total = parse_api_payload(payload, content_type, require_contract=True)
    if total is None or len(records) > 1 or (not records and total != 0):
        raise ValueError("API response contract not verified")
    if any(normalize_api_record(record) is None for record in records):
        raise ValueError("API required posting fields not verified")


def report(check_connections: bool = False) -> dict:
    checks = []

    def add(name, ready, message):
        checks.append({"name": name, "ready": bool(ready), "message": message})

    for secret, env in REQUIRED_SECRETS.items():
        present = bool(os.getenv(env, "").strip())
        add(secret, present, "전달됨 (유효성·승인 미확인)" if present else "production Environment secret 미전달: 이름·등록 위치·접근 정책 확인")

    add("database_format", validate_database_url(os.getenv("JOBNKILL_DATABASE_URL", "").strip()),
        "외부 PostgreSQL 주소·명시적 TLS 필요; 중복 옵션·쿼리의 접속 대상 덮어쓰기 금지")
    add("api_template_format", validate_api_template(os.getenv("JOBNKILL_ALIO_API_URL_TEMPLATE", "").strip()),
        "공식 /1051000/recruitment/list 및 serviceKey·pageNo·numOfRows 자리표시자와 resultType 확인; 형식 통과는 운영승인 증명이 아님")
    endpoint = os.getenv("JOBNKILL_S3_ENDPOINT_URL", "").strip()
    endpoint_ready = not endpoint or validate_s3_endpoint(endpoint)
    prefix = os.getenv("JOBNKILL_S3_PREFIX", "documents").strip("/") or "documents"
    add("storage_format", os.getenv("JOBNKILL_STORAGE_BACKEND") == "s3" and endpoint_ready
        and validate_s3_bucket(os.getenv("JOBNKILL_S3_BUCKET", "").strip())
        and all(part not in {"", ".", ".."} for part in prefix.split("/")), "S3 backend·HTTPS endpoint·문서 prefix 확인")
    if os.getenv("AWS_ACCESS_KEY_ID", "").startswith("ASIA"):
        add("JOBNKILL_S3_SESSION_TOKEN", bool(os.getenv("AWS_SESSION_TOKEN", "").strip()),
            "AWS 임시 접근키는 session token 필요; 만료는 연결 진단에서 확인")
    for driver in ("psycopg", "boto3"):
        add(driver, importlib.util.find_spec(driver) is not None, "requirements-production.txt 설치 필요")

    configuration_ready = all(item["ready"] for item in checks)
    if check_connections and configuration_ready:
        probes = (
            ("database_connection", _database_probe, "SELECT 1 통과; DDL·쓰기 권한은 별도 검증", "DB 연결 실패: 외부 주소·TLS·방화벽·만료·계정 확인"),
            ("storage_connection", _storage_probe, "S3 연결·버전관리 정책 통과; Put/Delete 권한은 별도 검증", "S3 확인 실패: 키·버킷·리전·GetBucketVersioning 권한·버전관리 확인"),
            ("api_response", _api_probe, "1페이지 조회·응답 구조 통과; 운영승인·쿼터는 포털에서 별도 확인", "API 확인 실패: 승인·키·만료·쿼터·최종 URL·응답 구조 확인"),
        )
        for name, probe, success, failure in probes:
            try:
                probe()
            except Exception:
                # Exception chains / remote payloads can contain DSNs or keys.
                add(name, False, failure)
            else:
                add(name, True, success)
    return {
        "ready": all(item["ready"] for item in checks),
        "configuration_ready": configuration_ready,
        "connection_checks": "performed" if check_connections and configuration_ready else "not_performed",
        "read_only_diagnostics_passed": bool(check_connections and configuration_ready and all(c["ready"] for c in checks)),
        "collection_performed": False,
        "environment": "production", "profile": "github_collector",
        "checks": checks, "secrets_redacted": True,
        "notice": "진단 성공은 전체 동기화·운영승인·DB 쓰기·S3 Put/Delete 성공을 의미하지 않습니다.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-connections", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    result = report(args.check_connections)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.summary and os.getenv("GITHUB_STEP_SUMMARY"):
        lines = ["## Production refresh 진단", "", "| 점검 | 상태 | 안내 |", "|---|---|---|"]
        lines += [f"| {c['name']} | {'통과' if c['ready'] else '실패'} | {c['message']} |" for c in result["checks"]]
        lines += ["", result["notice"], "", "설정 위치: Settings → Environments → production → Environment secrets / variables", ""]
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write("\n".join(lines))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
