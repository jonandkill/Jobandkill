"""Stored national occupational references, separate from institution vacancies.

Q-Net field names and endpoint follow data.go.kr/15150267. The JSON/XML wrapper
adapters are contract fixtures, NOT evidence of a successful live API request.
This API does not supply knowledge, skills, attitudes or performance criteria.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .db import Connection, DatabaseTarget, connect, initialize, json_text, json_value
from .ingest import ConfigurationError, USER_AGENT, _source_lease


NCS_SOURCE = "ncs-common"
TRAINING_SOURCE = "ncs-training-2025"
NCS_ENDPOINT = "https://c.q-net.or.kr/openapi/NcsInfo/ncsinfodetail.do"
NCS_SOURCE_URL = "https://www.data.go.kr/data/15150267/openapi.do"
TRAINING_SOURCE_URL = "https://www.data.go.kr/data/15083321/fileData.do"
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 10_000
MAX_RECORDS = 2_000_000
UNAVAILABLE_FIELDS = ("knowledge", "skills", "attitudes", "performance_criteria")
NCS_FIELDS = (
    "ncsClCd", "compeUnitName", "compeUnitLevel", "ncsLclasCdnm",
    "ncsMclasCdnm", "ncsSclasCdnm", "ncsSubdCdnm", "compeUnitDef",
    "ncsLastLinkDt",
)


class CatalogError(ValueError):
    """Only a fixed error code may escape to logs; upstream bodies may be secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        raise CatalogError("invalid_pagination")
    result = int(value)
    if not minimum <= result <= maximum:
        raise CatalogError("invalid_pagination")
    return result


def _field(value: Any, limit: int = 20_000) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CatalogError("invalid_record")
    text = str(value).strip()
    if len(text) > limit or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise CatalogError("invalid_record")
    return text


def _xml_object(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return element.text or ""
    result: dict[str, Any] = {}
    for child in children:
        tag = child.tag.rsplit("}", 1)[-1]
        value = _xml_object(child)
        if tag in result:
            if not isinstance(result[tag], list):
                result[tag] = [result[tag]]
            result[tag].append(value)
        else:
            result[tag] = value
    return result


def parse_ncs_payload(payload: bytes) -> dict[str, Any]:
    """Parse only a paged successful government response; never trust total alone."""
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise CatalogError("invalid_payload_size")
    try:
        text = payload.decode("utf-8-sig").strip()
        if text.startswith("<"):
            if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                raise CatalogError("unsafe_xml")
            root = ET.fromstring(text)
            # Bound depth and nodes before recursive conversion.
            pending = [(root, 0)]
            count = 0
            while pending:
                element, depth = pending.pop()
                count += 1
                if depth > 12 or count > 50_000:
                    raise CatalogError("invalid_response")
                pending.extend((child, depth + 1) for child in element)
            data = _xml_object(root)
        else:
            data = json.loads(text)
    except (UnicodeError, ET.ParseError, json.JSONDecodeError, RecursionError):
        raise CatalogError("invalid_response") from None
    if not isinstance(data, dict):
        raise CatalogError("invalid_response")
    response = data.get("response", data)
    if not isinstance(response, dict):
        raise CatalogError("invalid_response")
    header = response.get("header", response)
    body = response.get("body", response)
    if not isinstance(header, dict) or not isinstance(body, dict):
        raise CatalogError("invalid_response")
    if str(header.get("resultCode", "")) != "00":
        raise CatalogError("upstream_api_error")
    total = _integer(body.get("totalCount"), 0, MAX_RECORDS)
    page = _integer(body.get("pageNo"), 1, MAX_PAGES)
    page_size = _integer(body.get("numOfRows"), 1, 1000)
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [])
    if items in (None, ""):
        items = []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise CatalogError("invalid_response")
    if len(items) > page_size:
        raise CatalogError("invalid_pagination")
    return {"items": items, "reported_total": total, "page_no": page, "page_size": page_size}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Even a same-host redirect must not receive the service key implicitly.
        return None


def _fetch_page(service_key: str, page_no: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "serviceKey": service_key, "pageNo": page_no,
        "numOfRows": PAGE_SIZE, "type": "json",
    })
    request = urllib.request.Request(
        NCS_ENDPOINT + "?" + query,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml"},
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:
            if response.getcode() != 200:
                raise CatalogError("upstream_http_error")
            if response.geturl() != request.full_url:
                raise CatalogError("upstream_redirect_blocked")
            payload = response.read(MAX_PAYLOAD_BYTES + 1)
        # A service that reflects credentials must not enter the public corpus.
        if service_key.encode() in payload or urllib.parse.quote(service_key, safe="").encode() in payload:
            raise CatalogError("credential_reflection_blocked")
        result = parse_ncs_payload(payload)
        if result["page_no"] != page_no or result["page_size"] != PAGE_SIZE:
            raise CatalogError("unexpected_page")
        return result
    except CatalogError:
        raise
    except Exception:
        # URLError/HTTPError may contain the credential-bearing URL or response.
        raise CatalogError("upstream_fetch_failed") from None


def _normalize_ncs(item: Mapping[str, Any]) -> dict[str, str]:
    # Preserve documented original fields, not arbitrary reflected credentials.
    raw = {name: _field(item.get(name)) for name in NCS_FIELDS}
    external = raw["ncsClCd"]
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", external) or not raw["compeUnitName"]:
        raise CatalogError("invalid_record")
    match = re.fullmatch(r"(\d{10})(?:_([A-Za-z0-9.-]+))?", external)
    standard_code, version = match.groups(default="") if match else ("", "")
    return {
        "source_slug": NCS_SOURCE, "external_key": external,
        "standard_code": standard_code, "version": version,
        "job_title": raw["compeUnitName"], "summary": raw["compeUnitDef"],
        "ncs_path": " > ".join(raw[name] for name in (
            "ncsLclasCdnm", "ncsMclasCdnm", "ncsSclasCdnm", "ncsSubdCdnm",
        ) if raw[name]),
        "level": raw["compeUnitLevel"], "training_hours": "",
        "source_url": NCS_SOURCE_URL, "source_updated_at": raw["ncsLastLinkDt"],
        "raw_json": json_text(raw),
    }


def _upsert(connection: Connection, item: dict[str, str], run_id: str = "") -> bool:
    identifier = hashlib.sha256((item["source_slug"] + "\0" + item["external_key"]).encode()).hexdigest()
    previous = connection.execute(
        "SELECT last_seen_run_id FROM occupation_catalog WHERE id=?", (identifier,),
    ).fetchone()
    is_new_to_run = previous is None or previous["last_seen_run_id"] != run_id
    connection.execute(
        """INSERT INTO occupation_catalog(
            id,source_slug,external_key,standard_code,version,job_title,summary,ncs_path,
            level,training_hours,source_url,source_updated_at,collected_at,content_hash,raw_json,last_seen_run_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_slug,external_key) DO UPDATE SET
            standard_code=excluded.standard_code,version=excluded.version,job_title=excluded.job_title,
            summary=excluded.summary,ncs_path=excluded.ncs_path,level=excluded.level,
            training_hours=excluded.training_hours,source_url=excluded.source_url,
            source_updated_at=excluded.source_updated_at,collected_at=excluded.collected_at,
            content_hash=excluded.content_hash,raw_json=excluded.raw_json,
            last_seen_run_id=CASE WHEN excluded.last_seen_run_id != '' THEN excluded.last_seen_run_id
                                 ELSE occupation_catalog.last_seen_run_id END""",
        (identifier, *(item[name] for name in (
            "source_slug", "external_key", "standard_code", "version", "job_title", "summary",
            "ncs_path", "level", "training_hours", "source_url", "source_updated_at",
        )), _now(), hashlib.sha256(item["raw_json"].encode()).hexdigest(), item["raw_json"], run_id),
    )
    return is_new_to_run


def _new_run(connection: Connection, source_slug: str, mode: str) -> str:
    run_id = uuid.uuid4().hex
    timestamp = _now()
    connection.execute(
        "INSERT INTO catalog_sync_runs(id,source_slug,mode,status,started_at,updated_at) "
        "VALUES (?,?,?,'running',?,?)", (run_id, source_slug, mode, timestamp, timestamp),
    )
    return run_id


def _run_result(connection: Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM catalog_sync_runs WHERE id=?", (run_id,)).fetchone()
    result = dict(row)
    for name in ("started_at", "updated_at", "finished_at"):
        if result[name] is not None:
            result[name] = str(result[name])
    result["run_id"] = result.pop("id")
    result["source"] = result["source_slug"]
    result["missing_fields"] = list(UNAVAILABLE_FIELDS)
    result["live_contract_verified"] = False
    result["completion_scope"] = (
        "제공된 2025 NCS 훈련기준 CSV 파일만 적재; 최신 전체 NCS·전체 기관 직무 수집 완료가 아님"
        if result["source_slug"] == TRAINING_SOURCE
        else "해당 API의 현재 페이지 조회 범위; 전체 NCS·전체 기관 직무를 뜻하지 않음"
    )
    return result


def _finish(connection: Connection, run_id: str, status: str, error: str = "") -> dict[str, Any]:
    connection.execute(
        "UPDATE catalog_sync_runs SET status=?,error_summary=?,updated_at=?,finished_at=? WHERE id=?",
        (status, error, _now(), _now(), run_id),
    )
    connection.commit()
    return _run_result(connection, run_id)


def sync_ncs_catalog(
    db_path: DatabaseTarget | None = None, max_pages: int = 100, restart: bool = False,
) -> dict[str, Any]:
    """Resume atomic page checkpoints; a bounded run never means 'all collected'."""
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("max_pages는 1~10000 범위의 정수여야 합니다.")
    if not isinstance(restart, bool):
        raise ValueError("restart는 bool이어야 합니다.")
    service_key = os.getenv("JOBNKILL_NCS_SERVICE_KEY", "").strip()
    if not service_key:
        raise ConfigurationError("JOBNKILL_NCS_SERVICE_KEY에 해당 API의 승인된 서비스키를 설정하세요.")
    # Accept the portal's encoded key without double-encoding, never inherit an
    # unrelated ALIO key/application approval implicitly.
    service_key = urllib.parse.unquote(service_key)
    if len(service_key) > 2048 or any(ord(char) < 32 for char in service_key):
        raise ConfigurationError("JOBNKILL_NCS_SERVICE_KEY 형식이 올바르지 않습니다.")
    target = initialize(db_path)
    with connect(target) as connection, _source_lease(connection, target, NCS_SOURCE):
        prior = connection.execute(
            "SELECT * FROM catalog_sync_runs WHERE source_slug=? AND mode='api' "
            "ORDER BY started_at DESC,id DESC LIMIT 1", (NCS_SOURCE,),
        ).fetchone()
        if not restart and prior and prior["status"] in {"running", "partial", "failed"}:
            run_id = str(prior["id"])
            if str(prior["error_summary"]).endswith("restart_required"):
                return _run_result(connection, run_id)
            connection.execute(
                "UPDATE catalog_sync_runs SET status='running',finished_at=NULL,updated_at=? WHERE id=?",
                (_now(), run_id),
            )
        else:
            run_id = _new_run(connection, NCS_SOURCE, "api")
        connection.commit()
        for _ in range(max_pages):
            state = _run_result(connection, run_id)
            page_no = int(state["next_page"])
            if page_no > MAX_PAGES:
                return _finish(connection, run_id, "partial", "page_limit_reached")
            try:
                page = _fetch_page(service_key, page_no)
                if state["reported_total"] is not None and state["reported_total"] != page["reported_total"]:
                    raise CatalogError("total_changed_restart_required")
                normalized = [_normalize_ncs(item) for item in page["items"]]
                signature = hashlib.sha256(json_text(sorted(item["external_key"] for item in normalized)).encode()).hexdigest()
                repeated = connection.execute(
                    "SELECT page_no FROM catalog_sync_pages WHERE run_id=? AND page_hash=?", (run_id, signature),
                ).fetchone()
                if repeated:
                    raise CatalogError("repeated_page_restart_required")
                if not normalized and state["unique_count"] != page["reported_total"]:
                    raise CatalogError("empty_page_before_total")
                new_unique = sum(_upsert(connection, item, run_id) for item in normalized)
                seen = int(state["records_seen"]) + len(normalized)
                unique = int(state["unique_count"]) + new_unique
                duplicates = int(state["duplicates"]) + len(normalized) - new_unique
                connection.execute(
                    "INSERT INTO catalog_sync_pages(run_id,page_no,page_hash,records_seen) VALUES (?,?,?,?)",
                    (run_id, page_no, signature, len(normalized)),
                )
                connection.execute(
                    "UPDATE catalog_sync_runs SET next_page=?,reported_total=?,records_seen=?,unique_count=?,"
                    "duplicates=?,error_summary='',updated_at=? WHERE id=?",
                    (page_no + 1, page["reported_total"], seen, unique, duplicates, _now(), run_id),
                )
                connection.commit()
                last_page = page_no * page["page_size"] >= page["reported_total"]
                if last_page:
                    complete = unique == page["reported_total"] and duplicates == 0
                    return _finish(connection, run_id, "completed" if complete else "partial",
                                   "" if complete else "unique_total_mismatch_restart_required")
                if len(normalized) != page["page_size"]:
                    return _finish(connection, run_id, "partial", "short_page_restart_required")
            except Exception as error:
                connection.rollback()
                code = error.code if isinstance(error, CatalogError) else "catalog_sync_failed"
                # No exception string or traceback is persisted or returned.
                current = _run_result(connection, run_id)
                return _finish(connection, run_id, "partial" if current["unique_count"] else "failed", code)
        return _finish(connection, run_id, "partial", "page_budget_reached_resume_available")


def _read_import(input_path: Path | str) -> bytes:
    try:
        with Path(input_path).open("rb") as file:
            payload = file.read(MAX_PAYLOAD_BYTES + 1)
    except OSError:
        raise CatalogError("import_file_unavailable") from None
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise CatalogError("invalid_payload_size")
    return payload


def _import_normalized(
    items: list[dict[str, str]], source_slug: str, db_path: DatabaseTarget | None,
    reported_total: int | None = None, source_file_hash: str = "",
) -> dict[str, Any]:
    target = initialize(db_path)
    with connect(target) as connection, _source_lease(connection, target, source_slug):
        run_id = _new_run(connection, source_slug, "import")
        unique = len({item["external_key"] for item in items})
        for item in items:
            _upsert(connection, item)  # Offline import never changes API checkpoints.
        connection.execute(
            "UPDATE catalog_sync_runs SET records_seen=?,unique_count=?,duplicates=?,reported_total=?,source_file_hash=? WHERE id=?",
            (len(items), unique, len(items) - unique, reported_total, source_file_hash, run_id),
        )
        return _finish(connection, run_id, "imported")


def import_ncs_catalog(input_path: Path | str, db_path: DatabaseTarget | None = None) -> dict[str, Any]:
    raw = _read_import(input_path)
    page = parse_ncs_payload(raw)
    return _import_normalized([_normalize_ncs(item) for item in page["items"]], NCS_SOURCE,
                              db_path, page["reported_total"], hashlib.sha256(raw).hexdigest())


def import_ncs_training_csv(input_path: Path | str, db_path: DatabaseTarget | None = None) -> dict[str, Any]:
    """Import the official four-column 2025 NCS training snapshot (KOGL type 1).

    This snapshot supplies a versioned unit code, title, level and training hours,
    not a unit definition, KSA, institution-specific requirements or live totals.
    The downloaded official file requires CP949 (a superset of EUC-KR).
    """
    raw = _read_import(input_path)
    text = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None:
        raise CatalogError("unsupported_csv_encoding")
    rows = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    columns = ["분류번호", "명칭", "수준", "훈련시간"]
    normalized = []
    try:
        if rows.fieldnames != columns:
            raise CatalogError("unexpected_training_csv_columns")
        for row in rows:
            if (len(normalized) >= 100_000 or set(row) != set(columns)
                    or any(row[name] is None for name in columns)):
                raise CatalogError("invalid_training_csv_row")
            original = {name: _field(row[name]) for name in columns}
            external = original["분류번호"]
            match = re.fullmatch(r"(\d{10})_([A-Za-z0-9.-]+)", external)
            if not match or not original["명칭"]:
                raise CatalogError("invalid_training_csv_row")
            level = str(_integer(original["수준"], 1, 8)) if original["수준"] else ""
            hours = str(_integer(original["훈련시간"], 0, 100_000)) if original["훈련시간"] else ""
            normalized.append({
                "source_slug": TRAINING_SOURCE, "external_key": external,
                "standard_code": match.group(1), "version": match.group(2),
                "job_title": original["명칭"], "summary": "", "ncs_path": "",
                "level": level, "training_hours": hours,
                "source_url": TRAINING_SOURCE_URL, "source_updated_at": "2025-12-31",
                "raw_json": json_text(original),
            })
    except (csv.Error, TypeError):
        raise CatalogError("invalid_training_csv") from None
    if not normalized:
        raise CatalogError("empty_training_csv")
    result = _import_normalized(normalized, TRAINING_SOURCE, db_path,
                                source_file_hash=hashlib.sha256(raw).hexdigest())
    result["completion_scope"] = "제공된 2025 NCS 훈련기준 CSV 파일만 적재; 최신 전체 NCS·전체 기관 직무 수집 완료가 아님"
    result["license_code"] = "KOGL1"
    result["license_note"] = "공공누리 제1유형: 한국산업인력공단 및 원문 출처 표시"
    return result


def _public_item(row: Any, detail: bool = False) -> dict[str, Any]:
    result = dict(row)
    result.pop("last_seen_run_id", None)
    raw = json_value(result.pop("raw_json", "{}"), {})
    result["collected_at"] = str(result["collected_at"])
    result["ncs_code"] = result["standard_code"]
    result["kind"] = "national_standard_reference"
    result["missing_fields"] = list(UNAVAILABLE_FIELDS)
    for name in UNAVAILABLE_FIELDS:
        result[name] = []
    for name in ("summary", "standard_code", "version", "ncs_path", "level", "training_hours"):
        if not result[name]:
            result["missing_fields"].append(name)
    result["evidence_status"] = "official_source_reference_not_institution_requirement"
    result["provider"] = "한국산업인력공단"
    result["license_code"] = "KOGL1" if result["source_slug"] == TRAINING_SOURCE else "public_data_terms"
    result["license_note"] = (
        "공공누리 제1유형: 한국산업인력공단 및 원문 출처 표시"
        if result["source_slug"] == TRAINING_SOURCE else "공공데이터포털 해당 API 이용조건 확인"
    )
    if detail:
        result["raw"] = raw
    return result


def list_catalog(
    connection: Connection, query: str = "", limit: int = 20, offset: int = 0,
) -> list[dict[str, Any]]:
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("검색어는 200자 이하여야 합니다.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit은 1~100 범위의 정수여야 합니다.")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_RECORDS:
        raise ValueError("offset 범위를 확인하세요.")
    escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    match = "%" + escaped + "%"
    rows = connection.execute(
        "SELECT * FROM occupation_catalog WHERE job_title LIKE ? ESCAPE '\\' "
        "OR summary LIKE ? ESCAPE '\\' OR ncs_path LIKE ? ESCAPE '\\' OR external_key LIKE ? ESCAPE '\\' "
        "ORDER BY job_title,source_slug,external_key LIMIT ? OFFSET ?",
        (match, match, match, match, limit, offset),
    ).fetchall()
    return [_public_item(row) for row in rows]


def get_catalog_item(connection: Connection, id: str) -> dict[str, Any] | None:
    if not isinstance(id, str) or not re.fullmatch(r"[a-f0-9]{64}", id):
        return None
    row = connection.execute("SELECT * FROM occupation_catalog WHERE id=?", (id,)).fetchone()
    return _public_item(row, detail=True) if row else None


def catalog_coverage(connection: Connection) -> dict[str, Any]:
    sources = []
    total = 0
    for source_slug in (NCS_SOURCE, TRAINING_SOURCE):
        records = int(connection.execute(
            "SELECT COUNT(*) AS count FROM occupation_catalog WHERE source_slug=?", (source_slug,),
        ).fetchone()["count"])
        total += records
        run = connection.execute(
            "SELECT id FROM catalog_sync_runs WHERE source_slug=? ORDER BY started_at DESC,id DESC LIMIT 1",
            (source_slug,),
        ).fetchone()
        api_run = connection.execute(
            "SELECT id FROM catalog_sync_runs WHERE source_slug=? AND mode='api' ORDER BY started_at DESC,id DESC LIMIT 1",
            (source_slug,),
        ).fetchone()
        sources.append({
            "source_slug": source_slug, "records": records,
            "latest_run": _run_result(connection, str(run["id"])) if run else None,
            "latest_api_run": _run_result(connection, str(api_run["id"])) if api_run else None,
            "status": "stored" if records else "not_collected",
            "scope": "능력단위 정보" if source_slug == NCS_SOURCE else "2025년 훈련기준 공개 파일",
        })
    missing = {name: total for name in UNAVAILABLE_FIELDS}
    for name in ("summary", "standard_code", "version", "ncs_path", "level", "training_hours"):
        missing[name] = int(connection.execute(
            f"SELECT COUNT(*) AS count FROM occupation_catalog WHERE {name}=''",
        ).fetchone()["count"])
    return {
        "catalog_records": total, "sources": sources, "missing_fields": missing,
        "all_government_data_collected": False,
        "notes": [
            "정부 기준정보는 기관 채용공고의 직무기술서와 별도로 관리합니다.",
            "이 소스가 제공하지 않는 지식·기술·태도·수행준거는 누락으로 표시하며 AI로 사실을 채우지 않습니다.",
            "API 총계와 고유 원본 키 수를 검증해도 해당 조회 범위의 완료이며 전체 기관·전체 직무 수집 완료는 아닙니다.",
            "이전 수집분은 삭제·최신 여부가 확정되지 않은 기록을 포함할 수 있습니다. 수집일과 원문을 확인하세요.",
        ],
    }
