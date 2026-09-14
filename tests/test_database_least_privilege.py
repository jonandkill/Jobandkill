from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobandkill.__main__ import main
from jobandkill.config import configuration_report
from jobandkill.db import (
    CURRENT_SCHEMA_VERSION,
    Connection,
    automatic_migrations_enabled,
    connect,
    database_target,
    initialize,
    validate_production_database_target,
)


ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL_PATH = ROOT / "scripts" / "provision_database_roles.sql"

CORPUS_TABLES = {
    "sources", "sync_runs", "institutions", "postings", "attachments",
    "document_objects", "document_gc_queue", "storage_namespaces", "job_profiles",
    "extraction_evidence", "rights_decisions",
    "occupation_catalog", "catalog_sync_runs", "catalog_sync_pages",
}
PERSONAL_TABLES = {
    "users", "user_consents", "login_tokens", "sessions", "user_drafts",
    "auth_request_events",
}
CLEANUP_TABLES = {
    "users", "user_consents", "login_tokens", "sessions", "user_drafts",
    "auth_request_events",
}
CLEANUP_SELECT_COLUMNS = {
    "users": {"id", "email_verified_at", "created_at"},
    "user_consents": {"verified_at", "accepted_at"},
    "login_tokens": {"user_id", "expires_at", "used_at"},
    "sessions": {"expires_at", "idle_expires_at", "revoked_at"},
    "user_drafts": {"user_id", "updated_at"},
    "auth_request_events": {"created_at"},
}


def table_grants(sql: str, role: str) -> list[tuple[set[str], set[str]]]:
    matches = re.finditer(
        rf"GRANT\s+([A-Z, ]+)\s+ON TABLE\s+([^;]+?)\s+TO\s+{re.escape(role)}\s*;",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    grants: list[tuple[set[str], set[str]]] = []
    for match in matches:
        privileges = {item.strip().upper() for item in match.group(1).split(",")}
        tables = {
            item.strip().removeprefix("public.")
            for item in match.group(2).split(",")
        }
        grants.append((privileges, tables))
    return grants


def granted_tables(sql: str, role: str) -> set[str]:
    return set().union(*(tables for _, tables in table_grants(sql, role)))


class MigrationPolicyTests(unittest.TestCase):
    def test_production_runtime_does_not_initialize_a_blank_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"JOBNKILL_ENV": "production"}, clear=True,
        ):
            database = Path(directory) / "runtime.db"
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL URL"):
                initialize(database)
            self.assertFalse(database.exists())

    def test_explicit_zero_validates_an_owner_initialized_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.db"
            initialize(database, force_migrate=True)
            with patch.dict(
                os.environ,
                {"JOBNKILL_ENV": "test", "JOBNKILL_AUTO_MIGRATE": "0"},
                clear=True,
            ):
                self.assertEqual(initialize(database), database)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM app_metadata WHERE key='schema_version'"
                ).fetchone()[0]
        self.assertEqual(version, str(CURRENT_SCHEMA_VERSION))

    def test_explicit_zero_rejects_an_outdated_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"JOBNKILL_ENV": "test", "JOBNKILL_AUTO_MIGRATE": "0"},
            clear=True,
        ):
            database = Path(directory) / "runtime.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO app_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(CURRENT_SCHEMA_VERSION - 1),),
                )
            with self.assertRaisesRegex(RuntimeError, "버전"):
                initialize(database)

    def test_force_migrate_overrides_disabled_runtime_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"JOBNKILL_ENV": "test", "JOBNKILL_AUTO_MIGRATE": "0"},
            clear=True,
        ):
            database = Path(directory) / "admin.db"
            initialize(database, force_migrate=True)
            with sqlite3.connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                marker = connection.execute(
                    "SELECT value FROM app_metadata WHERE key='schema_version'"
                ).fetchone()[0]
        self.assertEqual(count, 5)
        self.assertEqual(marker, str(CURRENT_SCHEMA_VERSION))

    def test_production_admin_init_never_falls_back_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "JOBNKILL_ENV": "production",
                "JOBNKILL_DATABASE_ROLE": "admin",
            },
            clear=True,
        ):
            database = Path(directory) / "admin.db"
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL URL"):
                initialize(database, force_migrate=True)
            self.assertFalse(database.exists())

    def test_production_runtime_cannot_enable_automatic_ddl(self) -> None:
        with patch.dict(
            os.environ,
            {"JOBNKILL_ENV": "production", "JOBNKILL_AUTO_MIGRATE": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "관리자"):
                automatic_migrations_enabled()

    def test_init_command_is_the_forced_admin_path(self) -> None:
        with (
            patch("jobandkill.__main__.initialize", return_value=Path("admin.db")) as mocked,
            patch("jobandkill.__main__._print"),
        ):
            self.assertEqual(main(["init"]), 0)
        mocked.assert_called_once_with(None, force_migrate=True)

    def test_switch_accepts_only_zero_or_one(self) -> None:
        with patch.dict(os.environ, {"JOBNKILL_AUTO_MIGRATE": "false"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "0 또는 1"):
                automatic_migrations_enabled()

    def test_production_report_requires_explicit_zero(self) -> None:
        for value, expected in ((None, False), ("1", False), ("0", True)):
            environment = {"JOBNKILL_ENV": "production"}
            if value is not None:
                environment["JOBNKILL_AUTO_MIGRATE"] = value
            with self.subTest(value=value), patch.dict(os.environ, environment, clear=True):
                report = configuration_report(
                    production=True, collector_only=True, require_storage=False,
                )
            check = next(
                item for item in report["checks"]
                if item["name"] == "automatic_migrations"
            )
            self.assertEqual(check["ready"], expected)


class ProductionDatabaseBoundaryTests(unittest.TestCase):
    RUNTIME_URLS = {
        "web": "postgresql://jobandkill_web:secret@private/jobandkill?sslmode=require",
        "collector": "postgresql://jobandkill_collector:secret@private/jobandkill?sslmode=require",
        "cleanup": "postgresql://jobandkill_cleanup:secret@private/jobandkill?sslmode=require",
    }
    OWNER_URL = "postgresql://jobandkill:secret@private/jobandkill?sslmode=require"

    def test_each_runtime_role_requires_its_matching_username(self) -> None:
        for role, database_url in self.RUNTIME_URLS.items():
            with self.subTest(role=role), patch.dict(
                os.environ, {"JOBNKILL_DATABASE_ROLE": role}, clear=True,
            ):
                self.assertEqual(
                    validate_production_database_target(database_url), database_url,
                )
                other_url = self.RUNTIME_URLS[
                    "collector" if role != "collector" else "web"
                ]
                with self.assertRaisesRegex(RuntimeError, f"jobandkill_{role}"):
                    validate_production_database_target(other_url)

    def test_runtime_rejects_missing_invalid_admin_and_owner_credentials(self) -> None:
        cases = (
            ({}, self.RUNTIME_URLS["web"], "web\\|collector\\|cleanup"),
            ({"JOBNKILL_DATABASE_ROLE": "WEB"}, self.RUNTIME_URLS["web"], "web\\|collector\\|cleanup"),
            ({"JOBNKILL_DATABASE_ROLE": "admin"}, self.OWNER_URL, "web\\|collector\\|cleanup"),
            ({"JOBNKILL_DATABASE_ROLE": "web"}, self.OWNER_URL, "jobandkill_web"),
            (
                {"JOBNKILL_DATABASE_ROLE": "web"},
                self.RUNTIME_URLS["web"].replace("postgresql://", "POSTGRESQL://"),
                "PostgreSQL URL",
            ),
        )
        for environment, database_url, message in cases:
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    validate_production_database_target(database_url)

    def test_production_ignores_ambient_owner_database_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "JOBNKILL_ENV": "production",
                "JOBNKILL_DATABASE_ROLE": "web",
                "DATABASE_URL": self.OWNER_URL,
                "JOBNKILL_DB_PATH": str(Path(directory) / "fallback.db"),
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL URL"):
                database_target()
            self.assertFalse((Path(directory) / "fallback.db").exists())

    def test_normal_connect_never_accepts_admin_credentials(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_DATABASE_ROLE": "admin",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "web\\|collector\\|cleanup"):
                connect(self.OWNER_URL)

    def test_admin_init_requires_declared_admin_and_owner_username(self) -> None:
        for role, database_url, message in (
            ("web", self.OWNER_URL, "ROLE=admin"),
            ("admin", self.RUNTIME_URLS["web"], "소유자 URL"),
        ):
            with self.subTest(role=role), patch.dict(os.environ, {
                "JOBNKILL_ENV": "production",
                "JOBNKILL_DATABASE_ROLE": role,
            }, clear=True):
                with self.assertRaisesRegex(RuntimeError, message):
                    initialize(database_url, force_migrate=True)

    def test_admin_init_requires_postgres_tls(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_DATABASE_ROLE": "admin",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "sslmode=require"):
                initialize(self.OWNER_URL.split("?", 1)[0], force_migrate=True)

    def test_admin_init_uses_only_the_separate_admin_connection_path(self) -> None:
        raw = MagicMock()
        raw.execute.return_value.fetchall.return_value = []
        with (
            patch.dict(os.environ, {
                "JOBNKILL_ENV": "production",
                "JOBNKILL_DATABASE_ROLE": "admin",
            }, clear=True),
            patch(
                "jobandkill.db._admin_connect",
                return_value=Connection(raw, "postgres"),
            ) as admin_connect,
            patch("jobandkill.db.connect") as runtime_connect,
        ):
            self.assertEqual(
                initialize(self.OWNER_URL, force_migrate=True), self.OWNER_URL,
            )
        admin_connect.assert_called_once_with(self.OWNER_URL)
        runtime_connect.assert_not_called()

    def test_configuration_report_rejects_web_collector_role_swaps(self) -> None:
        cases = (
            ("collector", self.RUNTIME_URLS["collector"], False, "web"),
            ("web", self.RUNTIME_URLS["web"], True, "collector"),
        )
        for role, database_url, collector_only, expected in cases:
            with self.subTest(role=role, collector_only=collector_only), patch.dict(
                os.environ,
                {
                    "JOBNKILL_DATABASE_ROLE": role,
                    "JOBNKILL_DATABASE_URL": database_url,
                },
                clear=True,
            ):
                report = configuration_report(
                    production=True,
                    collector_only=collector_only,
                    require_storage=False,
                )
            database = next(
                item for item in report["checks"] if item["name"] == "database"
            )
            self.assertFalse(database["ready"])
            self.assertIn(expected, database["message"])

    def test_configuration_report_handles_a_malformed_url_as_not_ready(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_DATABASE_ROLE": "web",
            "JOBNKILL_DATABASE_URL": "postgresql://jobandkill_web@[broken/jobandkill",
        }, clear=True):
            report = configuration_report(production=True, require_storage=False)
        database = next(
            item for item in report["checks"] if item["name"] == "database"
        )
        self.assertFalse(database["ready"])


class ProvisioningSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = ROLE_SQL_PATH.read_text(encoding="utf-8")

    def test_provisioning_sql_never_receives_or_sets_real_passwords(self) -> None:
        self.assertNotRegex(self.sql, r"(?i)PASSWORD\s+(?:'|:)")
        self.assertIn("\\set ECHO none", self.sql)
        for environment_name in (
            "JOBNKILL_WEB_DB_PASSWORD",
            "JOBNKILL_COLLECTOR_DB_PASSWORD",
            "JOBNKILL_CLEANUP_DB_PASSWORD",
        ):
            self.assertNotIn(environment_name, self.sql)
        operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
        for role in ("jobandkill_web", "jobandkill_collector", "jobandkill_cleanup"):
            self.assertIn(f"\\password {role}", operations)

    def test_service_roles_have_no_elevated_attributes_or_ddl_grants(self) -> None:
        normalized = " ".join(self.sql.split())
        for role in ("jobandkill_web", "jobandkill_collector", "jobandkill_cleanup"):
            role_pattern = (
                rf"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                rf"NOINHERIT NOREPLICATION NOBYPASSRLS;"
            )
            self.assertRegex(normalized, role_pattern)
            self.assertNotRegex(self.sql, rf"(?is)ALTER\s+(?:TABLE|SEQUENCE|SCHEMA).*OWNER TO\s+{role}")
        self.assertNotRegex(self.sql, r"(?i)GRANT\s+(?:ALL|CREATE|TEMPORARY)")
        self.assertIn("REVOKE CREATE ON SCHEMA public FROM PUBLIC", self.sql)
        self.assertIn("has_schema_privilege(service_role, namespace.oid, 'CREATE')", self.sql)
        self.assertIn("FROM pg_shdepend ownership", self.sql)
        self.assertIn("ownership.deptype='o'", self.sql)

    def test_web_reads_corpus_and_cruds_only_personal_tables(self) -> None:
        grants = table_grants(self.sql, "jobandkill_web")
        self.assertIn(({"SELECT"}, CORPUS_TABLES), grants)
        self.assertIn(({"SELECT", "INSERT", "UPDATE", "DELETE"}, PERSONAL_TABLES), grants)
        self.assertEqual(granted_tables(self.sql, "jobandkill_web"), CORPUS_TABLES | PERSONAL_TABLES)

    def test_schema_version_marker_is_read_only_for_every_runtime_role(self) -> None:
        for role in ("jobandkill_web", "jobandkill_collector", "jobandkill_cleanup"):
            self.assertRegex(
                self.sql,
                rf"(?is)GRANT\s+SELECT\s+ON TABLE\s+public\.app_metadata\s+TO"
                rf"\s+jobandkill_web,\s+jobandkill_collector,\s+jobandkill_cleanup\s*;",
            )
            self.assertNotRegex(
                self.sql,
                rf"(?is)GRANT\s+(?:INSERT|UPDATE|DELETE)[^;]+app_metadata[^;]+TO\s+{role}",
            )

    def test_collector_cruds_corpus_and_has_no_personal_grant(self) -> None:
        grants = table_grants(self.sql, "jobandkill_collector")
        self.assertEqual(grants, [({"SELECT", "INSERT", "UPDATE", "DELETE"}, CORPUS_TABLES)])
        self.assertTrue(granted_tables(self.sql, "jobandkill_collector").isdisjoint(PERSONAL_TABLES))

    def test_cleanup_has_only_minimum_select_delete_tables(self) -> None:
        grants = table_grants(self.sql, "jobandkill_cleanup")
        self.assertEqual(grants, [({"DELETE"}, CLEANUP_TABLES)])
        column_grants = {
            table: {column.strip() for column in columns.split(",")}
            for columns, table in re.findall(
                r"(?is)GRANT\s+SELECT\s*\(([^)]+)\)\s+ON TABLE\s+public\.([a-z_]+)"
                r"\s+TO\s+jobandkill_cleanup\s*;",
                self.sql,
            )
        }
        self.assertEqual(column_grants, CLEANUP_SELECT_COLUMNS)
        self.assertNotIn("payload_json", self.sql[self.sql.index("-- Retention cleanup:"):])
        self.assertNotIn("email,", self.sql[self.sql.index("-- Retention cleanup:"):])
        self.assertNotRegex(
            self.sql,
            r"(?is)GRANT\s+USAGE\s+ON SEQUENCE[^;]+TO\s+jobandkill_cleanup",
        )


if __name__ == "__main__":
    unittest.main()
