from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "jobandkill.db"
DatabaseTarget = Path | str
Params = Sequence[Any] | Mapping[str, Any]
POSTGRES_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})


def database_path() -> Path:
    configured = os.getenv("JOBNKILL_DB_PATH")
    path = Path(configured) if configured else DEFAULT_DB_PATH
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def database_target(target: DatabaseTarget | None = None) -> DatabaseTarget:
    """Resolve a DB target without exposing credentials in normal output."""
    if target is not None:
        return target
    configured = os.getenv("JOBNKILL_DATABASE_URL", os.getenv("DATABASE_URL", "")).strip()
    if configured:
        if not configured.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL은 PostgreSQL URL이어야 합니다.")
        return configured
    return database_path()


def render_private_postgres_url(database_url: str) -> bool:
    """Return whether a URL targets Render's same-region private network."""
    parsed = urlsplit(database_url)
    return bool(
        os.getenv("RENDER", "").strip().lower() == "true"
        and parsed.scheme in {"postgresql", "postgres"}
        and parsed.hostname
        and "." not in parsed.hostname
    )


def postgres_sslmode(database_url: str) -> str:
    return parse_qs(urlsplit(database_url).query).get(
        "sslmode", [os.getenv("PGSSLMODE", "")]
    )[-1]


class Connection:
    """Small DB-API compatibility layer for SQLite and psycopg."""

    def __init__(self, raw: Any, dialect: str) -> None:
        self.raw = raw
        self.dialect = dialect

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self.dialect == "postgres" else statement

    def execute(self, statement: str, params: Params = ()) -> Any:
        return self.raw.execute(self._sql(statement), params)

    def executescript(self, script: str) -> None:
        if self.dialect == "sqlite":
            self.raw.executescript(script)
        else:
            for statement in script.split(";"):
                if statement.strip():
                    self.raw.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


def connect(target: DatabaseTarget | None = None) -> Connection:
    resolved = database_target(target)
    if isinstance(resolved, str) and resolved.startswith(("postgresql://", "postgres://")):
        sslmode = postgres_sslmode(resolved)
        if (
            os.getenv("JOBNKILL_ENV", "development").strip().lower() == "production"
            and sslmode not in POSTGRES_TLS_MODES
            and not render_private_postgres_url(resolved)
        ):
            raise RuntimeError("운영 PostgreSQL은 sslmode=require 이상으로 암호화해야 합니다.")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL 운영에는 requirements-production.txt의 psycopg가 필요합니다."
            ) from error
        try:
            raw = psycopg.connect(resolved, row_factory=dict_row, connect_timeout=10)
        except Exception:
            raise RuntimeError("PostgreSQL 데이터베이스에 연결할 수 없습니다.") from None
        return Connection(raw, "postgres")

    db_path = Path(resolved)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(db_path, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 30000")
    return Connection(raw, "sqlite")


def _sqlite_columns(connection: Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_sqlite(connection: Connection) -> None:
    columns = _sqlite_columns(connection, "attachments")
    for name, definition in (
        ("storage_backend", "TEXT NOT NULL DEFAULT ''"),
        ("storage_key", "TEXT"),
        ("processing_started_at", "TEXT"),
        ("processing_token", "TEXT"),
        ("rights_revision", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE attachments ADD COLUMN {name} {definition}")
    connection.execute(
        "UPDATE attachments SET storage_backend='local' "
        "WHERE storage_key IS NOT NULL AND storage_backend=''"
    )
    object_columns = _sqlite_columns(connection, "document_objects")
    for name, definition in (
        ("upload_attachment_id", "INTEGER"),
        ("upload_claim_token", "TEXT"),
    ):
        if name not in object_columns:
            connection.execute(f"ALTER TABLE document_objects ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS document_objects_owner_idx "
        "ON document_objects(upload_attachment_id, state)"
    )
    connection.execute(
        "DELETE FROM job_profiles WHERE attachment_id IS NULL AND extraction_status<>'metadata'"
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO document_objects(
          storage_backend, storage_key, sha256, state, ready_at
        )
        SELECT CASE WHEN storage_backend='' THEN 'local' ELSE storage_backend END,
               storage_key, COALESCE(sha256, ''), 'ready', collected_at
        FROM attachments WHERE storage_key IS NOT NULL
        """
    )


def _migrate_postgres(connection: Connection) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('jobandkill:document-state', 0))"
    )
    columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='attachments'
            """
        ).fetchall()
    }
    for name, definition in (
        ("storage_backend", "TEXT NOT NULL DEFAULT ''"),
        ("storage_key", "TEXT"),
        ("processing_started_at", "TIMESTAMPTZ"),
        ("processing_token", "TEXT"),
        ("rights_revision", "BIGINT NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE attachments ADD COLUMN IF NOT EXISTS {name} {definition}")
    connection.execute(
        "UPDATE attachments SET storage_backend='local' "
        "WHERE storage_key IS NOT NULL AND storage_backend=''"
    )
    object_columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='document_objects'
            """
        ).fetchall()
    }
    for name, definition in (
        ("upload_attachment_id", "BIGINT"),
        ("upload_claim_token", "TEXT"),
    ):
        if name not in object_columns:
            connection.execute(
                f"ALTER TABLE document_objects ADD COLUMN IF NOT EXISTS {name} {definition}"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS document_objects_owner_idx "
        "ON document_objects(upload_attachment_id, state)"
    )
    connection.execute(
        "DELETE FROM job_profiles WHERE attachment_id IS NULL AND extraction_status<>'metadata'"
    )
    connection.execute(
        """
        INSERT INTO document_objects(storage_backend, storage_key, sha256, state, ready_at)
        SELECT CASE WHEN storage_backend='' THEN 'local' ELSE storage_backend END,
               storage_key, COALESCE(sha256, ''), 'ready', collected_at
        FROM attachments WHERE storage_key IS NOT NULL
        ON CONFLICT(storage_backend, storage_key) DO NOTHING
        """
    )


def initialize(target: DatabaseTarget | None = None) -> DatabaseTarget:
    resolved = database_target(target)
    is_postgres = isinstance(resolved, str) and resolved.startswith(("postgresql://", "postgres://"))
    schema_name = "schema.postgres.sql" if is_postgres else "schema.sql"
    schema = (Path(__file__).parent / schema_name).read_text(encoding="utf-8")
    with connect(resolved) as connection:
        if connection.dialect == "postgres":
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('jobandkill:schema-migration', 0))"
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('jobandkill:document-state', 0))"
            )
        connection.executescript(schema)
        if connection.dialect == "sqlite":
            _migrate_sqlite(connection)
            connection.execute("PRAGMA user_version = 7")
        else:
            _migrate_postgres(connection)
    return resolved


def insert_id(connection: Connection, statement: str, params: Params = ()) -> int:
    if connection.dialect == "postgres":
        row = connection.execute(statement.rstrip().rstrip(";") + " RETURNING id", params).fetchone()
        return int(row["id"])
    cursor = connection.execute(statement, params)
    return int(cursor.lastrowid)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def public_stats(connection: Connection) -> dict[str, Any]:
    counts = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM institutions) AS institutions,
          (SELECT COUNT(*) FROM postings) AS postings,
          (SELECT COUNT(*) FROM job_profiles WHERE extraction_status != 'rejected') AS profiles,
          (SELECT COUNT(*) FROM attachments WHERE parser_status = 'parsed') AS parsed_documents,
          (SELECT COUNT(*) FROM document_gc_queue) AS pending_document_deletions,
          (SELECT COUNT(*) FROM document_gc_queue WHERE last_error<>'') AS failed_document_deletions,
          (SELECT COUNT(*) FROM attachments WHERE storage_backend='quarantine') AS quarantined_documents,
          (SELECT MAX(finished_at) FROM sync_runs WHERE status IN ('success', 'partial')) AS last_sync_at
        """
    ).fetchone()
    return dict(counts)


def list_sources(connection: Connection) -> list[dict[str, Any]]:
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


def _profile_payload(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "duties_json", "knowledge_json", "skills_json", "attitudes_json",
        "qualifications_json", "keywords_json", "ncs_categories_json",
        "positions_json", "regions_json",
    ):
        if key in result:
            result[key.removesuffix("_json")] = json_value(result.pop(key), [])
    return result


def search_profiles(
    connection: Connection,
    query: str = "",
    institution: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = " ".join(query.split())[:100]
    institution = " ".join(institution.split())[:100]
    limit = min(max(limit, 1), 50)
    offset = max(offset, 0)
    like = "ILIKE" if connection.dialect == "postgres" else "LIKE"
    cast = "::text" if connection.dialect == "postgres" else ""
    terms: list[str] = []
    params: list[Any] = []
    if institution:
        terms.append(f"jp.institution_name {like} ?")
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
        LEFT JOIN attachments a ON a.id = jp.attachment_id
    """
    visible = (
        "((jp.attachment_id IS NULL AND jp.extraction_status='metadata') "
        "OR a.rights_status IN ('open_document', 'authorized'))"
    )
    today = "CURRENT_DATE::text" if connection.dialect == "postgres" else "date('now')"
    order = f"""
        ORDER BY CASE WHEN p.application_end >= {today} THEN 0 ELSE 1 END,
          p.application_end DESC, jp.updated_at DESC
        LIMIT ? OFFSET ?
    """
    rows: list[Any] = []
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", query)[:8]
    if tokens and connection.dialect == "sqlite":
        fts_query = " OR ".join(f'"{token}"*' for token in tokens)
        rows = connection.execute(
            select + "JOIN job_profiles_fts ON job_profiles_fts.rowid = jp.id "
            + f"WHERE job_profiles_fts MATCH ? AND {where} AND {visible} "
            + "AND jp.extraction_status != 'rejected' " + order,
            [fts_query, *params, limit, offset],
        ).fetchall()
    elif tokens:
        searchable = " || ' ' || ".join(
            f"COALESCE(jp.{column}{cast}, '')"
            for column in ("job_title", "institution_name", "ncs_path", "summary", "duties_json", "skills_json")
        )
        rows = connection.execute(
            select + f"WHERE to_tsvector('simple', {searchable}) @@ plainto_tsquery('simple', ?) "
            + f"AND {where} AND {visible} AND jp.extraction_status != 'rejected' " + order,
            [query, *params, limit, offset],
        ).fetchall()
    if query and not rows:
        pattern = f"%{query}%"
        fallback = (
            f"(jp.job_title {like} ? OR jp.institution_name {like} ? OR jp.ncs_path {like} ? "
            f"OR jp.summary {like} ? OR jp.duties_json{cast} {like} ? OR jp.skills_json{cast} {like} ?)"
        )
        rows = connection.execute(
            select + f"WHERE {fallback} AND {where} AND {visible} "
            + "AND jp.extraction_status != 'rejected' " + order,
            [pattern] * 6 + params + [limit, offset],
        ).fetchall()
    elif not query:
        rows = connection.execute(
            select + f"WHERE {where} AND {visible} AND jp.extraction_status != 'rejected' " + order,
            params + [limit, offset],
        ).fetchall()
    return [_profile_payload(row) for row in rows]


def get_profile(connection: Connection, profile_id: int) -> dict[str, Any] | None:
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
          AND ((jp.attachment_id IS NULL AND jp.extraction_status='metadata')
               OR a.rights_status IN ('open_document', 'authorized'))
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
