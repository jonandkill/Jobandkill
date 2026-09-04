from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from jobandkill.auth import public_url
from jobandkill.config import configuration_report
from jobandkill.db import connect
from jobandkill.server import JobAndKillHandler, serve


class RenderConfigurationTests(unittest.TestCase):
    def test_public_url_uses_render_external_url_in_production(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_PUBLIC_URL": "",
            "RENDER_EXTERNAL_URL": "https://jobandkill-career-writer.onrender.com/",
        }, clear=True):
            self.assertEqual(
                public_url(), "https://jobandkill-career-writer.onrender.com"
            )

    def test_private_render_postgres_does_not_require_public_tls(self) -> None:
        with patch.dict(os.environ, {
            "RENDER": "true",
            "JOBNKILL_DATABASE_URL": "postgresql://user:password@dpg-private/jobandkill",
        }, clear=True):
            report = configuration_report(production=True, collector_only=True)
        database = next(item for item in report["checks"] if item["name"] == "database")
        self.assertTrue(database["ready"])
        self.assertIn("비공개", database["message"])

    def test_external_render_postgres_still_requires_tls(self) -> None:
        with patch.dict(os.environ, {
            "RENDER": "true",
            "JOBNKILL_DATABASE_URL": (
                "postgresql://user:password@dpg.example.render.com/jobandkill"
            ),
        }, clear=True):
            report = configuration_report(production=True, collector_only=True)
        database = next(item for item in report["checks"] if item["name"] == "database")
        self.assertFalse(database["ready"])

    def test_web_startup_does_not_require_collector_storage_credentials(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "RENDER": "true",
            "JOBNKILL_DATABASE_URL": "postgresql://user:password@dpg-private/jobandkill",
            "RENDER_EXTERNAL_URL": "https://jobandkill-career-writer.onrender.com",
            "JOBNKILL_AUTH_RATE_SECRET": "generated-secret",
            "JOBNKILL_SMTP_HOST": "smtp.example.com",
            "JOBNKILL_SMTP_FROM": "login@example.com",
        }, clear=True):
            report = configuration_report(production=True, require_storage=False)
        self.assertNotIn("document_storage", {item["name"] for item in report["checks"]})

    def test_connect_accepts_private_render_postgres(self) -> None:
        database_url = "postgresql://user:password@dpg-private/jobandkill"
        postgres_connect = MagicMock()
        psycopg = types.ModuleType("psycopg")
        psycopg.connect = postgres_connect  # type: ignore[attr-defined]
        psycopg_rows = types.ModuleType("psycopg.rows")
        psycopg_rows.dict_row = object()  # type: ignore[attr-defined]
        with (
            patch.dict(os.environ, {
                "JOBNKILL_ENV": "production",
                "RENDER": "true",
            }, clear=True),
            patch.dict(sys.modules, {
                "psycopg": psycopg,
                "psycopg.rows": psycopg_rows,
            }),
        ):
            connection = connect(database_url)
        self.assertEqual(connection.dialect, "postgres")
        postgres_connect.assert_called_once()

    def test_connect_rejects_external_postgres_without_tls(self) -> None:
        database_url = "postgresql://user:password@dpg.example.render.com/jobandkill"
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "RENDER": "true",
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
