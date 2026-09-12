from __future__ import annotations

import hashlib
import html
import io
import json
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .db import Connection, DatabaseTarget, connect, initialize, insert_id, json_text
from .storage import (
    DocumentStore,
    LocalDocumentStore,
    S3DocumentStore,
    document_directory,
    get_document_store,
)


USER_AGENT = "JobAndKillCollector/0.1 (+https://github.com/jonandkill/Jobandkill)"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_HWPX_MEMBERS = 512
MAX_HWPX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_HWPX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_HWPX_COMPRESSION_RATIO = 200
MAX_HWPX_MEMBER_PATH_LENGTH = 512
HWPX_COMPRESSION_TYPES = frozenset((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))
HWPX_SECTION_PATH = re.compile(r"^Contents/section[0-9]+\.xml$")
ALLOWED_DOWNLOAD_HOST_SUFFIXES = (".data.go.kr", ".alio.go.kr", ".ncs.go.kr")
ALLOWED_DOCUMENT_RIGHTS = ("open_document", "authorized")
RESTRICTIVE_DOCUMENT_RIGHTS = ("metadata_only", "restricted", "review_required")
DOCUMENT_CLAIM_TIMEOUT = timedelta(hours=1)


class ConfigurationError(RuntimeError):
    pass


class LostDocumentClaim(RuntimeError):
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
    attachments_authoritative: bool = False
    attachment_errors: int = 0
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


def _document_recheck_due(value: Any) -> bool:
    try:
        days = int(os.getenv("JOBNKILL_DOCUMENT_RECHECK_DAYS", "7"))
    except ValueError:
        days = 7
    days = min(max(days, 1), 365)
    if not value:
        return True
    try:
        checked = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        return checked.astimezone(UTC) <= datetime.now(UTC) - timedelta(days=days)
    except (TypeError, ValueError):
        return True


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


def _attachments(
    record: dict[str, Any], base_url: str = ""
) -> tuple[list[AttachmentRecord], bool, int]:
    raw_items: list[Any] = []
    authoritative = False
    container_found = False
    for key in ATTACHMENT_LIST_KEYS:
        if key not in record:
            continue
        container_found = True
        candidate = record[key]
        if isinstance(candidate, list):
            raw_items = candidate
            authoritative = True
            break
        if isinstance(candidate, dict):
            if "item" in candidate or "items" in candidate:
                nested = candidate.get("item") if "item" in candidate else candidate.get("items")
                if isinstance(nested, dict):
                    raw_items = [nested]
                    authoritative = True
                    break
                if isinstance(nested, list):
                    raw_items = nested
                    authoritative = True
                    break
            elif candidate and all(isinstance(value, dict) for value in candidate.values()):
                raw_items = list(candidate.values())
                authoritative = True
                break
        return [], False, 1
    attachments: list[AttachmentRecord] = []
    errors = 0
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            errors += 1
            continue
        title = clean_text(pick(item, ATTACHMENT_TITLE_KEYS), 500) or f"첨부파일 {index + 1}"
        url = _absolute_https_url(pick(item, ATTACHMENT_URL_KEYS), base_url)
        if not url:
            errors += 1
            continue
        external_key = clean_text(pick(item, ATTACHMENT_ID_KEYS), 200)
        if not external_key:
            external_key = hashlib.sha256(url.encode()).hexdigest()[:24]
        media_type = clean_text(pick(item, ("mediaType", "contentType", "mimeType")), 100)
        license_code = clean_text(pick(item, ("licenseCode", "license", "koglType")), 100)
        attachments.append(
            AttachmentRecord(external_key, title, url, _attachment_kind(title), media_type, license_code)
        )
    return attachments, authoritative and errors == 0, errors if container_found else 0


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
    attachments, attachments_authoritative, attachment_errors = _attachments(record, base_url)
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
        attachments=attachments,
        attachments_authoritative=attachments_authoritative,
        attachment_errors=attachment_errors,
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
        self.max_pages = min(max(int(os.getenv("JOBNKILL_MAX_PAGES", "10")), 1), 1000)
        self.truncated = False
        self.total_count: int | None = None
        if not self.template or not self.service_key:
            raise ConfigurationError(
                "공공데이터포털 활용신청 후 JOBNKILL_ALIO_API_URL_TEMPLATE과 "
                "JOBNKILL_ALIO_SERVICE_KEY를 설정해야 합니다."
            )
        missing = {name for name in ("service_key", "page", "page_size") if "{" + name + "}" not in self.template}
        if missing:
            raise ConfigurationError("API URL 템플릿에 다음 자리표시자가 필요합니다: " + ", ".join(sorted(missing)))
        parsed = urllib.parse.urlparse(self.template)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname == "data.go.kr" or parsed.hostname.endswith(".data.go.kr")
        ):
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
            payload, content_type = _fetch_api_response(request)
            if len(payload) > 10 * 1024 * 1024:
                raise ValueError("API 응답이 10MB 제한을 넘었습니다.")
            records, total = parse_api_payload(payload, content_type)
            self.total_count = total
            if not records:
                self.truncated = total is not None and seen < total
                break
            yield records
            seen += len(records)
            if total is not None and seen >= total:
                break
            if len(records) < self.page_size:
                self.truncated = total is not None and seen < total
                break
            if page == self.max_pages:
                self.truncated = total is None or seen < total


def _fetch_api_response(request: urllib.request.Request) -> tuple[bytes, str]:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(10 * 1024 * 1024 + 1), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == 2:
                raise RuntimeError(f"공식 API 요청 실패: HTTP {error.code}") from error
            retry_after = error.headers.get("Retry-After", "")
            delay = min(int(retry_after), 30) if retry_after.isdigit() else 2 ** attempt
            time.sleep(delay)
        except urllib.error.URLError as error:
            if attempt == 2:
                raise RuntimeError("공식 API 네트워크 요청에 실패했습니다.") from error
            time.sleep(2 ** attempt)
    raise RuntimeError("공식 API 요청에 실패했습니다.")


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


def _source_row(connection: Connection, slug: str) -> Mapping[str, Any]:
    row = connection.execute("SELECT * FROM sources WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise ConfigurationError(f"등록되지 않은 소스입니다: {slug}")
    return row


@contextmanager
def _source_lease(connection: Connection, target: DatabaseTarget, source_slug: str) -> Iterator[None]:
    lock_name = f"jobandkill:source:{source_slug}"
    lock_file: Any = None
    if connection.dialect == "postgres":
        locked = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(?, 0)) AS locked", (lock_name,)
        ).fetchone()["locked"]
        if not locked:
            raise RuntimeError(f"이미 동기화 중인 소스입니다: {source_slug}")
    else:
        try:
            import fcntl
        except ImportError as error:
            raise RuntimeError("SQLite 소스 잠금은 Unix 계열 실행 환경이 필요합니다.") from error
        db_path = Path(target)
        lock_path = db_path.with_name(f"{db_path.name}.{source_slug}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise RuntimeError(f"이미 동기화 중인 소스입니다: {source_slug}") from error
    try:
        yield
    finally:
        if connection.dialect == "postgres":
            try:
                connection.execute("SELECT pg_advisory_unlock(hashtextextended(?, 0))", (lock_name,))
            except Exception:
                # Closing the PostgreSQL session releases the advisory lock even
                # when the caller's transaction is already in an aborted state.
                pass
        elif lock_file is not None:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def _upsert_institution(connection: Connection, record: PostingRecord) -> int:
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
            return insert_id(
                connection,
                "INSERT INTO institutions(name, institution_type) VALUES (?, ?)",
                (record.institution_name, record.institution_type),
            )
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


def _upsert_posting(connection: Connection, source_id: int, institution_id: int, record: PostingRecord) -> tuple[int, str]:
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
    posting_id = insert_id(
        connection,
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
    return posting_id, "created"


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
    connection: Connection,
    posting_id: int,
    default_rights: str,
    attachments: list[AttachmentRecord],
    authoritative: bool = True,
) -> int:
    for item in attachments:
        rights, reason = _attachment_rights(default_rights, item.license_code)
        lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
        existing = connection.execute(
            """
            SELECT id, rights_status, rights_reason, parser_status, title, official_url, media_type,
                   license_code, storage_backend, storage_key, storage_path, sha256, collected_at
            FROM attachments WHERE posting_id=? AND external_key=?
            """ + lock_suffix,
            (posting_id, item.external_key),
        ).fetchone()
        if existing:
            if not authoritative:
                continue
            content_changed = any(
                str(existing[name] or "") != value
                for name, value in (
                    ("title", item.title),
                    ("official_url", item.official_url),
                    ("media_type", item.media_type),
                    ("license_code", item.license_code),
                )
            )
            decision = connection.execute(
                """
                SELECT new_status FROM rights_decisions
                WHERE attachment_id=? ORDER BY id DESC LIMIT 1
                """,
                (int(existing["id"]),),
            ).fetchone()
            manual_status = str(decision["new_status"]) if decision else ""
            # A recorded institution-specific authorization remains usable, and
            # an explicit restrictive decision must never be reopened by a feed.
            # A feed-derived/open-document status is recalculated every sync so
            # an upstream license withdrawal blocks publication immediately.
            if (
                manual_status == "authorized"
                and not content_changed
                and str(existing["rights_status"]) == "authorized"
            ):
                effective_rights = manual_status
                effective_reason = str(existing["rights_reason"])
            elif manual_status in RESTRICTIVE_DOCUMENT_RIGHTS:
                effective_rights = manual_status
                effective_reason = str(existing["rights_reason"])
            elif manual_status == "authorized":
                effective_rights = "review_required"
                effective_reason = "기존 허가 후 첨부 식별정보가 변경되어 재승인이 필요함"
            elif str(existing["rights_status"]) in RESTRICTIVE_DOCUMENT_RIGHTS and rights == "open_document":
                effective_rights = "review_required"
                effective_reason = "철회·미확인 이력 후 개방표시가 다시 나타나 재검토가 필요함"
            else:
                effective_rights = rights
                effective_reason = reason
            if content_changed and (existing["storage_path"] or existing["storage_key"]):
                effective_rights = "review_required"
                effective_reason = "기존 원문을 정리하고 변경 문서의 권리를 다시 확인해야 함"
            was_allowed = str(existing["rights_status"]) in ALLOWED_DOCUMENT_RIGHTS
            is_allowed = effective_rights in ALLOWED_DOCUMENT_RIGHTS
            if is_allowed:
                parser_status = str(existing["parser_status"])
                if not was_allowed or content_changed:
                    parser_status = "queued"
                elif parser_status == "parsed" and _document_recheck_due(existing["collected_at"]):
                    parser_status = "queued"
            else:
                parser_status = "blocked_by_rights"
            if content_changed or not is_allowed:
                connection.execute(
                    "DELETE FROM job_profiles WHERE attachment_id=?", (int(existing["id"]),)
                )
            storage_key = str(existing["storage_key"] or "")
            storage_backend = str(existing["storage_backend"] or "local")
            discard_stored_content = content_changed or not is_allowed
            if discard_stored_content:
                _enqueue_attachment_uploads(
                    connection, int(existing["id"]),
                    "공식 피드의 문서 변경, 권리 제한 또는 철회 중인 업로드",
                )
            if discard_stored_content and storage_key:
                _enqueue_document_gc(
                    connection, storage_backend, storage_key,
                    "공식 피드의 문서 변경, 권리 제한 또는 철회",
                )
            connection.execute(
                """
                UPDATE attachments SET kind=?, title=?, official_url=?, media_type=?, license_code=?,
                  rights_status=?, rights_reason=?, rights_revision=rights_revision+1, parser_status=?,
                  extracted_text=CASE WHEN ? THEN NULL ELSE extracted_text END,
                  processing_started_at=CASE WHEN ?='not_requested' THEN processing_started_at ELSE NULL END,
                  processing_token=CASE WHEN ?='not_requested' THEN processing_token ELSE NULL END,
                  last_checked_at=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (item.kind, item.title, item.official_url, item.media_type, item.license_code,
                 effective_rights, effective_reason, parser_status, discard_stored_content, parser_status,
                 parser_status, now_iso(), int(existing["id"])),
            )
        else:
            if not authoritative:
                rights = "review_required"
                reason = "가져오기 파일의 기준시점을 확인할 수 없어 문서 권리 재검토가 필요함"
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


def _restrict_missing_attachments(
    connection: Connection, posting_id: int, attachments: list[AttachmentRecord]
) -> int:
    present = {item.external_key for item in attachments}
    lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
    rows = connection.execute(
        """
        SELECT id, external_key, rights_status, rights_reason, storage_backend, storage_key
        FROM attachments WHERE posting_id=?
        """ + lock_suffix,
        (posting_id,),
    ).fetchall()
    restricted = 0
    for row in rows:
        if str(row["external_key"]) in present:
            continue
        attachment_id = int(row["id"])
        was_allowed = str(row["rights_status"]) in ALLOWED_DOCUMENT_RIGHTS
        storage_key = str(row["storage_key"] or "")
        _enqueue_attachment_uploads(
            connection, attachment_id, "공식 공고에서 제거된 첨부의 진행 중 업로드"
        )
        if storage_key:
            _enqueue_document_gc(
                connection, str(row["storage_backend"] or "local"), storage_key,
                "공식 공고에서 첨부가 제거됨",
            )
        connection.execute("DELETE FROM job_profiles WHERE attachment_id=?", (attachment_id,))
        connection.execute(
            """
            UPDATE attachments SET
              rights_status=CASE WHEN ? THEN 'review_required' ELSE rights_status END,
              parser_status='blocked_by_rights',
              rights_reason=CASE WHEN ? THEN
                '공식 공고의 최신 첨부 목록에서 제거되어 재확인이 필요함'
                ELSE rights_reason END,
              rights_revision=rights_revision+1,
              extracted_text=NULL, processing_started_at=NULL, processing_token=NULL,
              updated_at=CURRENT_TIMESTAMP WHERE id=?
            """,
            (was_allowed, was_allowed, attachment_id),
        )
        restricted += 1
    return restricted


def _metadata_profile(connection: Connection, posting_id: int, record: PostingRecord) -> None:
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
    connection: Connection,
    source_slug: str,
    records: Iterable[dict[str, Any]],
    base_url: str,
    authoritative_attachments: bool = True,
) -> dict[str, int]:
    _lock_document_state(connection)
    source = _source_row(connection, source_slug)
    counters = {
        "seen": 0, "created": 0, "updated": 0, "unchanged": 0,
        "attachments": 0, "attachment_errors": 0, "skipped": 0,
    }
    for raw in records:
        counters["seen"] += 1
        normalized = normalize_api_record(raw, base_url)
        if not normalized:
            counters["skipped"] += 1
            continue
        institution_id = _upsert_institution(connection, normalized)
        posting_id, change = _upsert_posting(connection, int(source["id"]), institution_id, normalized)
        counters["attachment_errors"] += normalized.attachment_errors
        if change in counters:
            counters[change] += 1
        counters["attachments"] += _upsert_attachments(
            connection, posting_id, str(source["default_rights"]), normalized.attachments,
            authoritative=authoritative_attachments,
        )
        if authoritative_attachments and normalized.attachments_authoritative:
            _restrict_missing_attachments(connection, posting_id, normalized.attachments)
        _metadata_profile(connection, posting_id, normalized)
    return counters


SOURCE_FACTORIES = {
    "data-go-kr-alio": AlioApiSource,
    "ncs-job-description": NcsMetadataSource,
}


def sync_source(source_slug: str, db_path: DatabaseTarget | None = None) -> dict[str, Any]:
    target = initialize(db_path)
    with connect(target) as connection, _source_lease(connection, target, source_slug):
        source = _source_row(connection, source_slug)
        factory = SOURCE_FACTORIES.get(source_slug)
        if not factory:
            raise ConfigurationError(f"자동 수집기가 아직 연결되지 않은 소스입니다: {source_slug}")
        run_id = insert_id(
            connection,
            "INSERT INTO sync_runs(source_id, status) VALUES (?, 'running')",
            (int(source["id"]),),
        )
        connection.commit()
        totals = {
            "seen": 0, "created": 0, "updated": 0, "unchanged": 0,
            "attachments": 0, "attachment_errors": 0, "skipped": 0,
        }
        cleanup_quarantined = 0
        cleanup_failed = 0
        cleanup_due = 0
        cleanup_pending_total = 0
        cleanup_pending_attachments = 0
        try:
            adapter = factory()
            for page in adapter.fetch():
                result = ingest_records(connection, source_slug, page, str(source["base_url"]))
                for key in totals:
                    totals[key] += result[key]
                connection.commit()
                # Public text is blocked in the ingest transaction. Cleanup is
                # attempted after every page, but an unrelated retry backlog must
                # never starve later pages that may contain additional withdrawals.
                cleanup = purge_restricted_documents(target, _initialized=True)
                cleanup_quarantined = max(cleanup_quarantined, cleanup["quarantined"])
                cleanup_failed = max(cleanup_failed, cleanup["failed"])
                cleanup_due = max(cleanup_due, cleanup["due_remaining"])
                cleanup_pending_total = max(cleanup_pending_total, cleanup["pending_total"])
                cleanup_pending_attachments = max(
                    cleanup_pending_attachments, cleanup["pending_attachments"]
                )
            cleanup = purge_restricted_documents(target, _initialized=True)
            cleanup_quarantined = max(cleanup_quarantined, cleanup["quarantined"])
            cleanup_failed = max(cleanup_failed, cleanup["failed"])
            cleanup_due = cleanup["due_remaining"]
            cleanup_pending_total = cleanup["pending_total"]
            cleanup_pending_attachments = cleanup["pending_attachments"]
            truncated = bool(getattr(adapter, "truncated", False))
            status = "partial" if (
                totals["skipped"] or totals["attachment_errors"] or truncated
                or cleanup_quarantined or cleanup_failed or cleanup_due
                or cleanup_pending_attachments
            ) else "success"
            connection.execute(
                """
                UPDATE sync_runs SET status=?, finished_at=?, records_seen=?, records_created=?,
                  records_updated=?, attachments_seen=? WHERE id=?
                """,
                (status, now_iso(), totals["seen"], totals["created"], totals["updated"], totals["attachments"], run_id),
            )
            connection.execute("UPDATE sources SET last_success_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (now_iso(), int(source["id"])))
            connection.commit()
            return {
                "run_id": run_id,
                "source": source_slug,
                "status": status,
                "truncated": truncated,
                "reported_total": getattr(adapter, "total_count", None),
                "cleanup_quarantined": cleanup_quarantined,
                "cleanup_failed": cleanup_failed,
                "cleanup_due": cleanup_due,
                "cleanup_pending_total": cleanup_pending_total,
                "cleanup_pending_attachments": cleanup_pending_attachments,
                **totals,
            }
        except Exception as error:
            connection.rollback()
            connection.execute(
                "UPDATE sync_runs SET status='failed', finished_at=?, error_summary=? WHERE id=?",
                (now_iso(), clean_text(str(error), 1_000), run_id),
            )
            connection.commit()
            raise


def import_file(source_slug: str, input_path: Path | str, db_path: DatabaseTarget | None = None) -> dict[str, int]:
    path = Path(input_path)
    payload = path.read_bytes()
    records, _ = parse_api_payload(payload, mimetypes.guess_type(path.name)[0] or "")
    target = initialize(db_path)
    with connect(target) as connection, _source_lease(connection, target, source_slug):
        source = _source_row(connection, source_slug)
        # Keep PostgreSQL lock ordering consistent with schema migration:
        # finish the metadata read before ingest acquires document-state.
        connection.commit()
        result = ingest_records(
            connection, source_slug, records, str(source["base_url"]), authoritative_attachments=False
        )
        connection.commit()
    cleanup = purge_restricted_documents(target, _initialized=True)
    if cleanup["failed"] or cleanup["due_remaining"] or cleanup["pending_attachments"]:
        raise RuntimeError(
            "문서 권리 정리가 실패했거나 즉시 처리할 작업이 남아 재시도해야 합니다."
        )
    result["cleanup_quarantined"] = cleanup["quarantined"]
    result["cleanup_pending_total"] = cleanup["pending_total"]
    return result


def _lock_document_state(connection: Connection) -> None:
    if connection.dialect == "postgres":
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('jobandkill:document-state', 0))"
        )


def _begin_document_write(connection: Connection) -> None:
    if connection.dialect == "sqlite":
        connection.execute("BEGIN IMMEDIATE")
    _lock_document_state(connection)


@contextmanager
def _object_lease(target: DatabaseTarget, backend: str, key: str) -> Iterator[None]:
    lock_name = f"jobandkill:document:{backend}:{key}"
    is_postgres = isinstance(target, str) and target.startswith(("postgresql://", "postgres://"))
    if is_postgres:
        lock_connection = connect(target)
        try:
            lock_connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(?, 0))", (lock_name,)
            )
            lock_connection.commit()
            yield
        finally:
            try:
                lock_connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(?, 0))", (lock_name,)
                )
                lock_connection.commit()
            except Exception:
                pass
            lock_connection.close()
        return

    try:
        import fcntl
    except ImportError as error:
        raise RuntimeError("SQLite 객체 잠금은 Unix 계열 실행 환경이 필요합니다.") from error
    db_path = Path(target)
    lock_dir = db_path.parent / ".jobandkill-object-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / hashlib.sha256(lock_name.encode()).hexdigest()
    lock_file = lock_path.open("a+b")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _enqueue_document_gc(connection: Connection, backend: str, key: str, reason: str) -> None:
    if not key:
        return
    _lock_document_state(connection)
    connection.execute(
        """
        INSERT INTO document_gc_queue(storage_backend, storage_key, reason)
        VALUES (?, ?, ?)
        ON CONFLICT(storage_backend, storage_key) DO UPDATE SET
          reason=excluded.reason, updated_at=CURRENT_TIMESTAMP
        """,
        (backend or "local", key, clean_text(reason, 500)),
    )
    connection.execute(
        """
        UPDATE document_objects SET state='delete_pending', updated_at=CURRENT_TIMESTAMP
        WHERE storage_backend=? AND storage_key=?
        """,
        (backend or "local", key),
    )


def _enqueue_attachment_uploads(
    connection: Connection, attachment_id: int, reason: str
) -> set[tuple[str, str]]:
    """Fence and enqueue every registered upload owned by an attachment claim."""
    _lock_document_state(connection)
    rows = connection.execute(
        """
        SELECT storage_backend, storage_key FROM document_objects
        WHERE upload_attachment_id=? AND upload_claim_token IS NOT NULL
          AND state IN ('staging', 'delete_pending')
        """,
        (attachment_id,),
    ).fetchall()
    keys: set[tuple[str, str]] = set()
    for row in rows:
        backend = str(row["storage_backend"] or "local")
        key = str(row["storage_key"] or "")
        if not key:
            continue
        keys.add((backend, key))
        _enqueue_document_gc(connection, backend, key, reason)
    return keys


def _register_upload_intent(
    target: DatabaseTarget,
    attachment_id: int,
    claim_token: str,
    backend: str,
    key: str,
    digest: str,
) -> bool:
    """Register an upload only while the owning rights/claim CAS is still valid."""
    with connect(target) as connection:
        _begin_document_write(connection)
        lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
        owner = connection.execute(
            """
            SELECT rights_status, parser_status, processing_token
            FROM attachments WHERE id=?
            """ + lock_suffix,
            (attachment_id,),
        ).fetchone()
        if (
            not owner
            or str(owner["rights_status"]) not in ALLOWED_DOCUMENT_RIGHTS
            or str(owner["parser_status"]) != "not_requested"
            or str(owner["processing_token"] or "") != claim_token
        ):
            return False
        connection.execute(
            """
            INSERT INTO document_objects(
              storage_backend, storage_key, sha256, state, upload_attachment_id,
              upload_claim_token, upload_started_at
            ) VALUES (?, ?, ?, 'staging', ?, ?, ?)
            ON CONFLICT(storage_backend, storage_key) DO UPDATE SET
              sha256=excluded.sha256, state='staging', upload_started_at=excluded.upload_started_at,
              upload_attachment_id=excluded.upload_attachment_id,
              upload_claim_token=excluded.upload_claim_token,
              last_error='', updated_at=CURRENT_TIMESTAMP
            """,
            (backend, key, digest, attachment_id, claim_token, now_iso()),
        )
        return True


def _store_matches_legacy_reference(
    store: DocumentStore, backend: str, key: str, display_path: str
) -> bool:
    if backend == "local" and isinstance(store, LocalDocumentStore):
        candidate = Path(display_path)
        if not candidate.is_absolute():
            from .db import ROOT
            candidate = ROOT / candidate
        try:
            resolved = candidate.resolve()
            if key:
                return resolved == (store.root / key).resolve()
            return store.root in resolved.parents
        except OSError:
            return False
    if backend == "s3" and isinstance(store, S3DocumentStore):
        expected = f"s3://{store.bucket}/"
        return display_path == expected + key if key else display_path.startswith(expected)
    return False


def _ensure_store_namespace(target: DatabaseTarget, store: DocumentStore) -> None:
    """Pin a database backend to one physical root/bucket namespace."""
    with connect(target) as connection:
        _begin_document_write(connection)
        current = connection.execute(
            "SELECT location_id FROM storage_namespaces WHERE storage_backend=?",
            (store.backend,),
        ).fetchone()
        if current:
            if str(current["location_id"]) != store.location_id:
                raise RuntimeError(
                    "문서 저장 위치가 데이터베이스에 고정된 위치와 다릅니다. "
                    "기존 위치에서 정리하거나 명시적인 저장소 마이그레이션이 필요합니다."
                )
            return

        legacy_count = 0
        last_id = 0
        while True:
            legacy_rows = connection.execute(
                """
                SELECT id, storage_key, storage_path FROM attachments
                WHERE id>? AND (storage_backend=? OR (?='local' AND storage_backend=''))
                  AND storage_path IS NOT NULL
                ORDER BY id LIMIT 1000
                """,
                (last_id, store.backend, store.backend),
            ).fetchall()
            if not legacy_rows:
                break
            legacy_count += len(legacy_rows)
            if any(
                not _store_matches_legacy_reference(
                    store, store.backend, str(row["storage_key"] or ""), str(row["storage_path"])
                )
                for row in legacy_rows
            ):
                raise RuntimeError(
                    "기존 문서 객체의 실제 저장 위치를 확인할 수 없어 자동 연결을 중단했습니다."
                )
            last_id = int(legacy_rows[-1]["id"])
        if not legacy_count:
            unresolved = connection.execute(
                """
                SELECT (
                  (SELECT COUNT(*) FROM document_objects WHERE storage_backend=?) +
                  (SELECT COUNT(*) FROM document_gc_queue WHERE storage_backend=?)
                ) AS count
                """,
                (store.backend, store.backend),
            ).fetchone()
            if int(unresolved["count"]):
                raise RuntimeError(
                    "위치 정보가 없는 기존 문서 작업이 남아 있어 저장 위치를 자동 고정할 수 없습니다."
                )
        connection.execute(
            """
            INSERT INTO storage_namespaces(storage_backend, location_id)
            VALUES (?, ?) ON CONFLICT(storage_backend) DO NOTHING
            """,
            (store.backend, store.location_id),
        )
        pinned = connection.execute(
            "SELECT location_id FROM storage_namespaces WHERE storage_backend=?",
            (store.backend,),
        ).fetchone()
        if not pinned or str(pinned["location_id"]) != store.location_id:
            raise RuntimeError("다른 작업이 서로 다른 문서 저장 위치를 먼저 등록했습니다.")


def _normalize_legacy_storage_references(target: DatabaseTarget) -> int:
    """Convert safe storage_path-only rows before any shared-object count."""
    failed = 0
    last_id = 0
    while True:
        with connect(target) as connection:
            _begin_document_write(connection)
            lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
            rows = connection.execute(
                """
                SELECT id, storage_path FROM attachments
                WHERE id>? AND storage_path IS NOT NULL AND storage_key IS NULL
                  AND storage_backend IN ('', 'local')
                ORDER BY id LIMIT 1000
                """ + lock_suffix,
                (last_id,),
            ).fetchall()
            if not rows:
                return failed
            for row in rows:
                attachment_id = int(row["id"])
                try:
                    key = _legacy_storage_key(str(row["storage_path"]))
                except Exception as error:
                    failed += 1
                    connection.execute("DELETE FROM job_profiles WHERE attachment_id=?", (attachment_id,))
                    connection.execute(
                        """
                        UPDATE attachments SET rights_status='review_required',
                          rights_revision=rights_revision+1,
                          parser_status='blocked_by_rights', extracted_text=NULL,
                          processing_started_at=NULL, processing_token=NULL,
                          storage_backend='quarantine',
                          rights_reason=rights_reason || ?, last_checked_at=?, updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND storage_path=? AND storage_key IS NULL
                        """,
                        (
                            f" | 저장 위치 검증 오류: {clean_text(error, 300)}",
                            now_iso(), attachment_id, str(row["storage_path"]),
                        ),
                    )
                    continue
                converted = connection.execute(
                    """
                    UPDATE attachments SET storage_backend='local', storage_key=?,
                      updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND storage_path=? AND storage_key IS NULL
                    RETURNING sha256, collected_at
                    """,
                    (key, attachment_id, str(row["storage_path"])),
                ).fetchone()
                if not converted:
                    continue
                connection.execute(
                    """
                    INSERT INTO document_objects(
                      storage_backend, storage_key, sha256, state, ready_at
                    ) VALUES ('local', ?, ?, 'ready', ?)
                    ON CONFLICT(storage_backend, storage_key) DO NOTHING
                    """,
                    (key, str(converted["sha256"] or ""), converted["collected_at"]),
                )
            last_id = int(rows[-1]["id"])


def _queue_object_gc(
    target: DatabaseTarget, backend: str, key: str, reason: str, error: Exception | None = None
) -> None:
    with connect(target) as connection:
        _begin_document_write(connection)
        _enqueue_document_gc(connection, backend, key, reason)
        if error is not None:
            connection.execute(
                """
                UPDATE document_objects SET last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE storage_backend=? AND storage_key=?
                """,
                (clean_text(error, 500), backend, key),
            )


def _mark_object_ready(
    connection: Connection, backend: str, key: str, digest: str
) -> None:
    _lock_document_state(connection)
    # GC and finalization use queue-before-object ordering.
    connection.execute(
        "DELETE FROM document_gc_queue WHERE storage_backend=? AND storage_key=?",
        (backend, key),
    )
    connection.execute(
        """
        INSERT INTO document_objects(storage_backend, storage_key, sha256, state, ready_at)
        VALUES (?, ?, ?, 'ready', ?)
        ON CONFLICT(storage_backend, storage_key) DO UPDATE SET
          sha256=excluded.sha256, state='ready', ready_at=excluded.ready_at,
          upload_attachment_id=NULL, upload_claim_token=NULL,
          last_error='', updated_at=CURRENT_TIMESTAMP
        """,
        (backend, key, digest, now_iso()),
    )


def _allowed_storage_references(
    connection: Connection, backend: str, key: str, exclude_attachment_id: int | None = None
) -> int:
    params: list[Any] = [backend, backend, key]
    excluding = ""
    if exclude_attachment_id is not None:
        excluding = " AND id<>?"
        params.append(exclude_attachment_id)
    row = connection.execute(
        """
        SELECT COUNT(*) AS count FROM attachments
        WHERE (storage_backend=? OR (?='local' AND storage_backend='')) AND storage_key=?
          AND rights_status IN ('open_document', 'authorized')
        """ + excluding,
        params,
    ).fetchone()
    return int(row["count"])


def _legacy_storage_key(storage_path: str) -> str:
    candidate = Path(storage_path)
    if not candidate.is_absolute():
        from .db import ROOT
        candidate = ROOT / candidate
    candidate = candidate.resolve()
    root = document_directory()
    if root not in candidate.parents:
        raise RuntimeError("이전 버전 문서 경로가 허용된 문서 저장소 밖에 있습니다.")
    return candidate.relative_to(root).as_posix()


def _detach_restricted_attachment(
    target: DatabaseTarget, attachment_id: int
) -> tuple[str, str] | None:
    with connect(target) as connection:
        reference = connection.execute(
            "SELECT storage_backend, storage_key, storage_path FROM attachments WHERE id=?",
            (attachment_id,),
        ).fetchone()
    if reference and (reference["storage_key"] or reference["storage_path"]):
        backend = str(reference["storage_backend"] or "local")
        _ensure_store_namespace(target, get_document_store(backend))
    with connect(target) as connection:
        _begin_document_write(connection)
        lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
        current = connection.execute(
            """
            SELECT id, rights_status, storage_backend, storage_key, storage_path
            FROM attachments WHERE id=?
            """ + lock_suffix,
            (attachment_id,),
        ).fetchone()
        if not current or str(current["rights_status"]) in ALLOWED_DOCUMENT_RIGHTS:
            return None
        storage_key = str(current["storage_key"] or "")
        backend = str(current["storage_backend"] or "local")
        if not storage_key:
            if current["storage_path"]:
                backend = "local"
                storage_key = _legacy_storage_key(str(current["storage_path"]))
            else:
                return None
        _enqueue_document_gc(connection, backend, storage_key, "문서 권리 제한 또는 철회")
        # Keep the restricted reference until deletion succeeds (or a shared
        # allowed reference proves the object must be retained). This preserves
        # target-specific retry evidence across process restarts.
        return backend, storage_key


def _record_gc_failure(
    target: DatabaseTarget, backend: str, key: str, attempts: int, error: Exception
) -> None:
    delay = min(3_600, 30 * (2 ** min(attempts, 7)))
    retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
    message = clean_text(error, 500)
    with connect(target) as connection:
        _begin_document_write(connection)
        connection.execute(
            """
            UPDATE document_gc_queue SET attempts=attempts+1, next_attempt_at=?, last_error=?,
              updated_at=CURRENT_TIMESTAMP WHERE storage_backend=? AND storage_key=?
            """,
            (retry_at, message, backend, key),
        )
        connection.execute(
            """
            UPDATE document_objects SET state='delete_pending', last_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE storage_backend=? AND storage_key=?
            """,
            (message, backend, key),
        )


def drain_document_gc(
    db_path: DatabaseTarget | None = None,
    limit: int = 1_000,
    *,
    _initialized: bool = False,
    only_keys: Iterable[tuple[str, str]] | None = None,
) -> dict[str, int]:
    target = db_path if _initialized and db_path is not None else initialize(db_path)
    capped = min(max(limit, 1), 10_000)
    scoped_keys = sorted({(backend or "local", key) for backend, key in (only_keys or ()) if key})
    if only_keys is not None and not scoped_keys:
        return {
            "seen": 0, "deleted": 0, "retained": 0, "failed": 0,
            "pending_failed": 0, "pending_total": 0, "due_remaining": 0,
        }
    scope_sql = ""
    scope_params: list[Any] = []
    if scoped_keys:
        scope_sql = " AND (" + " OR ".join(
            "(storage_backend=? AND storage_key=?)" for _ in scoped_keys
        ) + ")"
        for backend, key in scoped_keys:
            scope_params.extend((backend, key))
    stale_at = (datetime.now(UTC) - DOCUMENT_CLAIM_TIMEOUT).replace(microsecond=0).isoformat()
    due_at = now_iso()
    with connect(target) as connection:
        _begin_document_write(connection)
        stale_comparison = (
            "datetime(o.updated_at) < datetime(?)"
            if connection.dialect == "sqlite"
            else "o.updated_at < ?"
        )
        connection.execute(
            """
            INSERT INTO document_gc_queue(storage_backend, storage_key, reason)
            SELECT storage_backend, storage_key, '완료되지 않은 업로드 정리' FROM document_objects o
            WHERE o.state='staging' AND """ + stale_comparison + """
              AND NOT EXISTS (
                SELECT 1 FROM attachments a WHERE a.storage_backend=o.storage_backend
                  AND a.storage_key=o.storage_key
                  AND a.rights_status IN ('open_document', 'authorized')
              )
            ON CONFLICT(storage_backend, storage_key) DO NOTHING
            """,
            (stale_at,),
        )
        connection.execute(
            """
            INSERT INTO document_gc_queue(storage_backend, storage_key, reason)
            SELECT DISTINCT CASE WHEN storage_backend='' THEN 'local' ELSE storage_backend END,
                   storage_key, '제한 상태 문서 정리'
            FROM attachments
            WHERE rights_status NOT IN ('open_document', 'authorized') AND storage_key IS NOT NULL
            ON CONFLICT(storage_backend, storage_key) DO NOTHING
            """
        )
        connection.execute(
            """
            UPDATE document_objects SET state='ready', ready_at=COALESCE(ready_at, CURRENT_TIMESTAMP),
              upload_attachment_id=NULL, upload_claim_token=NULL,
              last_error='', updated_at=CURRENT_TIMESTAMP
            WHERE state='staging' AND EXISTS (
              SELECT 1 FROM attachments a WHERE a.storage_backend=document_objects.storage_backend
                AND a.storage_key=document_objects.storage_key
                AND a.rights_status IN ('open_document', 'authorized')
            )
            """
        )
        due = connection.execute(
            """
            SELECT storage_backend, storage_key, attempts FROM document_gc_queue
            WHERE (next_attempt_at IS NULL OR next_attempt_at<=?)
            """ + scope_sql + """
            ORDER BY updated_at, storage_backend, storage_key LIMIT ?
            """,
            (due_at, *scope_params, capped),
        ).fetchall()

    result = {"seen": 0, "deleted": 0, "retained": 0, "failed": 0}
    stores: dict[str, DocumentStore] = {}
    for queued in due:
        backend, key = str(queued["storage_backend"]), str(queued["storage_key"])
        result["seen"] += 1
        with _object_lease(target, backend, key):
            with connect(target) as connection:
                _begin_document_write(connection)
                lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
                current = connection.execute(
                    """
                    SELECT attempts FROM document_gc_queue
                    WHERE storage_backend=? AND storage_key=?
                    """ + lock_suffix,
                    (backend, key),
                ).fetchone()
                if not current:
                    continue
                if _allowed_storage_references(connection, backend, key):
                    connection.execute(
                        """
                        UPDATE attachments SET storage_path=NULL, storage_backend='', storage_key=NULL,
                          sha256=NULL, processing_started_at=NULL, processing_token=NULL,
                          updated_at=CURRENT_TIMESTAMP
                        WHERE (storage_backend=? OR (?='local' AND storage_backend=''))
                          AND storage_key=?
                          AND rights_status NOT IN ('open_document', 'authorized')
                        """,
                        (backend, backend, key),
                    )
                    connection.execute(
                        "DELETE FROM document_gc_queue WHERE storage_backend=? AND storage_key=?",
                        (backend, key),
                    )
                    connection.execute(
                        """
                        UPDATE document_objects SET state='ready', upload_attachment_id=NULL,
                          upload_claim_token=NULL, last_error='', updated_at=CURRENT_TIMESTAMP
                        WHERE storage_backend=? AND storage_key=?
                        """,
                        (backend, key),
                    )
                    result["retained"] += 1
                    continue
                attempts = int(current["attempts"])
            try:
                store = stores.get(backend)
                if store is None:
                    store = get_document_store(backend)
                    _ensure_store_namespace(target, store)
                    stores[backend] = store
                store.delete(key)
            except Exception as error:
                _record_gc_failure(target, backend, key, attempts, error)
                result["failed"] += 1
                continue
            with connect(target) as connection:
                _begin_document_write(connection)
                # Every application reference promotion uses the same object
                # lease. This second check also repairs pre-migration/manual
                # writes that bypassed the lease.
                if _allowed_storage_references(connection, backend, key):
                    connection.execute(
                        """
                        UPDATE attachments SET storage_path=NULL, storage_backend='', storage_key=NULL,
                          sha256=NULL, parser_status='queued', updated_at=CURRENT_TIMESTAMP
                        WHERE storage_backend=? AND storage_key=?
                          AND rights_status IN ('open_document', 'authorized')
                        """,
                        (backend, key),
                    )
                connection.execute(
                    """
                    UPDATE attachments SET storage_path=NULL, storage_backend='', storage_key=NULL,
                      sha256=NULL, processing_started_at=NULL, processing_token=NULL,
                      updated_at=CURRENT_TIMESTAMP
                    WHERE (storage_backend=? OR (?='local' AND storage_backend=''))
                      AND storage_key=?
                      AND rights_status NOT IN ('open_document', 'authorized')
                    """,
                    (backend, backend, key),
                )
                connection.execute(
                    "DELETE FROM document_gc_queue WHERE storage_backend=? AND storage_key=?",
                    (backend, key),
                )
                connection.execute(
                    "DELETE FROM document_objects WHERE storage_backend=? AND storage_key=?",
                    (backend, key),
                )
            result["deleted"] += 1
    with connect(target) as connection:
        pending_total = int(connection.execute(
            "SELECT COUNT(*) AS count FROM document_gc_queue WHERE 1=1" + scope_sql,
            scope_params,
        ).fetchone()["count"])
        due_remaining = int(connection.execute(
            """
            SELECT COUNT(*) AS count FROM document_gc_queue
            WHERE (next_attempt_at IS NULL OR next_attempt_at<=?)
            """ + scope_sql,
            (due_at, *scope_params),
        ).fetchone()["count"])
        pending_failures = int(connection.execute(
            "SELECT COUNT(*) AS count FROM document_gc_queue WHERE last_error<>''" + scope_sql,
            scope_params,
        ).fetchone()["count"])
    result["pending_total"] = pending_total
    result["due_remaining"] = due_remaining
    result["pending_failed"] = pending_failures
    result["failed"] = max(result["failed"], pending_failures)
    return result


def purge_restricted_documents(
    db_path: DatabaseTarget | None = None, limit: int = 1_000, *, _initialized: bool = False
) -> dict[str, int]:
    target = db_path if _initialized and db_path is not None else initialize(db_path)
    capped = min(max(limit, 1), 10_000)
    _normalize_legacy_storage_references(target)
    with connect(target) as connection:
        _begin_document_write(connection)
        connection.execute(
            """
            DELETE FROM job_profiles WHERE attachment_id IN (
              SELECT id FROM attachments
              WHERE rights_status NOT IN ('open_document', 'authorized')
            )
            """
        )
        connection.execute(
            """
            UPDATE attachments SET extracted_text=NULL, parser_status='blocked_by_rights',
              processing_started_at=NULL, processing_token=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE rights_status NOT IN ('open_document', 'authorized')
              AND (extracted_text IS NOT NULL OR parser_status<>'blocked_by_rights'
                   OR processing_started_at IS NOT NULL OR processing_token IS NOT NULL)
            """
        )
    with connect(target) as connection:
        quarantined = int(connection.execute(
            """
            SELECT COUNT(*) AS count FROM attachments
            WHERE storage_backend='quarantine' AND storage_path IS NOT NULL
            """
        ).fetchone()["count"])
        rows = connection.execute(
            """
            SELECT id FROM attachments
            WHERE rights_status NOT IN ('open_document', 'authorized')
              AND storage_backend<>'quarantine'
              AND (storage_key IS NOT NULL OR storage_path IS NOT NULL)
            ORDER BY id LIMIT ?
            """,
            (capped,),
        ).fetchall()
    result = {
        "seen": 0, "detached": 0, "failed": 0,
        "deleted": 0, "pending_failed": 0, "pending_total": 0,
        "due_remaining": 0, "pending_attachments": 0,
        "quarantined": quarantined,
    }
    for row in rows:
        result["seen"] += 1
        try:
            if _detach_restricted_attachment(target, int(row["id"])):
                result["detached"] += 1
        except Exception as error:
            result["failed"] += 1
            with connect(target) as connection:
                connection.execute(
                    """
                    UPDATE attachments SET rights_reason=rights_reason || ?, last_checked_at=?,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (f" | 저장 정리 오류: {clean_text(error, 300)}", now_iso(), int(row["id"])),
                )
    gc_result = drain_document_gc(target, capped, _initialized=True)
    result["deleted"] = gc_result["deleted"]
    result["failed"] += gc_result["failed"]
    result["pending_failed"] = gc_result["pending_failed"]
    result["pending_total"] = gc_result["pending_total"]
    result["due_remaining"] = gc_result["due_remaining"]
    with connect(target) as connection:
        result["pending_attachments"] = int(connection.execute(
            """
            SELECT COUNT(*) AS count FROM attachments
            WHERE rights_status NOT IN ('open_document', 'authorized')
              AND storage_backend<>'quarantine'
              AND (storage_key IS NOT NULL OR storage_path IS NOT NULL)
            """
        ).fetchone()["count"])
    return result


def _attachment_identity(row: Mapping[str, Any]) -> str:
    identity = {
        "external_key": str(row["external_key"] or ""),
        "title": str(row["title"] or ""),
        "official_url": str(row["official_url"] or ""),
        "media_type": str(row["media_type"] or ""),
        "license_code": str(row["license_code"] or ""),
        "sha256": str(row["sha256"] or ""),
        "rights_status": str(row["rights_status"] or ""),
        "rights_reason": str(row["rights_reason"] or ""),
        "rights_revision": int(row["rights_revision"] or 0),
    }
    return hashlib.sha256(json_text(identity).encode()).hexdigest()


def get_attachment_rights_review(
    attachment_id: int, db_path: DatabaseTarget | None = None
) -> dict[str, Any]:
    """Return the safe review fields and CAS token required for a promotion."""
    target = initialize(db_path)
    with connect(target) as connection:
        row = connection.execute(
            """
            SELECT id, external_key, title, official_url, media_type, license_code,
                   rights_status, rights_reason, rights_revision, sha256, updated_at
            FROM attachments WHERE id=?
            """,
            (attachment_id,),
        ).fetchone()
    if not row:
        raise ValueError("첨부파일을 찾을 수 없습니다.")
    result = dict(row)
    result["identity"] = _attachment_identity(row)
    return result


def set_attachment_rights(
    attachment_id: int,
    new_status: str,
    reason: str,
    decided_by: str,
    db_path: DatabaseTarget | None = None,
    expected_identity: str | None = None,
) -> None:
    allowed = {"open_document", "authorized", "metadata_only", "restricted", "review_required"}
    if new_status not in allowed:
        raise ValueError("유효하지 않은 권리 상태입니다.")
    if len(reason.strip()) < 10 or not decided_by.strip():
        raise ValueError("판정 근거(10자 이상)와 판정자를 기록해야 합니다.")
    if new_status in ALLOWED_DOCUMENT_RIGHTS and not expected_identity:
        raise ValueError(
            "허용 판정에는 rights-info에서 확인한 --expected-identity가 필요합니다."
        )
    target = initialize(db_path)
    target_keys: set[tuple[str, str]] = set()
    # Promotions may verify their old object before it becomes user-visible.
    # Restrictive decisions never perform storage/network I/O before the rights
    # block commits; cleanup runs afterward with the reference evidence intact.
    with connect(target) as connection:
        legacy_reference = connection.execute(
            "SELECT rights_status, storage_backend, storage_key, storage_path "
            "FROM attachments WHERE id=?",
            (attachment_id,),
        ).fetchone()
    if not legacy_reference:
        raise ValueError("첨부파일을 찾을 수 없습니다.")
    if (
        new_status in ALLOWED_DOCUMENT_RIGHTS
        and (legacy_reference["storage_key"] or legacy_reference["storage_path"])
    ):
        try:
            legacy_backend = str(legacy_reference["storage_backend"] or "local")
            _ensure_store_namespace(target, get_document_store(legacy_backend))
        except Exception as error:
            raise RuntimeError(
                "기존 원문 저장 위치를 확인할 수 없어 허용 상태로 전환하지 않았습니다."
            ) from error
    with connect(target) as connection:
        _begin_document_write(connection)
        lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
        current = connection.execute(
            """
            SELECT rights_status, parser_status, storage_backend, storage_key, storage_path,
                   external_key, title, official_url, media_type, license_code, sha256,
                   rights_reason, rights_revision
            FROM attachments WHERE id=?
            """ + lock_suffix,
            (attachment_id,),
        ).fetchone()
        if not current:
            raise ValueError("첨부파일을 찾을 수 없습니다.")
        if (
            new_status in ALLOWED_DOCUMENT_RIGHTS
            and _attachment_identity(current) != expected_identity
        ):
            raise ValueError(
                "검토 후 첨부 식별정보가 변경되었습니다. rights-info로 다시 확인해야 합니다."
            )
        was_allowed = str(current["rights_status"]) in ALLOWED_DOCUMENT_RIGHTS
        becomes_allowed = new_status in ALLOWED_DOCUMENT_RIGHTS
        parser_status = (
            str(current["parser_status"]) if was_allowed and becomes_allowed
            else ("queued" if becomes_allowed else "blocked_by_rights")
        )
        should_purge = parser_status == "blocked_by_rights"
        old_backend = str(current["storage_backend"] or "local")
        old_key = str(current["storage_key"] or "")
        clear_old_reference = bool(old_key and becomes_allowed and not was_allowed)
        if becomes_allowed and not was_allowed and not old_key and current["storage_path"]:
            old_backend = "local"
            old_key = _legacy_storage_key(str(current["storage_path"]))
            clear_old_reference = True
        if should_purge:
            target_keys.update(_enqueue_attachment_uploads(
                connection, attachment_id, "수동 권리 제한 중인 업로드 정리"
            ))
        if clear_old_reference:
            target_keys.add((old_backend, old_key))
            _enqueue_document_gc(connection, old_backend, old_key, "권리 상태 변경 전 기존 객체 정리")
        elif should_purge and old_key:
            target_keys.add((old_backend, old_key))
            _enqueue_document_gc(connection, old_backend, old_key, "권리 제한 후 원문 객체 정리")
        if should_purge:
            connection.execute("DELETE FROM job_profiles WHERE attachment_id=?", (attachment_id,))
        connection.execute(
            """
            UPDATE attachments SET rights_status=?, rights_reason=?,
              rights_revision=rights_revision+1, parser_status=?,
              extracted_text=CASE WHEN ?='blocked_by_rights' THEN NULL ELSE extracted_text END,
              processing_started_at=CASE WHEN ?='not_requested' THEN processing_started_at ELSE NULL END,
              processing_token=CASE WHEN ?='not_requested' THEN processing_token ELSE NULL END,
              updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                new_status, reason.strip(), parser_status, parser_status,
                parser_status, parser_status, attachment_id,
            ),
        )
        if clear_old_reference:
            connection.execute(
                """
                UPDATE attachments SET storage_path=NULL, storage_backend='', storage_key=NULL, sha256=NULL
                WHERE id=?
                """,
                (attachment_id,),
            )
        decision_id = insert_id(
            connection,
            """
            INSERT INTO rights_decisions(attachment_id, previous_status, new_status, reason, decided_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (attachment_id, current["rights_status"], new_status, reason.strip(), decided_by.strip()),
        )
    cleanup: dict[str, int] | None = None
    detach_error: Exception | None = None
    if should_purge:
        _normalize_legacy_storage_references(target)
        try:
            detached_key = _detach_restricted_attachment(target, attachment_id)
            if detached_key:
                target_keys.add(detached_key)
        except Exception as error:
            detach_error = error
    if target_keys:
        cleanup = drain_document_gc(
            target, max(len(target_keys), 1), _initialized=True, only_keys=target_keys
        )
    if cleanup and cleanup["failed"]:
        raise RuntimeError(
            "권리 상태는 반영됐지만 원문 객체 삭제가 실패하여 재시도 대기 중입니다."
        )
    if cleanup and (cleanup["pending_total"] or cleanup["due_remaining"]):
        raise RuntimeError(
            "권리 상태는 반영됐지만 대상 원문 객체 삭제가 아직 완료되지 않았습니다."
        )
    with connect(target) as connection:
        current_state = connection.execute(
            """
            SELECT rights_status, storage_backend, storage_key, storage_path FROM attachments WHERE id=?
            """,
            (attachment_id,),
        ).fetchone()
        latest_decision = connection.execute(
            "SELECT id FROM rights_decisions WHERE attachment_id=? ORDER BY id DESC LIMIT 1",
            (attachment_id,),
        ).fetchone()
        active_uploads = int(connection.execute(
            """
            SELECT COUNT(*) AS count FROM document_objects
            WHERE upload_attachment_id=? AND state='staging'
            """,
            (attachment_id,),
        ).fetchone()["count"])
    if current_state and str(current_state["storage_backend"]) == "quarantine":
        raise RuntimeError(
            "권리 상태는 차단했지만 이전 원문 위치를 검증할 수 없어 격리했습니다."
        )
    if should_purge and current_state and (
        current_state["storage_key"] or current_state["storage_path"] or active_uploads
    ):
        raise RuntimeError(
            "권리 상태는 차단했지만 대상 원문 또는 진행 중 업로드 정리가 완료되지 않았습니다."
        )
    if detach_error is not None:
        raise RuntimeError(
            "권리 상태는 차단했지만 대상 원문 저장 위치를 확인하거나 정리하지 못했습니다."
        ) from detach_error
    if (
        not current_state
        or str(current_state["rights_status"]) != new_status
        or not latest_decision
        or int(latest_decision["id"]) != decision_id
    ):
        raise RuntimeError("권리 판정 직후 다른 판정이 반영되어 현재 상태를 다시 확인해야 합니다.")


def _allowed_download_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    configured = tuple(
        suffix.strip().lower().lstrip(".")
        for suffix in os.getenv("JOBNKILL_ALLOWED_DOWNLOAD_HOSTS", "").split(",") if suffix.strip()
    )
    hostname = parsed.hostname.lower()
    suffixes = tuple(suffix.lstrip(".") for suffix in ALLOWED_DOWNLOAD_HOST_SUFFIXES) + configured
    return any(hostname == suffix or hostname.endswith("." + suffix) for suffix in suffixes)


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


def _pdf_pages(document: Path | bytes) -> list[str]:
    try:
        from pypdf import PdfReader

        source: Any = io.BytesIO(document) if isinstance(document, bytes) else str(document)
        reader = PdfReader(source, strict=False)
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
        command = [executable, "-layout", "-", "-"] if isinstance(document, bytes) else [
            executable, "-layout", str(document), "-",
        ]
        completed = subprocess.run(
            command,
            input=document if isinstance(document, bytes) else None,
            check=True,
            capture_output=True,
            timeout=120,
        )
        return [completed.stdout.decode("utf-8", errors="replace")]


def _unsafe_hwpx(reason: str) -> ValueError:
    return ValueError(f"안전 제한을 충족하지 않는 HWPX 문서입니다: {reason}")


def _validate_hwpx_member(info: zipfile.ZipInfo, seen_names: set[str]) -> None:
    name = info.filename
    if (
        not name
        or len(name) > MAX_HWPX_MEMBER_PATH_LENGTH
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
    ):
        raise _unsafe_hwpx("허용되지 않은 압축 항목 경로")
    path = name[:-1] if name.endswith("/") else name
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise _unsafe_hwpx("허용되지 않은 압축 항목 경로")
    if name in seen_names:
        raise _unsafe_hwpx("중복된 압축 항목 경로")
    seen_names.add(name)
    if info.flag_bits & 0x1:
        raise _unsafe_hwpx("암호화된 압축 항목")
    if info.compress_type not in HWPX_COMPRESSION_TYPES:
        raise _unsafe_hwpx("지원하지 않는 압축 방식")
    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode) if info.create_system == 3 else 0
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise _unsafe_hwpx("일반 파일이 아닌 압축 항목")
    if info.file_size < 0 or info.compress_size < 0:
        raise _unsafe_hwpx("올바르지 않은 압축 항목 크기")
    if info.file_size > MAX_HWPX_MEMBER_BYTES:
        raise _unsafe_hwpx("개별 압축 항목 크기 제한 초과")
    if info.file_size and (
        info.compress_size == 0
        or info.file_size > info.compress_size * MAX_HWPX_COMPRESSION_RATIO
    ):
        raise _unsafe_hwpx("개별 압축 항목의 압축비 제한 초과")


def _hwpx_pages(document: Path | bytes) -> list[str]:
    compressed_size = len(document) if isinstance(document, bytes) else document.stat().st_size
    if compressed_size > MAX_DOCUMENT_BYTES:
        raise _unsafe_hwpx("25MB 압축 파일 크기 제한 초과")
    pages: list[str] = []
    source: Any = io.BytesIO(document) if isinstance(document, bytes) else document
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_HWPX_MEMBERS:
                raise _unsafe_hwpx("압축 항목 수 제한 초과")
            seen_names: set[str] = set()
            total_uncompressed = 0
            total_compressed = 0
            sections: list[zipfile.ZipInfo] = []
            for info in members:
                _validate_hwpx_member(info, seen_names)
                total_uncompressed += info.file_size
                total_compressed += info.compress_size
                if total_uncompressed > MAX_HWPX_UNCOMPRESSED_BYTES:
                    raise _unsafe_hwpx("전체 압축 해제 크기 제한 초과")
                if HWPX_SECTION_PATH.fullmatch(info.filename) and not info.is_dir():
                    sections.append(info)
            if total_uncompressed and (
                total_compressed == 0
                or total_uncompressed > total_compressed * MAX_HWPX_COMPRESSION_RATIO
            ):
                raise _unsafe_hwpx("전체 압축비 제한 초과")
            if not sections:
                raise _unsafe_hwpx("직무 내용 섹션 XML 없음")
            for info in sorted(sections, key=lambda item: item.filename):
                with archive.open(info) as member:
                    xml_payload = member.read(MAX_HWPX_MEMBER_BYTES + 1)
                if len(xml_payload) > MAX_HWPX_MEMBER_BYTES or len(xml_payload) != info.file_size:
                    raise _unsafe_hwpx("압축 항목의 실제 크기 불일치")
                try:
                    root = ET.fromstring(xml_payload)
                except ET.ParseError as error:
                    raise _unsafe_hwpx("올바르지 않은 섹션 XML") from error
                chunks = [
                    element.text or ""
                    for element in root.iter()
                    if element.tag.split("}")[-1] == "t"
                ]
                pages.append("\n".join(chunks))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise _unsafe_hwpx("올바르지 않은 ZIP 구조") from error
    return pages


SECTION_LABELS: dict[str, tuple[str, ...]] = {
    "duties": ("직무수행내용", "주요업무", "주요 직무", "능력단위", "직무 내용"),
    "knowledge": ("필요지식", "필요 지식", "지식"),
    "skills": ("필요기술", "필요 기술", "기술"),
    "attitudes": ("직무수행태도", "직무 수행태도", "태도"),
    "qualifications": ("관련자격", "관련 자격", "자격요건", "지원자격"),
}
MAX_SECTION_VALUES_PER_FIELD = 100
MAX_EXTRACTION_EVIDENCE_ITEMS = 500


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
    return sections, evidence


def _add_section_value(
    sections: dict[str, list[str]], evidence: list[dict[str, Any]], field_name: str, line: str, page_number: int
) -> None:
    for chunk in re.split(r"\s*[○●•▪]\s*", line):
        value = clean_text(chunk.strip("-–—; "), 1_000)
        if len(value) < 2 or value in sections[field_name]:
            continue
        if (
            len(sections[field_name]) >= MAX_SECTION_VALUES_PER_FIELD
            or len(evidence) >= MAX_EXTRACTION_EVIDENCE_ITEMS
        ):
            raise ValueError("문서 추출 항목이 안전 한도를 초과했습니다.")
        sections[field_name].append(value)
        evidence.append({"field_name": field_name, "field_value": value, "page_number": page_number, "excerpt": value[:300]})


def _parse_document(
    document: Path | bytes, content_type: str, suffix_hint: str = ""
) -> tuple[list[str], str]:
    suffix = suffix_hint.lower() if isinstance(document, bytes) else document.suffix.lower()
    if suffix == ".pdf" or "pdf" in content_type.lower():
        return _pdf_pages(document), "pdf-sections-v1"
    if suffix == ".hwpx":
        return _hwpx_pages(document), "hwpx-sections-v1"
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
    connection: Connection,
    attachment: Mapping[str, Any],
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
        profile_id = insert_id(
            connection,
            """
            INSERT INTO job_profiles(
              posting_id, attachment_id, institution_name, job_title, summary, duties_json,
              knowledge_json, skills_json, attitudes_json, qualifications_json, keywords_json,
              extraction_status, rights_status, parser_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'machine_extracted', ?, ?)
            """,
            (int(attachment["posting_id"]), int(attachment["id"]), *values),
        )
    for item in evidence:
        connection.execute(
            """
            INSERT INTO extraction_evidence(job_profile_id, field_name, field_value, page_number, source_url, excerpt)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (profile_id, item["field_name"], item["field_value"], item["page_number"], attachment["official_url"], item["excerpt"]),
        )
    return profile_id


def _claim_document(target: DatabaseTarget, attachment_id: int) -> dict[str, Any] | None:
    claimed_at = now_iso()
    claim_token = uuid.uuid4().hex
    with connect(target) as connection:
        _begin_document_write(connection)
        cursor = connection.execute(
            """
            UPDATE attachments SET parser_status='not_requested', processing_started_at=?, processing_token=?,
              last_checked_at=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND rights_status IN ('open_document', 'authorized')
              AND parser_status IN ('queued', 'failed')
            """,
            (claimed_at, claim_token, claimed_at, attachment_id),
        )
        if cursor.rowcount != 1:
            return None
        row = connection.execute(
            """
            SELECT a.*, p.title AS posting_title, p.id AS posting_id, i.name AS institution_name
            FROM attachments a
            JOIN postings p ON p.id = a.posting_id
            JOIN institutions i ON i.id = p.institution_id
            WHERE a.id=?
            """,
            (attachment_id,),
        ).fetchone()
        return dict(row) if row else None


def _mark_document_outcome(
    target: DatabaseTarget, attachment_id: int, claim_token: str, status: str, message: str
) -> bool:
    with connect(target) as connection:
        _begin_document_write(connection)
        cursor = connection.execute(
            """
            UPDATE attachments SET parser_status=?, processing_started_at=NULL, processing_token=NULL,
              rights_reason=rights_reason || ?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND rights_status IN ('open_document', 'authorized')
              AND parser_status='not_requested' AND processing_token=?
            """,
            (status, message, attachment_id, claim_token),
        )
        return cursor.rowcount == 1


def _preflight_document_digest(
    target: DatabaseTarget, attachment_id: int, claim_token: str, digest: str
) -> bool:
    """Fence a claim and demote changed authorized bytes before any parsing/upload."""
    with connect(target) as connection:
        _begin_document_write(connection)
        lock_suffix = " FOR UPDATE" if connection.dialect == "postgres" else ""
        current = connection.execute(
            """
            SELECT rights_status, parser_status, processing_started_at, processing_token,
                   sha256, storage_backend, storage_key
            FROM attachments WHERE id=?
            """ + lock_suffix,
            (attachment_id,),
        ).fetchone()
        if (
            not current
            or str(current["rights_status"]) not in ALLOWED_DOCUMENT_RIGHTS
            or str(current["parser_status"]) != "not_requested"
            or not current["processing_started_at"]
            or str(current["processing_token"] or "") != claim_token
        ):
            return False
        if not (
            str(current["rights_status"]) == "authorized"
            and current["sha256"]
            and str(current["sha256"]) != digest
        ):
            return True

        old_backend = str(current["storage_backend"] or "local")
        old_key = str(current["storage_key"] or "")
        _enqueue_attachment_uploads(
            connection, attachment_id, "재검증 중 승인 원문 바이트 변경 감지"
        )
        if old_key:
            _enqueue_document_gc(
                connection, old_backend, old_key, "재검증 중 승인 원문 바이트 변경 감지"
            )
        connection.execute("DELETE FROM job_profiles WHERE attachment_id=?", (attachment_id,))
        reason = "승인된 원문과 다른 바이트가 확인되어 새 문서의 재승인이 필요함"
        changed = connection.execute(
            """
            UPDATE attachments SET rights_status='review_required', rights_reason=?,
              rights_revision=rights_revision+1,
              parser_status='blocked_by_rights', extracted_text=NULL,
              processing_started_at=NULL, processing_token=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND rights_status='authorized' AND parser_status='not_requested'
              AND processing_token=?
            """,
            (reason, attachment_id, claim_token),
        )
        if changed.rowcount == 1:
            connection.execute(
                """
                INSERT INTO rights_decisions(
                  attachment_id, previous_status, new_status, reason, decided_by
                ) VALUES (?, 'authorized', 'review_required', ?, 'system:digest-revalidation')
                """,
                (attachment_id, reason),
            )
        return False


def _finalize_document(
    target: DatabaseTarget,
    attachment_id: int,
    claim_token: str,
    payload: bytes,
    digest: str,
    suffix: str,
    content_type: str,
    text: str,
    sections: dict[str, list[str]],
    evidence: list[dict[str, Any]],
    parser_version: str,
    store: DocumentStore,
) -> bool:
    intended_key = store.key_for(digest, suffix)
    old_reference: tuple[str, str] | None = None
    finalized = False
    upload_registered = False
    try:
        with _object_lease(target, store.backend, intended_key):
            if _preflight_document_digest(target, attachment_id, claim_token, digest):
                # The intent and upload share the object lease. If this worker is
                # terminated after put(), rights changes can find this owned intent.
                _ensure_store_namespace(target, store)
                upload_registered = _register_upload_intent(
                    target, attachment_id, claim_token, store.backend, intended_key, digest
                )
                if upload_registered:
                    stored = store.put(payload, digest, suffix, content_type)
                    if stored.key != intended_key or stored.backend != store.backend:
                        raise RuntimeError("문서 저장소가 예정된 객체 키와 다른 결과를 반환했습니다.")
                    with connect(target) as connection:
                        _begin_document_write(connection)
                        lock_suffix = " FOR UPDATE OF a" if connection.dialect == "postgres" else ""
                        current = connection.execute(
                            """
                            SELECT a.*, p.title AS posting_title, p.id AS posting_id,
                                   i.name AS institution_name
                            FROM attachments a
                            JOIN postings p ON p.id = a.posting_id
                            JOIN institutions i ON i.id = p.institution_id
                            WHERE a.id=?
                            """ + lock_suffix,
                            (attachment_id,),
                        ).fetchone()
                        if (
                            not current
                            or str(current["rights_status"]) not in ALLOWED_DOCUMENT_RIGHTS
                            or str(current["parser_status"]) != "not_requested"
                            or not current["processing_started_at"]
                            or str(current["processing_token"] or "") != claim_token
                        ):
                            _enqueue_document_gc(
                                connection, store.backend, intended_key, "업로드 중 작업 권한 변경"
                            )
                        else:
                            old_backend = str(current["storage_backend"] or "")
                            old_key = str(current["storage_key"] or "")
                            if old_backend and old_key:
                                old_reference = (old_backend, old_key)
                            _store_extracted_profile(
                                connection, current, sections, evidence, parser_version
                            )
                            cursor = connection.execute(
                                """
                                UPDATE attachments SET storage_path=?, storage_backend=?, storage_key=?,
                                  sha256=?, parser_status='parsed', processing_started_at=NULL,
                                  processing_token=NULL, extracted_text=?, collected_at=?, last_checked_at=?,
                                  updated_at=CURRENT_TIMESTAMP
                                WHERE id=? AND rights_status IN ('open_document', 'authorized')
                                  AND parser_status='not_requested' AND processing_started_at IS NOT NULL
                                  AND processing_token=?
                                """,
                                (
                                    stored.display_path, stored.backend, stored.key, digest,
                                    text[:2_000_000], now_iso(), now_iso(), attachment_id, claim_token,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise LostDocumentClaim(
                                    "문서 처리 권한 또는 작업 소유권이 변경되었습니다."
                                )
                            _mark_object_ready(connection, stored.backend, stored.key, digest)
                            if old_reference and old_reference != (stored.backend, stored.key):
                                _enqueue_document_gc(
                                    connection, old_reference[0], old_reference[1], "문서 객체 교체"
                                )
                            finalized = True
        if not finalized:
            purge_restricted_documents(target, _initialized=True)
        elif old_reference and old_reference != (store.backend, intended_key):
            drain_document_gc(target, _initialized=True)
        return finalized
    except Exception as error:
        if upload_registered:
            _queue_object_gc(target, store.backend, intended_key, "업로드 또는 참조 확정 실패", error)
            drain_document_gc(target, _initialized=True)
        raise


def process_documents(db_path: DatabaseTarget | None = None, limit: int = 20) -> dict[str, int]:
    target = initialize(db_path)
    capped = min(max(limit, 1), 100)
    result = {
        "seen": 0, "parsed": 0, "unsupported": 0, "failed": 0,
        "rights_blocked": 0, "gc_failed": 0, "gc_pending": 0,
        "gc_backlog": 0, "quarantined": 0,
    }
    cleanup = purge_restricted_documents(target, _initialized=True)
    result["gc_failed"] = cleanup["failed"]
    result["gc_pending"] = cleanup["due_remaining"] + cleanup["pending_attachments"]
    result["gc_backlog"] = cleanup["pending_total"]
    result["quarantined"] = cleanup["quarantined"]
    cutoff = (datetime.now(UTC) - DOCUMENT_CLAIM_TIMEOUT).replace(microsecond=0).isoformat()
    with connect(target) as connection:
        connection.execute(
            """
            UPDATE attachments SET parser_status='failed', processing_started_at=NULL, processing_token=NULL,
              updated_at=CURRENT_TIMESTAMP
            WHERE rights_status IN ('open_document', 'authorized')
              AND parser_status='not_requested'
              AND (processing_started_at IS NULL OR processing_started_at<?)
            """,
            (cutoff,),
        )
        rows = connection.execute(
            """
            SELECT id FROM attachments
            WHERE rights_status IN ('open_document', 'authorized')
              AND parser_status IN ('queued', 'failed')
            ORDER BY id LIMIT ?
            """,
            (capped,),
        ).fetchall()
    store = get_document_store() if rows else None
    for row in rows:
        attachment = _claim_document(target, int(row["id"]))
        if not attachment:
            continue
        attachment_id = int(attachment["id"])
        claim_token = str(attachment["processing_token"])
        result["seen"] += 1
        try:
            payload, content_type, final_url = _download(str(attachment["official_url"]))
            digest = hashlib.sha256(payload).hexdigest()
            if not _preflight_document_digest(target, attachment_id, claim_token, digest):
                purge_restricted_documents(target, _initialized=True)
                result["rights_blocked"] += 1
                continue
            suffix = _extension(str(attachment["title"]), final_url, content_type)
            pages, parser_version = _parse_document(payload, content_type, suffix)
            text = "\n\f\n".join(pages).strip()
            if not text:
                raise ValueError("문서에서 텍스트를 추출하지 못했습니다. OCR 검토가 필요합니다.")
            sections, evidence = extract_sections(pages)
            finalized = _finalize_document(
                target, attachment_id, claim_token, payload, digest, suffix, content_type, text,
                sections, evidence, parser_version, store,
            )
            result["parsed" if finalized else "rights_blocked"] += 1
        except NotImplementedError as error:
            marked = _mark_document_outcome(
                target, attachment_id, claim_token, "unsupported", f" | 파서: {clean_text(error, 500)}"
            )
            result["unsupported" if marked else "rights_blocked"] += 1
        except LostDocumentClaim:
            result["rights_blocked"] += 1
        except Exception as error:
            marked = _mark_document_outcome(
                target, attachment_id, claim_token, "failed", f" | 처리 오류: {clean_text(error, 500)}"
            )
            result["failed" if marked else "rights_blocked"] += 1
    final_cleanup = purge_restricted_documents(target, _initialized=True)
    result["gc_failed"] = max(result["gc_failed"], final_cleanup["failed"])
    result["gc_pending"] = (
        final_cleanup["due_remaining"] + final_cleanup["pending_attachments"]
    )
    result["gc_backlog"] = final_cleanup["pending_total"]
    result["quarantined"] = max(result["quarantined"], final_cleanup["quarantined"])
    return result
