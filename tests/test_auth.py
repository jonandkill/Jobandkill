from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from jobandkill.db import connect
from jobandkill.server import JobAndKillServer


class CommitFailingConnection:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __getattr__(self, name: str):
        return getattr(self.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.connection.rollback()
        self.connection.close()
        if exc_type is None:
            raise RuntimeError("forced commit failure")


class AuthHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = {name: os.environ.get(name) for name in (
            "JOBNKILL_ENV", "JOBNKILL_AUTH_DEV_SHOW_LINK", "JOBNKILL_PUBLIC_URL",
            "JOBNKILL_AUTH_RATE_SECRET",
        )}
        os.environ["JOBNKILL_ENV"] = "test"
        os.environ["JOBNKILL_AUTH_DEV_SHOW_LINK"] = "1"
        os.environ["JOBNKILL_AUTH_RATE_SECRET"] = "test-rate-secret"
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "auth.db"
        self.server = JobAndKillServer(("127.0.0.1", 0), self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["JOBNKILL_PUBLIC_URL"] = self.base_url
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def request(self, path: str, payload: dict, csrf: str = "") -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "Origin": self.base_url}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with self.opener.open(request) as response:
            return response.status, json.load(response)

    def failing_commit(self):
        return patch(
            "jobandkill.server.connect",
            side_effect=lambda target: CommitFailingConnection(connect(target)),
        )

    def login(self) -> tuple[str, str]:
        status, sent = self.request("/api/auth/request", {"email": "Writer@Example.test"})
        self.assertEqual(status, 202)
        link = sent["development_magic_link"]
        token = parse_qs(urlsplit(link).fragment)["login_token"][0]
        status, verified = self.request("/api/auth/verify", {"token": token})
        self.assertEqual(status, 200)
        self.assertTrue(verified["authenticated"])
        csrf = next(cookie.value for cookie in self.jar if cookie.name == "jobandkill_csrf")
        return token, csrf

    def test_magic_token_is_hashed_single_use_and_cookie_is_http_only(self) -> None:
        token, _ = self.login()
        with connect(self.db_path) as connection:
            stored = connection.execute("SELECT token_hash FROM login_tokens").fetchone()["token_hash"]
            self.assertNotEqual(stored, token)
            self.assertEqual(len(stored), 64)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/auth/verify", {"token": token})
        self.assertEqual(raised.exception.code, 401)
        session_cookie = next(cookie for cookie in self.jar if cookie.name == "jobandkill_session")
        self.assertTrue(session_cookie.has_nonstandard_attr("HttpOnly"))

    def test_authenticated_draft_create_update_and_csrf(self) -> None:
        _, csrf = self.login()
        payload = {
            "document_type": "career", "style": "bullet", "target_length": 800,
            "experience_title": "민원 기준 정비", "actions": ["민원 자료를 분류"],
            "facts_confirmed": False,
        }
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/drafts", {
                "client_key": "browser_test_1", "payload": payload, "current_step": 4,
            })
        self.assertEqual(raised.exception.code, 403)

        status, created = self.request("/api/user/drafts", {
            "client_key": "browser_test_1", "payload": payload, "current_step": 4,
        }, csrf)
        self.assertEqual(status, 201)
        self.assertEqual(created["revision"], 1)
        self.assertFalse(created["payload"]["facts_confirmed"])

        update = urllib.request.Request(
            f"{self.base_url}/api/user/drafts/{created['id']}",
            data=json.dumps({
                "client_key": "browser_test_1",
                "payload": {**payload, "experience_title": "민원 분류 기준 정비"},
                "current_step": 5, "revision": 1,
            }).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base_url, "X-CSRF-Token": csrf},
            method="PUT",
        )
        with self.opener.open(update) as response:
            updated = json.load(response)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["current_step"], 5)

        wrong_client = urllib.request.Request(
            f"{self.base_url}/api/user/drafts/{created['id']}",
            data=json.dumps({
                "client_key": "another_device_1", "payload": payload,
                "current_step": 2, "revision": 2,
            }).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base_url, "X-CSRF-Token": csrf},
            method="PUT",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(wrong_client)
        self.assertEqual(raised.exception.code, 409)

        with self.opener.open(f"{self.base_url}/api/user/drafts") as response:
            listing = json.load(response)
        self.assertEqual(len(listing["items"]), 1)

    def test_boolean_step_is_rejected_as_validation_error(self) -> None:
        _, csrf = self.login()
        payload = {
            "document_type": "career", "style": "bullet", "target_length": 800,
            "experience_title": "검증", "actions": [], "facts_confirmed": False,
        }
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/drafts", {
                "client_key": "boolean_step_1", "payload": payload, "current_step": True,
            }, csrf)
        self.assertEqual(raised.exception.code, 422)

    def test_verify_does_not_set_cookies_when_commit_fails(self) -> None:
        _, sent = self.request("/api/auth/request", {"email": "Writer@Example.test"})
        token = parse_qs(urlsplit(sent["development_magic_link"]).fragment)["login_token"][0]

        with self.failing_commit(), self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/auth/verify", {"token": token})

        self.assertEqual(raised.exception.code, 500)
        self.assertIsNone(raised.exception.headers.get("Set-Cookie"))
        self.assertFalse(any(cookie.name == "jobandkill_session" for cookie in self.jar))
        with connect(self.db_path) as connection:
            login_token = connection.execute(
                "SELECT used_at FROM login_tokens WHERE token_hash IS NOT NULL"
            ).fetchone()
            session_count = connection.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"]
        self.assertIsNone(login_token["used_at"])
        self.assertEqual(session_count, 0)

    def test_draft_create_does_not_return_success_when_commit_fails(self) -> None:
        _, csrf = self.login()
        draft = {
            "document_type": "career", "style": "bullet", "target_length": 800,
            "experience_title": "민원 기준 정비", "actions": ["민원 자료를 분류"],
            "facts_confirmed": False,
        }

        with self.failing_commit(), self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/drafts", {
                "client_key": "commit_failure_1", "payload": draft, "current_step": 4,
            }, csrf)

        self.assertEqual(raised.exception.code, 500)
        with connect(self.db_path) as connection:
            draft_count = connection.execute("SELECT COUNT(*) AS count FROM user_drafts").fetchone()["count"]
        self.assertEqual(draft_count, 0)


if __name__ == "__main__":
    unittest.main()
