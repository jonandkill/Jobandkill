from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "jobandkill.db"
DatabaseTarget = Path | str
Params = Sequence[Any] | Mapping[str, Any]
POSTGRES_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
AUTO_MIGRATE_VALUES = frozenset({"0", "1"})
PRODUCTION_DATABASE_USERS = {
    "web": "jobandkill_web",
    "collector": "jobandkill_collector",
    "cleanup": "jobandkill_cleanup",
}
PRODUCTION_ADMIN_DATABASE_ROLE = "admin"
PRODUCTION_ADMIN_DATABASE_USER = "jobandkill"
CURRENT_SCHEMA_VERSION = 10
SCHEMA_VERSION_KEY = "schema_version"


def database_path() -> Path:
    configured = os.getenv("JOBNKILL_DB_PATH")
    path = Path(configured) if configured else DEFAULT_DB_PATH
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _production_environment() -> bool:
    return os.getenv("JOBNKILL_ENV", "development").strip().lower() == "production"


def _validate_production_database_target(
    target: DatabaseTarget | None,
    *,
    expected_role: str | None = None,
    administrator: bool = False,
) -> str:
    """Validate a production DSN without returning or reporting its secrets."""
    if not isinstance(target, str) or not target.strip():
        raise RuntimeError("운영에는 PostgreSQL URL이 필요합니다.")
    database_url = target.strip()
    try:
        parsed = urlsplit(database_url)
        username = unquote(parsed.username or "")
    except ValueError:
        raise RuntimeError("운영 PostgreSQL URL이 올바르지 않습니다.") from None
    if (
        not database_url.startswith(("postgresql://", "postgres://"))
        or parsed.scheme not in {"postgresql", "postgres"}
        or not parsed.hostname
    ):
        raise RuntimeError("운영에는 PostgreSQL URL이 필요합니다.")

    configured_role = os.getenv("JOBNKILL_DATABASE_ROLE", "").strip()
    if administrator:
        if configured_role != PRODUCTION_ADMIN_DATABASE_ROLE:
            raise RuntimeError(
                "관리자 init은 JOBNKILL_DATABASE_ROLE=admin이 필요합니다."
            )
        if username != PRODUCTION_ADMIN_DATABASE_USER:
            raise RuntimeError("관리자 init은 PostgreSQL 소유자 URL이 필요합니다.")
        return database_url

    expected_username = PRODUCTION_DATABASE_USERS.get(configured_role)
    if expected_username is None:
        raise RuntimeError(
            "운영 런타임은 JOBNKILL_DATABASE_ROLE=web|collector|cleanup을 "
            "명시해야 합니다."
        )
    if expected_role is not None and configured_role != expected_role:
        raise RuntimeError(f"이 프로세스에는 {expected_role} 데이터베이스 역할이 필요합니다.")
    if username != expected_username:
        raise RuntimeError(
            f"{configured_role} 런타임은 {expected_username} PostgreSQL URL을 사용해야 합니다."
        )
    return database_url


def validate_production_database_target(
    target: DatabaseTarget | None, *, expected_role: str | None = None,
) -> str:
    """Validate that production runtime credentials match its declared role."""
    return _validate_production_database_target(target, expected_role=expected_role)


def _validate_production_admin_database_target(
    target: DatabaseTarget | None,
) -> str:
    return _validate_production_database_target(target, administrator=True)


def _resolve_database_target(
    target: DatabaseTarget | None = None, *, administrator: bool = False,
) -> DatabaseTarget:
    from_environment = target is None
    if target is None:
        if _production_environment():
            # Never inherit a provider's ambient owner DATABASE_URL in production.
            configured = os.getenv("JOBNKILL_DATABASE_URL", "").strip()
        else:
            configured = os.getenv(
                "JOBNKILL_DATABASE_URL", os.getenv("DATABASE_URL", "")
            ).strip()
        resolved: DatabaseTarget = configured or database_path()
    else:
        resolved = target

    if _production_environment():
        return (
            _validate_production_admin_database_target(resolved)
            if administrator
            else validate_production_database_target(resolved)
        )
    if (
        from_environment
        and isinstance(resolved, str)
        and not resolved.startswith(("postgresql://", "postgres://"))
    ):
        raise ValueError("DATABASE_URL은 PostgreSQL URL이어야 합니다.")
    return resolved


def database_target(target: DatabaseTarget | None = None) -> DatabaseTarget:
    """Resolve a normal runtime DB target without allowing admin credentials."""
    return _resolve_database_target(target)


def _admin_database_target(target: DatabaseTarget | None = None) -> DatabaseTarget:
    return _resolve_database_target(target, administrator=True)


def automatic_migrations_enabled() -> bool:
    """Return whether normal runtime startup may change the database schema.

    Development keeps the convenient historical auto-initialize behavior.  A
    production process is read/write runtime code, not a schema administrator,
    so an unset switch is deliberately treated as disabled there.  Operators
    can also disable automatic migrations explicitly in any environment.
    """
    configured = os.getenv("JOBNKILL_AUTO_MIGRATE", "").strip()
    production = os.getenv("JOBNKILL_ENV", "development").strip().lower() == "production"
    if configured and configured not in AUTO_MIGRATE_VALUES:
        raise RuntimeError("JOBNKILL_AUTO_MIGRATE는 0 또는 1이어야 합니다.")
    if production and configured == "1":
        raise RuntimeError(
            "운영 스키마 변경은 런타임 자동 마이그레이션이 아니라 "
            "관리자 `python -m jobandkill init`으로 실행해야 합니다."
        )
    if configured:
        return configured == "1"
    return not production


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


def _connect(
    target: DatabaseTarget | None = None, *, administrator: bool = False,
) -> Connection:
    resolved = (
        _admin_database_target(target) if administrator else database_target(target)
    )
    if isinstance(resolved, str) and resolved.startswith(("postgresql://", "postgres://")):
        sslmode = postgres_sslmode(resolved)
        if (
            os.getenv("JOBNKILL_ENV", "development").strip().lower() == "production"
            and sslmode not in POSTGRES_TLS_MODES
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


def connect(target: DatabaseTarget | None = None) -> Connection:
    """Connect with a runtime role; production owner credentials are rejected."""
    return _connect(target)


def _admin_connect(target: DatabaseTarget | None = None) -> Connection:
    return _connect(target, administrator=True)


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
    consent_columns = _sqlite_columns(connection, "user_consents")
    for name, definition in (
        ("notice_url", "TEXT NOT NULL DEFAULT ''"),
        ("notice_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("request_token_hash", "TEXT NOT NULL DEFAULT ''"),
        ("verified_at", "TEXT"),
    ):
        if name not in consent_columns:
            connection.execute(f"ALTER TABLE user_consents ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS user_consents_request_idx "
        "ON user_consents(request_token_hash)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS user_consents_pending_idx "
        "ON user_consents(verified_at, accepted_at)"
    )
    token_columns = _sqlite_columns(connection, "login_tokens")
    if "intent_hash" not in token_columns:
        connection.execute(
            "ALTER TABLE login_tokens ADD COLUMN intent_hash TEXT NOT NULL DEFAULT ''"
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
    consent_columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='user_consents'
            """
        ).fetchall()
    }
    for name, definition in (
        ("notice_url", "TEXT NOT NULL DEFAULT ''"),
        ("notice_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("request_token_hash", "TEXT NOT NULL DEFAULT ''"),
        ("verified_at", "TIMESTAMPTZ"),
    ):
        if name not in consent_columns:
            connection.execute(
                f"ALTER TABLE user_consents ADD COLUMN IF NOT EXISTS {name} {definition}"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS user_consents_request_idx "
        "ON user_consents(request_token_hash)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS user_consents_pending_idx "
        "ON user_consents(verified_at, accepted_at)"
    )
    token_columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='login_tokens'
            """
        ).fetchall()
    }
    if "intent_hash" not in token_columns:
        connection.execute(
            "ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS intent_hash TEXT NOT NULL DEFAULT ''"
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


def _schema_metadata_table(connection: Connection) -> str:
    return "public.app_metadata" if connection.dialect == "postgres" else "app_metadata"


def _record_current_schema(connection: Connection) -> None:
    table = _schema_metadata_table(connection)
    connection.execute(
        f"""
        INSERT INTO {table}(key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
          value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (SCHEMA_VERSION_KEY, str(CURRENT_SCHEMA_VERSION)),
    )


def _require_current_schema(connection: Connection) -> None:
    """Validate the owner-written marker using only a restricted SELECT."""
    table = _schema_metadata_table(connection)
    try:
        row = connection.execute(
            f"SELECT value FROM {table} WHERE key=?", (SCHEMA_VERSION_KEY,),
        ).fetchone()
    except Exception:
        raise RuntimeError(
            "데이터베이스 스키마가 초기화되지 않았습니다. 관리자 init을 먼저 실행하세요."
        ) from None
    if not row:
        raise RuntimeError(
            "데이터베이스 스키마 버전 정보가 없습니다. 관리자 init을 먼저 실행하세요."
        )
    if str(row["value"]) != str(CURRENT_SCHEMA_VERSION):
        raise RuntimeError(
            "데이터베이스 스키마 버전이 애플리케이션과 다릅니다. 관리자 init이 필요합니다."
        )


def initialize(
    target: DatabaseTarget | None = None, *, force_migrate: bool = False,
) -> DatabaseTarget:
    """Resolve and validate a database, applying schema only when authorized.

    Runtime callers use the environment-controlled policy.  The explicit
    ``force_migrate`` capability is reserved for the ``init`` administration
    command so production service roles never need table ownership or DDL.
    """
    resolved = (
        _admin_database_target(target) if force_migrate else database_target(target)
    )
    if not force_migrate and not automatic_migrations_enabled():
        # Runtime validation is a single read from owner-maintained metadata;
        # it requires neither information_schema access nor any DDL privilege.
        with connect(resolved) as connection:
            _require_current_schema(connection)
        return resolved
    is_postgres = isinstance(resolved, str) and resolved.startswith(("postgresql://", "postgres://"))
    schema_name = "schema.postgres.sql" if is_postgres else "schema.sql"
    schema = (Path(__file__).parent / schema_name).read_text(encoding="utf-8")
    connection_context = (
        _admin_connect(resolved) if force_migrate else connect(resolved)
    )
    with connection_context as connection:
        if connection.dialect == "postgres":
            connection.execute("SET LOCAL search_path TO public, pg_catalog")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('jobandkill:schema-migration', 0))"
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('jobandkill:document-state', 0))"
            )
        connection.executescript(schema)
        if connection.dialect == "sqlite":
            _migrate_sqlite(connection)
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        else:
            _migrate_postgres(connection)
        _record_current_schema(connection)
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
    offset = min(max(offset, 0), 1_000)
    if connection.dialect == "postgres":
        connection.execute("SET LOCAL statement_timeout = '2000ms'")
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
               jp.summary, jp.duties_json, jp.skills_json,
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
    results = [_profile_payload(row) for row in rows]
    for result in results:
        result["duties"] = result.get("duties", [])[:3]
        result["skills"] = result.get("skills", [])[:3]
        for key in ("ncs_categories", "positions", "regions"):
            result[key] = result.get(key, [])[:10]
    return results


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
