from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
            self.assertEqual(data["status"], "ok")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

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


if __name__ == "__main__":
    unittest.main()
