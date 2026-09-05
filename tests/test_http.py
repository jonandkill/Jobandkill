from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from jobandkill.server import JobAndKillServer


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.server = JobAndKillServer(("127.0.0.1", 0), Path(self.directory.name) / "http.db")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def test_health_and_security_headers(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/api/health") as response:
            data = json.load(response)
            self.assertEqual(data, {"status": "ok"})
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        with urllib.request.urlopen(f"{self.base_url}/api/stats") as response:
            data = json.load(response)
            self.assertEqual(set(data), {"stats"})
            self.assertTrue(
                {"institutions", "postings", "profiles", "last_sync_at"}
                <= set(data["stats"])
            )

    def test_production_adds_hsts(self) -> None:
        # This test exercises the production header branch against its
        # already-created test SQLite fixture, not a production DB connection.
        with patch.dict(os.environ, {"JOBNKILL_ENV": "production"}), patch(
            "jobandkill.db._production_environment", return_value=False,
        ):
            with urllib.request.urlopen(f"{self.base_url}/api/health") as response:
                self.assertEqual(
                    response.headers["Strict-Transport-Security"],
                    "max-age=31536000; includeSubDomains",
                )

    def test_search_rejects_unbounded_offset(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base_url}/api/jobs?offset=1001")
        self.assertEqual(raised.exception.code, 400)

    def test_compose_endpoint(self) -> None:
        payload = {
            "document_type": "career",
            "style": "bullet",
            "facts_confirmed": True,
            "experience_title": "업무 개선",
            "actions": ["자료를 분류"],
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/drafts/compose",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            data = json.load(response)
            self.assertIn("자료를 분류", data["output"])
            self.assertGreater(len(data["missing_fields"]), 0)

    def test_rejects_unconfirmed_draft(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/drafts/compose",
            data=json.dumps({"document_type": "career", "style": "bullet"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 422)

    def test_production_rejects_compose_without_parsing_or_composing_personal_data(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/drafts/compose",
            data=b'{"private-experience-secret": "not sent by the browser"}',
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with (
            patch.dict(os.environ, {"JOBNKILL_ENV": "production"}),
            patch("jobandkill.server.JobAndKillHandler._read_json") as read_json,
            patch("jobandkill.server.compose") as compose,
            self.assertRaises(urllib.error.HTTPError) as raised,
        ):
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(json.load(raised.exception)["error"]["code"], "browser_compose_only")
        read_json.assert_not_called()
        compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
