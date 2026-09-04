from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator

from .db import ROOT, connect, initialize, json_text


USER_AGENT = "JobAndKillCollector/0.1 (+https://github.com/jonandkill/Jobandkill)"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
ALLOWED_DOWNLOAD_HOST_SUFFIXES = (".data.go.kr", ".alio.go.kr", ".ncs.go.kr")


class ConfigurationError(RuntimeError):
    pass


@dataclass
class AttachmentRecord:
    external_key: str
    title: str
    official_url: str
    kind: str = "other"
    media_type: str = ""
    license_code: str = ""


@dataclass
class PostingRecord:
    external_id: str
    institution_name: str
    institution_code: str
    institution_type: str
    title: str
    original_url: str
    employment_type: str = ""
    hiring_type: str = ""
    education: str = ""
    regions: list[str] = field(default_factory=list)
    ncs_categories: list[str] = field(default_factory=list)
    positions: list[str] = field(default_factory=list)
    headcount_text: str = ""
    qualifications: str = ""
    preferences: str = ""
    selection_process: str = ""
    application_method: str = ""
    published_at: str | None = None
    application_start: str | None = None
    application_end: str | None = None
    attachments: list[AttachmentRecord] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "external_id": (
        "recrutPblntSn", "recruitPblntSn", "recruitId", "recrutNo", "pblntNo",
        "pbancSn", "idx", "id", "seq",
    ),
    "institution_name": (
        "instNm", "institutionName", "publicOrgNm", "pbancInsttNm", "organNm",
        "recrutPbancInsttNm", "agencyName",
    ),
    "institution_code": (
        "instCd", "institutionCode", "pblntInstCd", "pbancInsttCd", "agencyCode",
    ),
    "institution_type": ("instType", "institutionType", "publicOrgType", "insttTypeNm"),
    "title": (
        "recrutPbancTtl", "recrutPblntTtl", "title", "recruitTitle", "pbancTtl",
        "recrutPbancNm", "noticeTitle",
    ),
    "original_url": (
        "recrutPbancUrl", "recrutPblntUrl", "url", "detailUrl", "homepageUrl",
        "srcUrl", "originUrl",
    ),
    "employment_type": ("hireTypeNm", "employmentType", "hireType", "emplymShpNm"),
    "hiring_type": ("recrutSeNm", "hiringType", "recruitType", "recrutSe"),
    "education": ("acbgCondNm", "education", "educationCondition", "acbgCond"),
    "regions": ("workRgnNm", "regions", "workRegion", "workRgnLst", "regionNames"),
    "ncs_categories": ("ncsCdNm", "ncsCategories", "ncsCdLst", "ncsNames", "ncs"),
    "positions": ("recrutJobsNm", "positions", "jobNames", "recruitJobs", "recrutJobs"),
    "headcount_text": ("recrutNope", "headcount", "recruitCount", "hireCount"),
    "qualifications": ("aplyQlfcCn", "qualifications", "qualification", "eligibility"),
    "preferences": ("prefCondCn", "preferences", "preferred", "preferentialTreatment"),
    "selection_process": ("scrnprcdrMthdExpln", "selectionProcess", "screening", "process"),
    "application_method": ("aplyMthdExpln", "applicationMethod", "applyMethod"),
    "published_at": ("pblntDt", "publishedAt", "postedAt", "regDate", "regYmd"),
    "application_start": ("pbancBgngYmd", "applicationStart", "startDate", "recrutBgnDt"),
    "application_end": ("pbancEndYmd", "applicationEnd", "endDate", "recrutEndDt"),
}

ATTACHMENT_LIST_KEYS = (
    "attachments", "files", "fileList", "atchFileList", "atchFileLst",
    "recrutPbancAtchFileList", "recrutPblntAtchFileList",
)
ATTACHMENT_TITLE_KEYS = ("fileName", "fileNm", "atchFileNm", "title", "name", "orgnlFileNm")
ATTACHMENT_URL_KEYS = ("fileUrl", "downloadUrl", "atchFileUrl", "url", "href", "fileDownloadUrl")
ATTACHMENT_ID_KEYS = ("fileNo", "atchFileNo", "fileId", "id", "seq")


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def clean_text(value: Any, limit: int = 20_000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    text = html.unescape(str(value)).replace("\x00", " ")
    return " ".join(text.split())[:limit]


def pick(record: dict[str, Any], aliases: Iterable[str]) -> Any:
    lower = {str(key).lower(): value for key, value in record.items()}
    for name in aliases:
        value = record.get(name, lower.get(name.lower()))
        if value not in (None, "", [], {}):
            return value
    return None


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        candidates = value.get("item") or value.get("items") or list(value.values())
        return as_list(candidates)
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item = pick(item, ("name", "label", "title", "value", "nm", "ncsCdNm"))
            cleaned = clean_text(item, 500)
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result
    text = clean_text(value, 5_000)
    if not text:
        return []
    chunks = re.split(r"\s*(?:\||,|;|\n| / )\s*", text)
    return list(dict.fromkeys(item.strip() for item in chunks if item.strip()))


def normalize_date(value: Any) -> str | None:
    text = clean_text(value, 40)
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:25]


def _absolute_https_url(value: Any, base_url: str = "") -> str:
    text = clean_text(value, 2_000)
    if not text:
        return ""
    url = urllib.parse.urljoin(base_url, text)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    return url


def _attachment_kind(title: str) -> str:
    lowered = title.lower()
    if "직무기술" in title or "job description" in lowered:
        return "job_description"
    if "채용공고" in title or "공고문" in title:
        return "announcement"
    if "입사지원" in title or "지원서" in title:
        return "application_form"
    if "경력기술" in title:
        return "career_form"
    if "경험기술" in title:
        return "experience_form"
    return "other"


def _attachments(record: dict[str, Any], base_url: str = "") -> list[AttachmentRecord]:
    raw_items: Any = None
    for key in ATTACHMENT_LIST_KEYS:
        raw_items = record.get(key)
        if raw_items not in (None, "", [], {}):
            break
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("item") or raw_items.get("items") or list(raw_items.values())
    if not isinstance(raw_items, list):
        raw_items = []
    attachments: list[AttachmentRecord] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        title = clean_text(pick(item, ATTACHMENT_TITLE_KEYS), 500) or f"첨부파일 {index + 1}"
        url = _absolute_https_url(pick(item, ATTACHMENT_URL_KEYS), base_url)
        if not url:
            continue
        external_key = clean_text(pick(item, ATTACHMENT_ID_KEYS), 200)
        if not external_key:
            external_key = hashlib.sha256(url.encode()).hexdigest()[:24]
        media_type = clean_text(pick(item, ("mediaType", "contentType", "mimeType")), 100)
        license_code = clean_text(pick(item, ("licenseCode", "license", "koglType")), 100)
        attachments.append(
            AttachmentRecord(external_key, title, url, _attachment_kind(title), media_type, license_code)
        )
    return attachments


def normalize_api_record(record: dict[str, Any], base_url: str = "") -> PostingRecord | None:
    institution_name = clean_text(pick(record, FIELD_ALIASES["institution_name"]), 300)
    title = clean_text(pick(record, FIELD_ALIASES["title"]), 500)
    if not institution_name or not title:
        return None
    original_url = _absolute_https_url(pick(record, FIELD_ALIASES["original_url"]), base_url)
    external_id = clean_text(pick(record, FIELD_ALIASES["external_id"]), 200)
    if not external_id:
        identity = "|".join((institution_name, title, original_url))
        external_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return PostingRecord(
        external_id=external_id,
        institution_name=institution_name,
        institution_code=clean_text(pick(record, FIELD_ALIASES["institution_code"]), 100),
        institution_type=clean_text(pick(record, FIELD_ALIASES["institution_type"]), 200),
        title=title,
        original_url=original_url or base_url,
        employment_type=clean_text(pick(record, FIELD_ALIASES["employment_type"]), 300),
        hiring_type=clean_text(pick(record, FIELD_ALIASES["hiring_type"]), 300),
        education=clean_text(pick(record, FIELD_ALIASES["education"]), 500),
        regions=as_list(pick(record, FIELD_ALIASES["regions"])),
        ncs_categories=as_list(pick(record, FIELD_ALIASES["ncs_categories"])),
        positions=as_list(pick(record, FIELD_ALIASES["positions"])),
        headcount_text=clean_text(pick(record, FIELD_ALIASES["headcount_text"]), 200),
        qualifications=clean_text(pick(record, FIELD_ALIASES["qualifications"])),
        preferences=clean_text(pick(record, FIELD_ALIASES["preferences"])),
        selection_process=clean_text(pick(record, FIELD_ALIASES["selection_process"])),
        application_method=clean_text(pick(record, FIELD_ALIASES["application_method"])),
        published_at=normalize_date(pick(record, FIELD_ALIASES["published_at"])),
        application_start=normalize_date(pick(record, FIELD_ALIASES["application_start"])),
        application_end=normalize_date(pick(record, FIELD_ALIASES["application_end"])),
        attachments=_attachments(record, base_url),
        raw=record,
    )


def _walk_for_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return value
    if not isinstance(value, dict):
        return []
    for key in ("item", "items", "data", "records", "result", "list"):
        child = value.get(key)
        if isinstance(child, list):
            return [item for item in child if isinstance(item, dict)]
        if key == "item" and isinstance(child, dict):
            return [child]
        found = _walk_for_records(child)
        if found:
            return found
    for child in value.values():
        found = _walk_for_records(child)
        if found:
            return found
    return []


def _xml_to_dict(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        key = child.tag.split("}")[-1]
        value = _xml_to_dict(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def parse_api_payload(payload: bytes, content_type: str = "") -> tuple[list[dict[str, Any]], int | None]:
    decoded = payload.decode("utf-8-sig", errors="replace")
    try:
        data: Any = json.loads(decoded)
    except json.JSONDecodeError:
        try:
            data = _xml_to_dict(ET.fromstring(decoded))
        except ET.ParseError as error:
            raise ValueError(f"공식 API 응답을 JSON/XML로 해석할 수 없습니다: {content_type}") from error
    records = _walk_for_records(data)
    total: int | None = None
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key.lower() in {"totalcount", "total_count", "total"}:
                    try:
                        total = int(value)
                    except (TypeError, ValueError):
                        pass
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return records, total


class AlioApiSource:
    slug = "data-go-kr-alio"

    def __init__(self) -> None:
        self.template = os.getenv("JOBNKILL_ALIO_API_URL_TEMPLATE", "").strip()
        self.service_key = os.getenv("JOBNKILL_ALIO_SERVICE_KEY", "").strip()
        self.page_size = min(max(int(os.getenv("JOBNKILL_PAGE_SIZE", "100")), 1), 1000)
        self.max_pages = min(max(int(os.getenv("JOBNKILL_MAX_PAGES", "10")), 1), 100)
        if not self.template or not self.service_key:
            raise ConfigurationError(
                "공공데이터포털 활용신청 후 JOBNKILL_ALIO_API_URL_TEMPLATE과 "
                "JOBNKILL_ALIO_SERVICE_KEY를 설정해야 합니다."
            )
        missing = {name for name in ("service_key", "page", "page_size") if "{" + name + "}" not in self.template}
        if missing:
            raise ConfigurationError("API URL 템플릿에 다음 자리표시자가 필요합니다: " + ", ".join(sorted(missing)))
        parsed = urllib.parse.urlparse(self.template)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("data.go.kr"):
            raise ConfigurationError("ALIO API URL은 data.go.kr의 HTTPS 주소여야 합니다.")

    def fetch(self) -> Iterator[list[dict[str, Any]]]:
        seen = 0
        for page in range(1, self.max_pages + 1):
            url = self.template.format(
                service_key=urllib.parse.quote(self.service_key, safe="%"),
                page=page,
                page_size=self.page_size,
            )
            request = urllib.request.Request(url, headers={"Accept": "application/json, application/xml", "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read(10 * 1024 * 1024 + 1)
                    if len(payload) > 10 * 1024 * 1024:
                        raise ValueError("API 응답이 10MB 제한을 넘었습니다.")
                    records, total = parse_api_payload(payload, response.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as error:
                detail = error.read(500).decode(errors="replace")
                raise RuntimeError(f"공식 API 요청 실패: HTTP {error.code} {detail}") from error
            if not records:
                break
            yield records
            seen += len(records)
            if len(records) < self.page_size or (total is not None and seen >= total):
                break


class _NcsTableParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.in_row = False
        self.in_cell = False
        self.cell_depth = 0
        self.cells: list[list[str]] = []
        self.current: list[str] = []
        self.links: list[str] = []
        self.rows: list[tuple[list[str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "tr":
            self.in_row = True
            self.cells, self.links = [], []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.cell_depth = 1
            self.current = []
        elif self.in_cell:
            if tag == "br":
                self.current.append("\n")
            else:
                self.cell_depth += 1
                if tag == "li":
                    self.current.append("\n")
        if self.in_row and tag in {"a", "button"}:
            candidates = [attrs_dict.get("href"), attrs_dict.get("data-url"), attrs_dict.get("onclick")]
            for candidate in candidates:
                if not candidate:
                    continue
                urls = re.findall(r"(?:https://[^'\"\s)]+|/[A-Za-z0-9_./?=&%-]+)", candidate)
                for value in urls:
                    url = _absolute_https_url(value, self.base_url)
                    if url and url not in self.links:
                        self.links.append(url)

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell:
            self.cell_depth -= 1
            if tag in {"td", "th"} and self.cell_depth <= 0:
                self.cells.append([clean_text(item, 1_000) for item in self.current if clean_text(item, 1_000)])
                self.in_cell = False
        if tag == "tr" and self.in_row:
            flattened = ["\n".join(dict.fromkeys(cell)) for cell in self.cells]
            if flattened:
                self.rows.append((flattened, self.links.copy()))
            self.in_row = False

    def handle_data(self, data: str) -> None:
        if self.in_cell and data.strip():
            self.current.append(data)


class NcsMetadataSource:
    slug = "ncs-job-description"
    url = "https://m.ncs.go.kr/blind/bl04/JdsptList.do"

    def fetch(self) -> Iterator[list[dict[str, Any]]]:
        request = urllib.request.Request(self.url, headers={"Accept": "text/html", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(10 * 1024 * 1024 + 1)
        if len(body) > 10 * 1024 * 1024:
            raise ValueError("NCS 목록 페이지가 10MB 제한을 넘었습니다.")
        parser = _NcsTableParser(self.url)
        parser.feed(body.decode("utf-8", errors="replace"))
        records: list[dict[str, Any]] = []
        for cells, links in parser.rows:
            joined = " | ".join(cells)
            identifier = next((match.group(0) for cell in cells if (match := re.fullmatch(r"\d{3,}", cell.strip()))), "")
            date = next((match.group(0) for cell in cells if (match := re.search(r"20\d{2}[-.]\d{2}[-.]\d{2}", cell))), "")
            if not identifier or len(cells) < 3 or not date:
                continue
            id_index = next(index for index, cell in enumerate(cells) if cell.strip() == identifier)
            institution = cells[id_index + 1] if id_index + 1 < len(cells) else ""
            job_title = cells[id_index + 2] if id_index + 2 < len(cells) else ""
            ncs_items: list[str] = []
            for cell in cells[id_index + 3:]:
                for line in cell.splitlines():
                    if ">" in line and line not in ncs_items:
                        ncs_items.append(line)
            if not institution or not job_title:
                continue
            records.append(
                {
                    "id": identifier,
                    "institutionName": institution,
                    "title": job_title,
                    "positions": [job_title],
                    "ncsCategories": ncs_items,
                    "publishedAt": date.replace(".", "-"),
                    "url": next((link for link in links if "Jdspt" in link or "recruit" in link), self.url),
                    "attachments": [
                        {"id": hashlib.sha256(link.encode()).hexdigest()[:16], "name": "NCS 등록 직무기술서", "url": link}
                        for link in links if "download" in link.lower() or "file" in link.lower()
                    ],
                    "_source_row": joined,
                }
            )
        if records:
            yield records


def _source_row(connection: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM sources WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise ConfigurationError(f"등록되지 않은 소스입니다: {slug}")
    return row


def _upsert_institution(connection: sqlite3.Connection, record: PostingRecord) -> int:
    if record.institution_code:
        connection.execute(
            """
            INSERT INTO institutions(alio_code, name, institution_type, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(alio_code) DO UPDATE SET
              name = excluded.name,
              institution_type = CASE WHEN excluded.institution_type != '' THEN excluded.institution_type ELSE institutions.institution_type END,
              updated_at = CURRENT_TIMESTAMP
            """,
            (record.institution_code, record.institution_name, record.institution_type, json_text({})),
        )
        row = connection.execute("SELECT id FROM institutions WHERE alio_code = ?", (record.institution_code,)).fetchone()
    else:
        row = connection.execute("SELECT id FROM institutions WHERE name = ? ORDER BY id LIMIT 1", (record.institution_name,)).fetchone()
        if not row:
            cursor = connection.execute(
                "INSERT INTO institutions(name, institution_type) VALUES (?, ?)",
                (record.institution_name, record.institution_type),
            )
            return int(cursor.lastrowid)
    return int(row["id"])


def _posting_values(source_id: int, institution_id: int, record: PostingRecord) -> tuple[Any, ...]:
    raw_json = json_text(record.raw)
    content_hash = hashlib.sha256(raw_json.encode()).hexdigest()
    return (
        source_id, record.external_id, institution_id, record.title, record.employment_type,
        record.hiring_type, record.education, json_text(record.regions), json_text(record.ncs_categories),
        json_text(record.positions), record.headcount_text, record.qualifications, record.preferences,
        record.selection_process, record.application_method, record.published_at, record.application_start,
        record.application_end, record.original_url, raw_json, content_hash,
    )


def _upsert_posting(connection: sqlite3.Connection, source_id: int, institution_id: int, record: PostingRecord) -> tuple[int, str]:
    existing = connection.execute(
        "SELECT id, content_hash FROM postings WHERE source_id = ? AND external_id = ?",
        (source_id, record.external_id),
    ).fetchone()
    values = _posting_values(source_id, institution_id, record)
    if existing:
        if existing["content_hash"] == values[-1]:
            return int(existing["id"]), "unchanged"
        connection.execute(
            """
            UPDATE postings SET institution_id=?, title=?, employment_type=?, hiring_type=?, education=?,
              regions_json=?, ncs_categories_json=?, positions_json=?, headcount_text=?, qualifications=?,
              preferences=?, selection_process=?, application_method=?, published_at=?, application_start=?,
              application_end=?, original_url=?, raw_json=?, content_hash=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (*values[2:], int(existing["id"])),
        )
        return int(existing["id"]), "updated"
    cursor = connection.execute(
        """
        INSERT INTO postings(
          source_id, external_id, institution_id, title, employment_type, hiring_type, education,
          regions_json, ncs_categories_json, positions_json, headcount_text, qualifications,
          preferences, selection_process, application_method, published_at, application_start,
          application_end, original_url, raw_json, content_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )
    return int(cursor.lastrowid), "created"


def _attachment_rights(default_rights: str, license_code: str) -> tuple[str, str]:
    normalized = license_code.upper().replace(" ", "")
    if normalized in {"KOGL1", "공공누리1유형", "TYPE1", "CC0"}:
        return "open_document", f"문서 메타데이터의 개방 라이선스 표시: {license_code}"
    if default_rights == "open_document":
        return "open_document", "소스의 문서 기본 이용범위가 개방으로 확인됨"
    if default_rights == "restricted":
        return "restricted", "소스 정책상 상업적 이용 또는 재배포 제한"
    return "review_required", "첨부문서의 재이용 권리를 별도로 확인해야 함"


def _upsert_attachments(
    connection: sqlite3.Connection,
    posting_id: int,
    default_rights: str,
    attachments: list[AttachmentRecord],
) -> int:
    for item in attachments:
        rights, reason = _attachment_rights(default_rights, item.license_code)
        existing = connection.execute(
            "SELECT id, rights_status FROM attachments WHERE posting_id=? AND external_key=?",
            (posting_id, item.external_key),
        ).fetchone()
        if existing:
            preserved = existing["rights_status"] if existing["rights_status"] in {"authorized", "open_document", "restricted"} else rights
            connection.execute(
                """
                UPDATE attachments SET kind=?, title=?, official_url=?, media_type=?, license_code=?,
                  rights_status=?, rights_reason=?, last_checked_at=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (item.kind, item.title, item.official_url, item.media_type, item.license_code,
                 preserved, reason, now_iso(), int(existing["id"])),
            )
        else:
            connection.execute(
                """
                INSERT INTO attachments(
                  posting_id, external_key, kind, title, official_url, media_type,
                  license_code, rights_status, rights_reason, parser_status, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (posting_id, item.external_key, item.kind, item.title, item.official_url, item.media_type,
                 item.license_code, rights, reason,
                 "queued" if rights == "open_document" else "blocked_by_rights", now_iso()),
            )
    return len(attachments)


def _metadata_profile(connection: sqlite3.Connection, posting_id: int, record: PostingRecord) -> None:
    titles = record.positions or [record.title]
    ncs_path = " · ".join(record.ncs_categories)
    summary_parts = [part for part in (record.employment_type, record.hiring_type, record.education) if part]
    summary = " · ".join(summary_parts)
    qualifications = as_list(record.qualifications)
    for title in titles[:30]:
        existing = connection.execute(
            """
            SELECT id FROM job_profiles
            WHERE posting_id=? AND attachment_id IS NULL AND job_title=? AND ncs_code=''
            ORDER BY id LIMIT 1
            """,
            (posting_id, title),
        ).fetchone()
        values = (
            record.institution_name, title, ncs_path, summary, json_text(record.ncs_categories),
            json_text([]), json_text([]), json_text([]), json_text(qualifications),
            json_text(list(dict.fromkeys(record.ncs_categories + titles))),
        )
        if existing:
            connection.execute(
                """
                UPDATE job_profiles SET institution_name=?, job_title=?, ncs_path=?, summary=?,
                  duties_json=?, knowledge_json=?, skills_json=?, attitudes_json=?, qualifications_json=?,
                  keywords_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (*values, int(existing["id"])),
            )
        else:
            connection.execute(
                """
                INSERT INTO job_profiles(
                  posting_id, institution_name, job_title, ncs_path, summary, duties_json,
                  knowledge_json, skills_json, attitudes_json, qualifications_json, keywords_json,
                  extraction_status, rights_status, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'metadata', 'open_metadata', 'metadata-v1')
                """,
                (posting_id, *values),
            )


def ingest_records(
    connection: sqlite3.Connection,
    source_slug: str,
    records: Iterable[dict[str, Any]],
    base_url: str,
) -> dict[str, int]:
    source = _source_row(connection, source_slug)
    counters = {"seen": 0, "created": 0, "updated": 0, "attachments": 0, "skipped": 0}
    for raw in records:
        counters["seen"] += 1
        normalized = normalize_api_record(raw, base_url)
        if not normalized:
            counters["skipped"] += 1
            continue
        institution_id = _upsert_institution(connection, normalized)
        posting_id, change = _upsert_posting(connection, int(source["id"]), institution_id, normalized)
        if change in counters:
            counters[change] += 1
        counters["attachments"] += _upsert_attachments(
            connection, posting_id, str(source["default_rights"]), normalized.attachments
        )
        _metadata_profile(connection, posting_id, normalized)
    return counters


SOURCE_FACTORIES = {
    "data-go-kr-alio": AlioApiSource,
    "ncs-job-description": NcsMetadataSource,
}


def sync_source(source_slug: str, db_path: Path | str | None = None) -> dict[str, Any]:
    initialize(db_path)
    with connect(db_path) as connection:
        source = _source_row(connection, source_slug)
        factory = SOURCE_FACTORIES.get(source_slug)
        if not factory:
            raise ConfigurationError(f"자동 수집기가 아직 연결되지 않은 소스입니다: {source_slug}")
        cursor = connection.execute(
            "INSERT INTO sync_runs(source_id, status) VALUES (?, 'running')",
            (int(source["id"]),),
        )
        run_id = int(cursor.lastrowid)
        connection.commit()
        totals = {"seen": 0, "created": 0, "updated": 0, "attachments": 0, "skipped": 0}
        try:
            adapter = factory()
            for page in adapter.fetch():
                result = ingest_records(connection, source_slug, page, str(source["base_url"]))
                for key in totals:
                    totals[key] += result[key]
                connection.commit()
            status = "partial" if totals["skipped"] else "success"
            connection.execute(
                """
                UPDATE sync_runs SET status=?, finished_at=?, records_seen=?, records_created=?,
                  records_updated=?, attachments_seen=? WHERE id=?
                """,
                (status, now_iso(), totals["seen"], totals["created"], totals["updated"], totals["attachments"], run_id),
            )
            connection.execute("UPDATE sources SET last_success_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (now_iso(), int(source["id"])))
            connection.commit()
            return {"run_id": run_id, "source": source_slug, "status": status, **totals}
        except Exception as error:
            connection.execute(
                "UPDATE sync_runs SET status='failed', finished_at=?, error_summary=? WHERE id=?",
                (now_iso(), clean_text(str(error), 1_000), run_id),
            )
            connection.commit()
            raise


def import_file(source_slug: str, input_path: Path | str, db_path: Path | str | None = None) -> dict[str, int]:
    path = Path(input_path)
    payload = path.read_bytes()
    records, _ = parse_api_payload(payload, mimetypes.guess_type(path.name)[0] or "")
    initialize(db_path)
    with connect(db_path) as connection:
        source = _source_row(connection, source_slug)
        result = ingest_records(connection, source_slug, records, str(source["base_url"]))
        connection.commit()
        return result


def set_attachment_rights(
    attachment_id: int,
    new_status: str,
    reason: str,
    decided_by: str,
    db_path: Path | str | None = None,
) -> None:
    allowed = {"open_document", "authorized", "metadata_only", "restricted", "review_required"}
    if new_status not in allowed:
        raise ValueError("유효하지 않은 권리 상태입니다.")
    if len(reason.strip()) < 10 or not decided_by.strip():
        raise ValueError("판정 근거(10자 이상)와 판정자를 기록해야 합니다.")
    with connect(db_path) as connection:
        current = connection.execute("SELECT rights_status FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not current:
            raise ValueError("첨부파일을 찾을 수 없습니다.")
        parser_status = "queued" if new_status in {"open_document", "authorized"} else "blocked_by_rights"
        connection.execute(
            """
            UPDATE attachments SET rights_status=?, rights_reason=?, parser_status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (new_status, reason.strip(), parser_status, attachment_id),
        )
        connection.execute(
            """
            INSERT INTO rights_decisions(attachment_id, previous_status, new_status, reason, decided_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (attachment_id, current["rights_status"], new_status, reason.strip(), decided_by.strip()),
        )


def _allowed_download_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    configured = tuple(
        suffix.strip().lower() for suffix in os.getenv("JOBNKILL_ALLOWED_DOWNLOAD_HOSTS", "").split(",") if suffix.strip()
    )
    hostname = parsed.hostname.lower()
    suffixes = ALLOWED_DOWNLOAD_HOST_SUFFIXES + configured
    return any(hostname == suffix.lstrip(".") or hostname.endswith(suffix) for suffix in suffixes)


def _document_directory() -> Path:
    configured = Path(os.getenv("JOBNKILL_DOCUMENT_DIR", "data/documents"))
    directory = configured if configured.is_absolute() else ROOT / configured
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def _download(url: str) -> tuple[bytes, str, str]:
    if not _allowed_download_url(url):
        raise ValueError("허용된 공식 도메인의 HTTPS 문서만 다운로드할 수 있습니다.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf, application/octet-stream"})
    with urllib.request.urlopen(request, timeout=45) as response:
        final_url = response.geturl()
        if not _allowed_download_url(final_url):
            raise ValueError("다운로드 리디렉션이 허용되지 않은 도메인을 가리킵니다.")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_DOCUMENT_BYTES:
            raise ValueError("문서가 25MB 제한을 넘었습니다.")
        payload = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise ValueError("문서가 25MB 제한을 넘었습니다.")
        return payload, response.headers.get("Content-Type", ""), final_url


def _pdf_pages(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as error:
                raise ValueError("암호화된 PDF는 처리할 수 없습니다.") from error
        return [(page.extract_text() or "") for page in reader.pages[:200]]
    except ImportError:
        executable = shutil.which("pdftotext")
        if not executable:
            raise ValueError("PDF 추출기(pypdf 또는 pdftotext)가 설치되어 있지 않습니다.")
        completed = subprocess.run(
            [executable, "-layout", str(path), "-"], check=True, capture_output=True, timeout=120
        )
        return [completed.stdout.decode("utf-8", errors="replace")]


def _hwpx_pages(path: Path) -> list[str]:
    pages: list[str] = []
    with zipfile.ZipFile(path) as archive:
        section_names = sorted(
            name for name in archive.namelist()
            if name.startswith("Contents/section") and name.endswith(".xml")
        )
        for name in section_names:
            root = ET.fromstring(archive.read(name))
            chunks = [element.text or "" for element in root.iter() if element.tag.split("}")[-1] == "t"]
            pages.append("\n".join(chunks))
    return pages


SECTION_LABELS: dict[str, tuple[str, ...]] = {
    "duties": ("직무수행내용", "주요업무", "주요 직무", "능력단위", "직무 내용"),
    "knowledge": ("필요지식", "필요 지식", "지식"),
    "skills": ("필요기술", "필요 기술", "기술"),
    "attitudes": ("직무수행태도", "직무 수행태도", "태도"),
    "qualifications": ("관련자격", "관련 자격", "자격요건", "지원자격"),
}


def extract_sections(pages: list[str]) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    sections = {key: [] for key in SECTION_LABELS}
    evidence: list[dict[str, Any]] = []
    current: str | None = None
    for page_number, page in enumerate(pages, start=1):
        for raw_line in page.splitlines():
            line = clean_text(raw_line, 2_000).strip("|·•○●□■▪ ")
            if not line:
                continue
            matched = False
            compact = re.sub(r"[\s:\[\]()<>]", "", line)
            for field_name, labels in SECTION_LABELS.items():
                label = next((item for item in labels if compact.startswith(re.sub(r"\s", "", item))), None)
                if label:
                    current = field_name
                    remainder = re.sub(rf"^\s*[\[<(]?\s*{re.escape(label)}\s*[\])>]?\s*[:：-]?\s*", "", line)
                    if remainder and remainder != line:
                        _add_section_value(sections, evidence, field_name, remainder, page_number)
                    matched = True
                    break
            if matched:
                continue
            if current and len(line) >= 2:
                _add_section_value(sections, evidence, current, line, page_number)
    for key in sections:
        sections[key] = sections[key][:100]
    return sections, evidence


def _add_section_value(
    sections: dict[str, list[str]], evidence: list[dict[str, Any]], field_name: str, line: str, page_number: int
) -> None:
    for chunk in re.split(r"\s*[○●•▪]\s*", line):
        value = clean_text(chunk.strip("-–—; "), 1_000)
        if len(value) < 2 or value in sections[field_name]:
            continue
        sections[field_name].append(value)
        evidence.append({"field_name": field_name, "field_value": value, "page_number": page_number, "excerpt": value[:300]})


def _parse_document(path: Path, content_type: str) -> tuple[list[str], str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf" or "pdf" in content_type.lower():
        return _pdf_pages(path), "pdf-sections-v1"
    if suffix == ".hwpx":
        return _hwpx_pages(path), "hwpx-sections-v1"
    if suffix == ".hwp":
        raise NotImplementedError("바이너리 HWP는 HWPX/PDF 변환 파이프라인이 연결될 때까지 격리됩니다.")
    raise NotImplementedError(f"지원하지 않는 문서 형식입니다: {suffix or content_type}")


def _extension(title: str, url: str, content_type: str) -> str:
    for candidate in (title, urllib.parse.urlparse(url).path):
        suffix = Path(candidate).suffix.lower()
        if suffix in {".pdf", ".hwpx", ".hwp"}:
            return suffix
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    return guessed or ".bin"


def _store_extracted_profile(
    connection: sqlite3.Connection,
    attachment: sqlite3.Row,
    sections: dict[str, list[str]],
    evidence: list[dict[str, Any]],
    parser_version: str,
) -> int:
    title = re.sub(r"\.(?:pdf|hwp|hwpx)$", "", str(attachment["title"]), flags=re.IGNORECASE)
    title = re.sub(r"(?:NCS\s*)?직무기술서", "", title, flags=re.IGNORECASE).strip(" _-[]()")
    job_title = title or str(attachment["posting_title"])
    existing = connection.execute(
        "SELECT id FROM job_profiles WHERE posting_id=? AND attachment_id=? ORDER BY id LIMIT 1",
        (int(attachment["posting_id"]), int(attachment["id"])),
    ).fetchone()
    summary = sections["duties"][0] if sections["duties"] else str(attachment["posting_title"])
    values = (
        str(attachment["institution_name"]), job_title, summary,
        json_text(sections["duties"]), json_text(sections["knowledge"]), json_text(sections["skills"]),
        json_text(sections["attitudes"]), json_text(sections["qualifications"]),
        json_text(list(dict.fromkeys(sections["duties"][:10] + sections["skills"][:10]))),
        str(attachment["rights_status"]), parser_version,
    )
    if existing:
        profile_id = int(existing["id"])
        connection.execute(
            """
            UPDATE job_profiles SET institution_name=?, job_title=?, summary=?, duties_json=?, knowledge_json=?,
              skills_json=?, attitudes_json=?, qualifications_json=?, keywords_json=?, extraction_status='machine_extracted',
              rights_status=?, parser_version=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
            """,
            (*values, profile_id),
        )
        connection.execute("DELETE FROM extraction_evidence WHERE job_profile_id=?", (profile_id,))
    else:
        cursor = connection.execute(
            """
            INSERT INTO job_profiles(
              posting_id, attachment_id, institution_name, job_title, summary, duties_json,
              knowledge_json, skills_json, attitudes_json, qualifications_json, keywords_json,
              extraction_status, rights_status, parser_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'machine_extracted', ?, ?)
            """,
            (int(attachment["posting_id"]), int(attachment["id"]), *values),
        )
        profile_id = int(cursor.lastrowid)
    for item in evidence:
        connection.execute(
            """
            INSERT INTO extraction_evidence(job_profile_id, field_name, field_value, page_number, source_url, excerpt)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (profile_id, item["field_name"], item["field_value"], item["page_number"], attachment["official_url"], item["excerpt"]),
        )
    return profile_id


def process_documents(db_path: Path | str | None = None, limit: int = 20) -> dict[str, int]:
    initialize(db_path)
    result = {"seen": 0, "parsed": 0, "unsupported": 0, "failed": 0}
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT a.*, p.title AS posting_title, p.id AS posting_id, i.name AS institution_name
            FROM attachments a
            JOIN postings p ON p.id = a.posting_id
            JOIN institutions i ON i.id = p.institution_id
            WHERE a.rights_status IN ('open_document', 'authorized')
              AND a.parser_status IN ('queued', 'failed')
            ORDER BY a.id LIMIT ?
            """,
            (min(max(limit, 1), 100),),
        ).fetchall()
        for attachment in rows:
            result["seen"] += 1
            try:
                payload, content_type, final_url = _download(str(attachment["official_url"]))
                digest = hashlib.sha256(payload).hexdigest()
                suffix = _extension(str(attachment["title"]), final_url, content_type)
                path = _document_directory() / f"{digest}{suffix}"
                path.write_bytes(payload)
                pages, parser_version = _parse_document(path, content_type)
                text = "\n\f\n".join(pages).strip()
                if not text:
                    raise ValueError("문서에서 텍스트를 추출하지 못했습니다. OCR 검토가 필요합니다.")
                sections, evidence = extract_sections(pages)
                _store_extracted_profile(connection, attachment, sections, evidence, parser_version)
                stored_path = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
                connection.execute(
                    """
                    UPDATE attachments SET storage_path=?, sha256=?, parser_status='parsed', extracted_text=?,
                      collected_at=?, last_checked_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (stored_path, digest, text[:2_000_000], now_iso(), now_iso(), int(attachment["id"])),
                )
                result["parsed"] += 1
            except NotImplementedError as error:
                connection.execute(
                    "UPDATE attachments SET parser_status='unsupported', rights_reason=rights_reason || ?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f" | 파서: {clean_text(error, 500)}", int(attachment["id"])),
                )
                result["unsupported"] += 1
            except Exception as error:
                connection.execute(
                    "UPDATE attachments SET parser_status='failed', rights_reason=rights_reason || ?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f" | 처리 오류: {clean_text(error, 500)}", int(attachment["id"])),
                )
                result["failed"] += 1
            connection.commit()
    return result
