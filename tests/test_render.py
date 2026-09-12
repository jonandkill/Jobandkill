from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from jobandkill.auth import AuthError, public_url
from jobandkill.config import configuration_report
from jobandkill.db import connect
from jobandkill.server import JobAndKillHandler, serve


class RenderConfigurationTests(unittest.TestCase):
    @staticmethod
    def _blueprint_service(name: str) -> str:
        blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
        marker = f"    name: {name}\n"
        name_offset = blueprint.index(marker)
        start = blueprint.rfind("\n  - type:", 0, name_offset) + 1
        end = blueprint.find("\n  - type:", start)
        return blueprint[start:] if end == -1 else blueprint[start:end]

    def test_render_blueprint_uses_paid_singapore_resources(self) -> None:
        blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
        self.assertNotIn("databases:\n", blueprint)
        self.assertNotIn("plan: free", blueprint)
        web = self._blueprint_service("jobandkill-career-writer")
        self.assertIn("    plan: 1c-2g\n    region: singapore\n", web)
        self.assertIn('    autoDeployTrigger: "off"\n', web)
        self.assertIn("      - key: JOBNKILL_MAIL_TRANSPORT\n        value: resend\n", web)
        self.assertIn("      - key: JOBNKILL_RESEND_API_KEY\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_RESEND_FROM\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_PUBLIC_URL\n", web)
        self.assertEqual(web.count("      - key: JOBNKILL_PUBLIC_URL\n"), 1)
        self.assertIn("      - key: JOBNKILL_PRIVACY_POLICY_URL\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_PRIVACY_POLICY_VERSION\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_PRIVACY_POLICY_SHA256\n        sync: false\n", web)
        self.assertIn(
            '      - key: JOBNKILL_OVERSEAS_TRANSFER_REQUIRED\n        value: "1"\n', web
        )
        self.assertIn(
            "      - key: JOBNKILL_OVERSEAS_TRANSFER_POLICY_URL\n        sync: false\n", web
        )
        self.assertIn("      - key: JOBNKILL_OVERSEAS_TRANSFER_VERSION\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_OVERSEAS_TRANSFER_SHA256\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_DRAFT_RETENTION_DAYS\n        sync: false\n", web)
        self.assertIn("      - key: JOBNKILL_AUTO_MIGRATE\n        value: \"0\"\n", web)
        self.assertIn("scripts/run_with_database_role.sh gunicorn", web)
        self.assertIn("      - key: JOBNKILL_DATABASE_ROLE\n        value: web\n", web)
        self.assertIn("      - key: JOBNKILL_WEB_DATABASE_URL\n", web)
        self.assertIn("        sync: false\n", web)
        self.assertNotIn("fromDatabase:", web)
        self.assertNotIn("JOBNKILL_SMTP_", web)
        for forbidden in (
            "JOBNKILL_ALIO_API_URL_TEMPLATE",
            "JOBNKILL_ALIO_SERVICE_KEY",
            "JOBNKILL_STORAGE_BACKEND",
            "JOBNKILL_S3_",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ):
            self.assertNotIn(forbidden, web)

    def test_render_blueprint_daily_collector_is_isolated_from_web(self) -> None:
        cron = self._blueprint_service("jobandkill-daily-sync")
        self.assertIn("  - type: cron\n", cron)
        self.assertIn("    plan: 0.5c-512mb\n    region: singapore\n", cron)
        self.assertIn('    autoDeployTrigger: "off"\n', cron)
        self.assertIn('    schedule: "35 18 * * *"\n', cron)
        self.assertIn(
            "    startCommand: >-\n"
            "      scripts/run_with_database_role.sh sh -c\n"
            "      'python -m jobandkill doctor --production --collector-only --require-api\n"
            "      && python -m jobandkill sync --all\n"
            "      && python -m jobandkill process-documents --limit 20 --fail-on-error'\n",
            cron,
        )
        self.assertIn(
            "      - key: JOBNKILL_COLLECTOR_DATABASE_URL\n"
            "        # Private URL for jobandkill_collector only.",
            cron,
        )
        self.assertIn("      - key: JOBNKILL_DATABASE_ROLE\n        value: collector\n", cron)
        self.assertIn("      - key: JOBNKILL_AUTO_MIGRATE\n        value: \"0\"\n", cron)
        self.assertNotIn("fromDatabase:", cron)
        self.assertNotIn("cleanup-personal-data", cron)
        for key in (
            "JOBNKILL_ALIO_API_URL_TEMPLATE",
            "JOBNKILL_ALIO_SERVICE_KEY",
            "JOBNKILL_S3_BUCKET",
            "JOBNKILL_S3_REGION",
            "JOBNKILL_S3_PREFIX",
            "JOBNKILL_S3_ENDPOINT_URL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "JOBNKILL_S3_ENCRYPTION",
        ):
            self.assertIn(f"      - key: {key}\n", cron)
        for personal_key in (
            "JOBNKILL_DRAFT_RETENTION_DAYS",
            "JOBNKILL_PRIVACY_POLICY_",
            "JOBNKILL_OVERSEAS_TRANSFER_",
            "JOBNKILL_RESEND_",
        ):
            self.assertNotIn(personal_key, cron)

    def test_render_blueprint_privacy_cleanup_has_its_own_database_role(self) -> None:
        cleanup = self._blueprint_service("jobandkill-privacy-cleanup")
        self.assertIn("  - type: cron\n", cleanup)
        self.assertIn("    plan: 0.5c-512mb\n    region: singapore\n", cleanup)
        self.assertIn('    autoDeployTrigger: "off"\n', cleanup)
        self.assertIn('    schedule: "20 18 * * *"\n', cleanup)
        self.assertIn(
            "    startCommand: scripts/run_with_database_role.sh python -m jobandkill cleanup-personal-data --execute\n",
            cleanup,
        )
        self.assertIn("      - key: JOBNKILL_CLEANUP_DATABASE_URL\n", cleanup)
        self.assertIn("      - key: JOBNKILL_DATABASE_ROLE\n        value: cleanup\n", cleanup)
        self.assertIn("jobandkill_cleanup only", cleanup)
        self.assertIn("      - key: JOBNKILL_AUTO_MIGRATE\n        value: \"0\"\n", cleanup)
        self.assertIn("      - key: JOBNKILL_DRAFT_RETENTION_DAYS\n        sync: false\n", cleanup)
        self.assertNotIn("fromDatabase:", cleanup)
        for forbidden in (
            "JOBNKILL_ALIO_",
            "JOBNKILL_STORAGE_BACKEND",
            "JOBNKILL_S3_",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "JOBNKILL_PRIVACY_POLICY_",
            "JOBNKILL_RESEND_",
        ):
            self.assertNotIn(forbidden, cleanup)

    def test_no_runtime_service_receives_database_owner_connection(self) -> None:
        blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
        self.assertNotIn("fromDatabase:", blueprint)
        self.assertNotIn("      - key: JOBNKILL_DATABASE_URL\n", blueprint)
        for name in (
            "JOBNKILL_WEB_DATABASE_URL",
            "JOBNKILL_COLLECTOR_DATABASE_URL",
            "JOBNKILL_CLEANUP_DATABASE_URL",
        ):
            self.assertEqual(blueprint.count(f"      - key: {name}\n"), 1)
        self.assertEqual(blueprint.count('      - key: JOBNKILL_AUTO_MIGRATE\n        value: "0"\n'), 3)

    def test_role_wrapper_overwrites_legacy_owner_url_and_removes_other_urls(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_with_database_role.sh"
        environment = {
            "PATH": os.environ["PATH"],
            "JOBNKILL_DATABASE_ROLE": "collector",
            "JOBNKILL_DATABASE_URL": "postgresql://owner:old@owner/database",
            "DATABASE_URL": "postgresql://owner:fallback@owner/database",
            "JOBNKILL_WEB_DATABASE_URL": "postgresql://jobandkill_web:web@private/database",
            "JOBNKILL_COLLECTOR_DATABASE_URL": "postgresql://jobandkill_collector:collector@private/database?sslmode=require",
            "JOBNKILL_CLEANUP_DATABASE_URL": "postgresql://jobandkill_cleanup:cleanup@private/database",
            "JOBNKILL_ADMIN_DATABASE_URL": "postgresql://owner:admin@owner/database",
        }
        check = (
            'test "$JOBNKILL_DATABASE_URL" = '
            '"postgresql://jobandkill_collector:collector@private/database?sslmode=require" '
            '&& test -z "${DATABASE_URL+x}" '
            '&& test -z "${JOBNKILL_WEB_DATABASE_URL+x}" '
            '&& test -z "${JOBNKILL_COLLECTOR_DATABASE_URL+x}" '
            '&& test -z "${JOBNKILL_CLEANUP_DATABASE_URL+x}" '
            '&& test -z "${JOBNKILL_ADMIN_DATABASE_URL+x}"'
        )
        result = subprocess.run(
            [str(script), "sh", "-c", check], env=environment,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_requires_explicit_public_url(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_PUBLIC_URL": "",
            "RENDER_EXTERNAL_URL": "https://jobandkill-career-writer.onrender.com/",
        }, clear=True):
            with self.assertRaisesRegex(AuthError, "JOBNKILL_PUBLIC_URL"):
                public_url()

    def test_explicit_public_url_is_normalized_in_production(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_PUBLIC_URL": "https://jobandkill-career-writer.onrender.com/",
        }, clear=True):
            self.assertEqual(public_url(), "https://jobandkill-career-writer.onrender.com")

    def test_private_render_postgres_requires_and_accepts_tls(self) -> None:
        with patch.dict(os.environ, {
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "collector",
            "JOBNKILL_DATABASE_URL": (
                "postgresql://jobandkill_collector:password@dpg-private/jobandkill?sslmode=require"
            ),
        }, clear=True):
            report = configuration_report(production=True, collector_only=True)
        database = next(item for item in report["checks"] if item["name"] == "database")
        self.assertTrue(database["ready"])
        self.assertIn("비공개", database["message"])

        with patch.dict(os.environ, {
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "collector",
            "JOBNKILL_DATABASE_URL": "postgresql://jobandkill_collector:password@dpg-private/jobandkill",
        }, clear=True):
            report = configuration_report(production=True, collector_only=True)
        database = next(item for item in report["checks"] if item["name"] == "database")
        self.assertFalse(database["ready"])

    def test_collector_startup_does_not_require_personal_data_retention(self) -> None:
        environment = {
            "JOBNKILL_ENV": "production",
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "collector",
            "JOBNKILL_DATABASE_URL": "postgresql://jobandkill_collector:password@dpg-private/jobandkill?sslmode=require",
        }
        with patch.dict(os.environ, environment, clear=True):
            report = configuration_report(production=True, collector_only=True)
        self.assertNotIn("draft_retention", {item["name"] for item in report["checks"]})

    def test_external_render_postgres_still_requires_tls(self) -> None:
        with patch.dict(os.environ, {
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "collector",
            "JOBNKILL_DATABASE_URL": (
                "postgresql://jobandkill_collector:password@dpg.example.render.com/jobandkill"
            ),
        }, clear=True):
            report = configuration_report(production=True, collector_only=True)
        database = next(item for item in report["checks"] if item["name"] == "database")
        self.assertFalse(database["ready"])

    def test_web_startup_does_not_require_collector_storage_credentials(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "web",
            "JOBNKILL_DATABASE_URL": "postgresql://jobandkill_web:password@dpg-private/jobandkill?sslmode=require",
            "JOBNKILL_PUBLIC_URL": "https://jobandkill-career-writer.onrender.com",
            "JOBNKILL_AUTH_RATE_SECRET": "generated-secret",
            "JOBNKILL_SMTP_HOST": "smtp.example.com",
            "JOBNKILL_SMTP_FROM": "login@example.com",
        }, clear=True):
            report = configuration_report(production=True, require_storage=False)
        self.assertNotIn("document_storage", {item["name"] for item in report["checks"]})

    def test_connect_accepts_private_render_postgres_with_tls(self) -> None:
        database_url = "postgresql://jobandkill_collector:password@dpg-private/jobandkill?sslmode=require"
        postgres_connect = MagicMock()
        psycopg = types.ModuleType("psycopg")
        psycopg.connect = postgres_connect  # type: ignore[attr-defined]
        psycopg_rows = types.ModuleType("psycopg.rows")
        psycopg_rows.dict_row = object()  # type: ignore[attr-defined]
        with (
            patch.dict(os.environ, {
                "JOBNKILL_ENV": "production",
                "RENDER": "true",
                "JOBNKILL_DATABASE_ROLE": "collector",
            }, clear=True),
            patch.dict(sys.modules, {
                "psycopg": psycopg,
                "psycopg.rows": psycopg_rows,
            }),
        ):
            connection = connect(database_url)
        self.assertEqual(connection.dialect, "postgres")
        postgres_connect.assert_called_once()

    def test_connect_rejects_private_render_postgres_without_tls(self) -> None:
        database_url = "postgresql://jobandkill_collector:password@dpg-private/jobandkill"
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "collector",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "sslmode=require"):
                connect(database_url)

    def test_connect_rejects_external_postgres_without_tls(self) -> None:
        database_url = "postgresql://jobandkill_collector:password@dpg.example.render.com/jobandkill"
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "RENDER": "true",
            "JOBNKILL_DATABASE_ROLE": "collector",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "sslmode=require"):
                connect(database_url)

    def test_render_private_proxy_uses_forwarded_client_ip(self) -> None:
        handler = object.__new__(JobAndKillHandler)
        handler.client_address = ("10.0.0.8", 12345)
        handler.headers = {"X-Forwarded-For": "8.8.8.8"}
        with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
            self.assertEqual(handler._request_ip(), "8.8.8.8")

    @patch("jobandkill.server.JobAndKillServer")
    def test_server_prefers_render_port(self, server_class: MagicMock) -> None:
        instance = server_class.return_value
        with patch.dict(os.environ, {
            "PORT": "10000",
            "JOBNKILL_PORT": "8787",
            "JOBNKILL_HOST": "0.0.0.0",
        }, clear=True):
            serve()
        server_class.assert_called_once_with(("0.0.0.0", 10000), None)
        instance.serve_forever.assert_called_once_with()
        instance.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
