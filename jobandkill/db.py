from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "jobandkill.db"


def database_path() -> Path:
    configured = os.getenv("JOBNKILL_DB_PATH")
    path = Path(configured) if configured else DEFAULT_DB_PATH
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize(path: Path | str | None = None) -> Path:
    db_path = Path(path) if path else database_path()
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with connect(db_path) as connection:
        connection.executescript(schema)
        connection.execute("PRAGMA user_version = 1")
    return db_path


def as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_value(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def public_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    counts = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM institutions) AS institutions,
          (SELECT COUNT(*) FROM postings) AS postings,
          (SELECT COUNT(*) FROM job_profiles WHERE extraction_status != 'rejected') AS profiles,
          (SELECT COUNT(*) FROM attachments WHERE parser_status = 'parsed') AS parsed_documents,
          (SELECT MAX(finished_at) FROM sync_runs WHERE status IN ('success', 'partial')) AS last_sync_at
        """
    ).fetchone()
    return dict(counts)


def list_sources(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT s.*,
               r.status AS last_run_status,
               r.finished_at AS last_run_at,
               r.records_seen AS last_records_seen,
               r.error_summary AS last_error
        FROM sources s
        LEFT JOIN sync_runs r ON r.id = (
          SELECT id FROM sync_runs WHERE source_id = s.id ORDER BY id DESC LIMIT 1
        )
        ORDER BY s.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _profile_payload(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "duties_json",
        "knowledge_json",
        "skills_json",
        "attitudes_json",
        "qualifications_json",
        "keywords_json",
        "ncs_categories_json",
        "positions_json",
        "regions_json",
    ):
        if key in result:
            result[key.removesuffix("_json")] = json_value(result.pop(key), [])
    return result


def search_profiles(
    connection: sqlite3.Connection,
    query: str = "",
    institution: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = " ".join(query.split())[:100]
    institution = " ".join(institution.split())[:100]
    limit = min(max(limit, 1), 50)
    offset = max(offset, 0)
    terms: list[str] = []
    params: list[Any] = []
    if institution:
        terms.append("jp.institution_name LIKE ?")
        params.append(f"%{institution}%")
    where = " AND ".join(terms) if terms else "1 = 1"
    select = """
        SELECT jp.id, jp.institution_name, jp.job_title, jp.ncs_code, jp.ncs_path,
               jp.summary, jp.duties_json, jp.knowledge_json, jp.skills_json,
               jp.attitudes_json, jp.qualifications_json, jp.keywords_json,
               jp.extraction_status, jp.rights_status, p.application_end,
               p.original_url, p.ncs_categories_json, p.positions_json, p.regions_json
        FROM job_profiles jp
        JOIN postings p ON p.id = jp.posting_id
    """
    order = """
        ORDER BY
          CASE WHEN p.application_end >= date('now') THEN 0 ELSE 1 END,
          p.application_end DESC,
          jp.updated_at DESC
        LIMIT ? OFFSET ?
    """
    rows: list[sqlite3.Row] = []
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", query)[:8]
    if tokens:
        fts_query = " OR ".join(f'"{token}"*' for token in tokens)
        rows = connection.execute(
            select
            + "JOIN job_profiles_fts ON job_profiles_fts.rowid = jp.id "
            + f"WHERE job_profiles_fts MATCH ? AND {where} AND jp.extraction_status != 'rejected' "
            + order,
            [fts_query, *params, limit, offset],
        ).fetchall()
    if query and not rows:
        pattern = f"%{query}%"
        fallback = (
            "(jp.job_title LIKE ? OR jp.institution_name LIKE ? OR jp.ncs_path LIKE ? "
            "OR jp.summary LIKE ? OR jp.duties_json LIKE ? OR jp.skills_json LIKE ?)"
        )
        rows = connection.execute(
            select + f"WHERE {fallback} AND {where} AND jp.extraction_status != 'rejected' " + order,
            [pattern] * 6 + params + [limit, offset],
        ).fetchall()
    elif not query:
        rows = connection.execute(
            select + f"WHERE {where} AND jp.extraction_status != 'rejected' " + order,
            params + [limit, offset],
        ).fetchall()
    return [_profile_payload(row) for row in rows]


def get_profile(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT jp.*, p.title AS posting_title, p.employment_type, p.hiring_type,
               p.education, p.regions_json, p.ncs_categories_json, p.positions_json,
               p.qualifications AS posting_qualifications, p.preferences,
               p.selection_process, p.application_method, p.application_start,
               p.application_end, p.original_url, s.name AS source_name,
               s.provider AS source_provider, s.license_note,
               a.title AS attachment_title, a.official_url AS attachment_url,
               a.license_code AS attachment_license_code
        FROM job_profiles jp
        JOIN postings p ON p.id = jp.posting_id
        JOIN sources s ON s.id = p.source_id
        LEFT JOIN attachments a ON a.id = jp.attachment_id
        WHERE jp.id = ? AND jp.extraction_status != 'rejected'
        """,
        (profile_id,),
    ).fetchone()
    if not row:
        return None
    result = _profile_payload(row)
    evidence = connection.execute(
        """
        SELECT field_name, field_value, page_number, source_url, excerpt
        FROM extraction_evidence WHERE job_profile_id = ? ORDER BY id
        """,
        (profile_id,),
    ).fetchall()
    result["evidence"] = [dict(item) for item in evidence]
    return result
