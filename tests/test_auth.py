from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from jobandkill import auth
from jobandkill.config import configuration_report
from jobandkill.db import connect, initialize
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
            "JOBNKILL_AUTH_RATE_SECRET", "JOBNKILL_MAIL_TRANSPORT",
            "JOBNKILL_RESEND_API_KEY", "JOBNKILL_RESEND_FROM",
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

    def request(
        self, path: str, payload: dict, csrf: str = "", opener=None,
    ) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "Origin": self.base_url}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with (opener or self.opener).open(request) as response:
            return response.status, json.load(response)

    def failing_commit(self):
        return patch(
            "jobandkill.server.connect",
            side_effect=lambda target: CommitFailingConnection(connect(target)),
        )

    def login(self, email: str = "Writer@Example.test", opener=None, jar=None) -> tuple[str, str]:
        active_opener = opener if opener is not None else self.opener
        active_jar = jar if jar is not None else self.jar
        status, sent = self.request("/api/auth/request", {"email": email}, opener=active_opener)
        self.assertEqual(status, 202)
        link = sent["development_magic_link"]
        token = parse_qs(urlsplit(link).fragment)["login_token"][0]
        status, verified = self.request("/api/auth/verify", {"token": token}, opener=active_opener)
        self.assertEqual(status, 200)
        self.assertTrue(verified["authenticated"])
        csrf = next(cookie.value for cookie in active_jar if cookie.name == "jobandkill_csrf")
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

    def test_magic_link_is_bound_to_the_requesting_browser(self) -> None:
        status, sent = self.request("/api/auth/request", {"email": "intent@example.test"})
        self.assertEqual(status, 202)
        token = parse_qs(urlsplit(sent["development_magic_link"]).fragment)["login_token"][0]
        other_jar = http.cookiejar.CookieJar()
        other_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(other_jar))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/auth/verify", {"token": token}, opener=other_opener)
        self.assertEqual(raised.exception.code, 401)
        self.assertEqual(json.load(raised.exception)["error"]["code"], "login_intent_required")

        status, verified = self.request("/api/auth/verify", {"token": token})
        self.assertEqual(status, 200)
        self.assertTrue(verified["authenticated"])

    def test_unsolicited_request_cannot_invalidate_an_owners_valid_link(self) -> None:
        status, owner_sent = self.request(
            "/api/auth/request", {"email": "targeted@example.test"}
        )
        self.assertEqual(status, 202)
        owner_token = parse_qs(
            urlsplit(owner_sent["development_magic_link"]).fragment
        )["login_token"][0]

        attacker_jar = http.cookiejar.CookieJar()
        attacker_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(attacker_jar)
        )
        status, attacker_sent = self.request(
            "/api/auth/request",
            {"email": "targeted@example.test"},
            opener=attacker_opener,
        )
        self.assertEqual(status, 202)
        attacker_token = parse_qs(
            urlsplit(attacker_sent["development_magic_link"]).fragment
        )["login_token"][0]

        # The account owner's earlier link remains usable despite the later,
        # unauthenticated request made from a different browser.
        status, verified = self.request("/api/auth/verify", {"token": owner_token})
        self.assertEqual(status, 200)
        self.assertTrue(verified["authenticated"])

        # Successful verification atomically retires every sibling link, while
        # the consumed link itself remains one-time use.
        with self.assertRaises(urllib.error.HTTPError) as sibling:
            self.request(
                "/api/auth/verify",
                {"token": attacker_token},
                opener=attacker_opener,
            )
        self.assertEqual(sibling.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as replay:
            self.request("/api/auth/verify", {"token": owner_token})
        self.assertEqual(replay.exception.code, 401)

        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT used_at FROM login_tokens WHERE user_id=(SELECT id FROM users WHERE email=?)",
                ("targeted@example.test",),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["used_at"] is not None for row in rows))

    def test_production_login_requires_versioned_consent_before_persistence_and_audits_it(self) -> None:
        production = {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_PUBLIC_URL": "https://public.example.test",
            "JOBNKILL_PRIVACY_POLICY_URL": "https://public.example.test/privacy",
            "JOBNKILL_PRIVACY_POLICY_VERSION": "2026-09-01",
            "JOBNKILL_PRIVACY_POLICY_SHA256": "a" * 64,
            "JOBNKILL_OVERSEAS_TRANSFER_REQUIRED": "1",
            "JOBNKILL_OVERSEAS_TRANSFER_POLICY_URL": "https://public.example.test/overseas-transfer",
            "JOBNKILL_OVERSEAS_TRANSFER_VERSION": "2026-09-01",
            "JOBNKILL_OVERSEAS_TRANSFER_SHA256": "b" * 64,
        }
        with patch.dict(os.environ, production), patch(
            "jobandkill.server.origin_is_allowed", return_value=True,
        ), patch(
            "jobandkill.db._production_environment", return_value=False,
        ), patch("jobandkill.auth._send_magic_link") as send_mail:
            with self.assertRaises(urllib.error.HTTPError) as missing:
                self.request("/api/auth/request", {"email": "consent@example.test"})
            self.assertEqual(missing.exception.code, 422)
            with connect(self.db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"], 0)

            status, response = self.request("/api/auth/request", {
                "email": "consent@example.test",
                "privacy_policy_accepted": True,
                "privacy_policy_version": "2026-09-01",
                "overseas_transfer_accepted": True,
                "overseas_transfer_version": "2026-09-01",
            })
            self.assertEqual(status, 202)
            self.assertTrue(response["accepted"])
            with connect(self.db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT consent_type, policy_version, notice_url, notice_sha256,
                           request_token_hash, verified_at, metadata_json
                    FROM user_consents ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(
                [(row["consent_type"], row["policy_version"]) for row in rows],
                [("privacy_policy", "2026-09-01"), ("overseas_transfer", "2026-09-01")],
            )
            self.assertTrue(all("ip" not in row["metadata_json"].lower() for row in rows))
            self.assertTrue(all(row["verified_at"] is None for row in rows))
            self.assertEqual({row["notice_sha256"] for row in rows}, {"a" * 64, "b" * 64})
            self.assertEqual(len({row["request_token_hash"] for row in rows}), 1)

            link = send_mail.call_args.args[1]
            token = parse_qs(urlsplit(link).fragment)["login_token"][0]
            intent = next(
                cookie.value
                for cookie in self.jar
                if cookie.name == "__Host-jobandkill_login_intent"
            )
            verify_request = urllib.request.Request(
                f"{self.base_url}/api/auth/verify",
                data=json.dumps({"token": token}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.base_url,
                    "Cookie": f"__Host-jobandkill_login_intent={intent}",
                },
                method="POST",
            )
            with self.opener.open(verify_request) as verify_response:
                self.assertEqual(verify_response.status, 200)
                self.assertTrue(json.load(verify_response)["authenticated"])
            with connect(self.db_path) as connection:
                verified_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM user_consents WHERE verified_at IS NOT NULL"
                ).fetchone()["count"]
            self.assertEqual(verified_count, 2)

            send_mail.reset_mock()
            with patch(
                "jobandkill.server.record_login_consents",
                side_effect=RuntimeError("audit storage unavailable"),
            ), self.assertRaises(urllib.error.HTTPError) as audit_failure:
                self.request("/api/auth/request", {
                    "email": "audit-failure@example.test",
                    "privacy_policy_accepted": True,
                    "privacy_policy_version": "2026-09-01",
                    "overseas_transfer_accepted": True,
                    "overseas_transfer_version": "2026-09-01",
                })
            self.assertEqual(audit_failure.exception.code, 500)
            send_mail.assert_not_called()
            with connect(self.db_path) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT id FROM users WHERE email=?", ("audit-failure@example.test",)
                ).fetchone())
                self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM login_tokens").fetchone()["count"], 1)

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

    def test_draft_count_is_bounded_and_all_drafts_delete_is_owner_scoped(self) -> None:
        _, csrf = self.login("bounded@example.test")
        payload = {
            "document_type": "career", "style": "bullet", "target_length": 800,
            "experience_title": "검증", "actions": [], "facts_confirmed": False,
        }
        with connect(self.db_path) as connection:
            owner_id = connection.execute(
                "SELECT id FROM users WHERE email=?", ("bounded@example.test",)
            ).fetchone()["id"]
            other_id = "other-owner"
            connection.execute(
                "INSERT INTO users(id, email) VALUES (?, ?)", (other_id, "other-owner@example.test")
            )
            for index in range(auth.MAX_DRAFTS_PER_USER):
                connection.execute(
                    """
                    INSERT INTO user_drafts(id, user_id, client_key, title, payload_json)
                    VALUES (?, ?, ?, 'bounded', '{}')
                    """,
                    (f"00000000-0000-4000-8000-{index:012d}", owner_id, f"client_{index:04d}"),
                )
            connection.execute(
                """
                INSERT INTO user_drafts(id, user_id, client_key, title, payload_json)
                VALUES ('10000000-0000-4000-8000-000000000000', ?, 'other_client', 'other', '{}')
                """,
                (other_id,),
            )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/drafts", {
                "client_key": "over_limit_client", "payload": payload, "current_step": 1,
            }, csrf)
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(json.load(raised.exception)["error"]["code"], "draft_limit")

        request = urllib.request.Request(
            f"{self.base_url}/api/user/drafts",
            headers={"Origin": self.base_url, "X-CSRF-Token": csrf},
            method="DELETE",
        )
        with self.opener.open(request) as response:
            self.assertEqual(json.load(response), {
                "deleted": True, "count": auth.MAX_DRAFTS_PER_USER,
            })
        with connect(self.db_path) as connection:
            owner_count = connection.execute(
                "SELECT COUNT(*) AS count FROM user_drafts WHERE user_id=?", (owner_id,)
            ).fetchone()["count"]
            other_count = connection.execute(
                "SELECT COUNT(*) AS count FROM user_drafts WHERE user_id=?", (other_id,)
            ).fetchone()["count"]
        self.assertEqual(owner_count, 0)
        self.assertEqual(other_count, 1)

    def test_verify_does_not_set_cookies_when_commit_fails(self) -> None:
        _, sent = self.request("/api/auth/request", {"email": "Writer@Example.test"})
        token = parse_qs(urlsplit(sent["development_magic_link"]).fragment)["login_token"][0]
        sibling_jar = http.cookiejar.CookieJar()
        sibling_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(sibling_jar)
        )
        self.request(
            "/api/auth/request",
            {"email": "Writer@Example.test"},
            opener=sibling_opener,
        )

        with self.failing_commit(), self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/auth/verify", {"token": token})

        self.assertEqual(raised.exception.code, 500)
        self.assertIsNone(raised.exception.headers.get("Set-Cookie"))
        self.assertFalse(any(cookie.name == "jobandkill_session" for cookie in self.jar))
        with connect(self.db_path) as connection:
            login_tokens = connection.execute(
                "SELECT used_at FROM login_tokens WHERE token_hash IS NOT NULL"
            ).fetchall()
            session_count = connection.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"]
        self.assertEqual(len(login_tokens), 2)
        self.assertTrue(all(login_token["used_at"] is None for login_token in login_tokens))
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

    def test_account_export_is_csrf_protected_scoped_and_redacted(self) -> None:
        _, csrf = self.login("owner@example.test")
        payload = {
            "document_type": "career", "style": "bullet", "target_length": 800,
            "experience_title": "소유자 초안", "actions": ["자료 정리"], "facts_confirmed": False,
        }
        self.request("/api/user/drafts", {
            "client_key": "owner_export_1", "payload": payload, "current_step": 4,
        }, csrf)
        with connect(self.db_path) as connection:
            owner_id = connection.execute(
                "SELECT id FROM users WHERE email=?", ("owner@example.test",)
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO user_consents(
                  user_id, consent_type, policy_version, notice_url, notice_sha256,
                  request_token_hash, verified_at
                ) VALUES (?, 'privacy_policy', 'v1', 'https://example.test/privacy', ?, 'internal-token', CURRENT_TIMESTAMP)
                """,
                (owner_id, "a" * 64),
            )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/export", {})
        self.assertEqual(raised.exception.code, 403)

        other_jar = http.cookiejar.CookieJar()
        other_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(other_jar))
        _, other_csrf = self.login("other@example.test", other_opener, other_jar)
        other_payload = {**payload, "experience_title": "다른 사용자 초안"}
        self.request("/api/user/drafts", {
            "client_key": "other_export_1", "payload": other_payload, "current_step": 4,
        }, other_csrf, other_opener)

        _, exported = self.request("/api/user/export", {}, csrf)
        self.assertEqual(exported["account"]["email"], "owner@example.test")
        self.assertEqual([draft["title"] for draft in exported["drafts"]], ["소유자 초안"])
        self.assertEqual(exported["consents"][0]["policy_version"], "v1")
        self.assertNotIn("request_token_hash", exported["consents"][0])
        exported_json = json.dumps(exported)
        for secret_field in ("token_hash", "csrf_hash", "session_token", "request_subject_hash"):
            self.assertNotIn(secret_field, exported_json)

    def test_account_deletion_requires_recent_auth_and_keeps_cookies_on_failure(self) -> None:
        _, csrf = self.login()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/delete-account", {
                "confirmation": auth.ACCOUNT_DELETE_CONFIRMATION,
            })
        self.assertEqual(raised.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/delete-account", {"confirmation": "DELETE ACCOUNT"}, csrf)
        self.assertEqual(raised.exception.code, 422)
        self.assertIsNone(raised.exception.headers.get("Set-Cookie"))
        with connect(self.db_path) as connection:
            connection.execute(
                "UPDATE sessions SET created_at=?", (auth.iso(auth.utcnow() - timedelta(minutes=16)),)
            )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/delete-account", {
                "confirmation": auth.ACCOUNT_DELETE_CONFIRMATION,
            }, csrf)
        self.assertEqual(raised.exception.code, 403)
        self.assertIsNone(raised.exception.headers.get("Set-Cookie"))
        self.assertTrue(any(cookie.name == "jobandkill_session" for cookie in self.jar))
        with connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"], 1)

    def test_account_deletion_cascades_and_clears_cookies(self) -> None:
        _, csrf = self.login()
        payload = {
            "document_type": "career", "style": "bullet", "target_length": 800,
            "experience_title": "삭제할 초안", "actions": ["확인"], "facts_confirmed": False,
        }
        self.request("/api/user/drafts", {
            "client_key": "delete_account_1", "payload": payload, "current_step": 4,
        }, csrf)
        with self.failing_commit(), self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/user/delete-account", {
                "confirmation": auth.ACCOUNT_DELETE_CONFIRMATION,
            }, csrf)
        self.assertEqual(raised.exception.code, 500)
        self.assertIsNone(raised.exception.headers.get("Set-Cookie"))
        self.assertTrue(any(cookie.name == "jobandkill_session" for cookie in self.jar))
        request = urllib.request.Request(
            self.base_url + "/api/user/delete-account",
            data=json.dumps({"confirmation": auth.ACCOUNT_DELETE_CONFIRMATION}).encode(),
            headers={
                "Content-Type": "application/json", "Origin": self.base_url,
                "X-CSRF-Token": csrf,
            },
            method="POST",
        )
        with self.opener.open(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"deleted": True})
            self.assertEqual(len(response.headers.get_all("Set-Cookie")), 2)
            self.assertTrue(all("Max-Age=0" in value for value in response.headers.get_all("Set-Cookie")))
        self.assertFalse(any(cookie.name in {"jobandkill_session", "jobandkill_csrf"} for cookie in self.jar))
        with connect(self.db_path) as connection:
            for table in ("users", "login_tokens", "sessions", "user_drafts"):
                count = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                self.assertEqual(count, 0, table)

    def test_resend_failure_invalidates_the_new_magic_token(self) -> None:
        os.environ["JOBNKILL_AUTH_DEV_SHOW_LINK"] = "0"
        os.environ["JOBNKILL_MAIL_TRANSPORT"] = "resend"
        os.environ["JOBNKILL_RESEND_API_KEY"] = "re_test_key"
        os.environ["JOBNKILL_RESEND_FROM"] = "login@example.test"
        with patch("jobandkill.auth.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/auth/request", {"email": "writer@example.test"})
        self.assertEqual(raised.exception.code, 503)
        with connect(self.db_path) as connection:
            token = connection.execute("SELECT used_at FROM login_tokens").fetchone()
        self.assertIsNotNone(token["used_at"])


class MailTransportTests(unittest.TestCase):
    def test_production_accepts_encrypted_smtp(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_MAIL_TRANSPORT": "smtp",
            "JOBNKILL_SMTP_HOST": "smtp.example.test",
            "JOBNKILL_SMTP_FROM": "login@example.test",
            "JOBNKILL_SMTP_SECURITY": "starttls",
        }, clear=True):
            report = configuration_report(production=True, require_storage=False)
        login_mail = next(item for item in report["checks"] if item["name"] == "login_mail")
        self.assertTrue(login_mail["ready"])

    def test_production_accepts_resend_without_smtp(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_MAIL_TRANSPORT": "resend",
            "JOBNKILL_RESEND_API_KEY": "re_test_key",
            "JOBNKILL_RESEND_FROM": "login@example.test",
        }, clear=True):
            report = configuration_report(production=True, require_storage=False)
        login_mail = next(item for item in report["checks"] if item["name"] == "login_mail")
        self.assertTrue(login_mail["ready"])

    def test_invalid_mail_transport_is_not_ready(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_MAIL_TRANSPORT": "unknown",
        }, clear=True):
            report = configuration_report()
        login_mail = next(item for item in report["checks"] if item["name"] == "login_mail")
        self.assertFalse(login_mail["ready"])

    def test_resend_request_uses_expected_https_json_payload(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def getcode(self) -> int:
                return 202

        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_MAIL_TRANSPORT": "resend",
            "JOBNKILL_RESEND_API_KEY": "re_test_key",
            "JOBNKILL_RESEND_FROM": "login@example.test",
        }, clear=True), patch("jobandkill.auth.urllib.request.urlopen", return_value=Response()) as urlopen:
            auth._send_magic_link("reader@example.test", "https://app.example.test/#login_token=token")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer re_test_key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "from": "login@example.test",
            "to": ["reader@example.test"],
            "subject": "Job&Kill 로그인 링크",
            "text": "아래 링크로 Job&Kill에 로그인하세요. 링크는 15분 동안 한 번만 사용할 수 있습니다.\n\n"
            "https://app.example.test/#login_token=token\n\n요청하지 않았다면 이 메일을 무시하세요.",
        })


class RateLimitIPNormalizationTests(unittest.TestCase):
    def test_ipv4_is_canonical_and_host_specific(self) -> None:
        self.assertEqual(auth._rate_limit_ip_value("192.0.2.7"), "ipv4:192.0.2.7")
        self.assertNotEqual(
            auth._rate_limit_ip_value("192.0.2.7"),
            auth._rate_limit_ip_value("192.0.2.8"),
        )
        self.assertEqual(
            auth._rate_limit_ip_value("::ffff:192.0.2.7"),
            auth._rate_limit_ip_value("192.0.2.7"),
        )

    def test_ipv6_host_rotation_shares_a_64_prefix(self) -> None:
        expected = "ipv6:2001:db8:abcd:1234::/64"
        self.assertEqual(auth._rate_limit_ip_value("2001:db8:abcd:1234::1"), expected)
        self.assertEqual(
            auth._rate_limit_ip_value("2001:0db8:abcd:1234:ffff:ffff:ffff:ffff"),
            expected,
        )

    def test_ipv6_64_boundary_remains_distinct(self) -> None:
        self.assertEqual(
            auth._rate_limit_ip_value("2001:db8:abcd:1234:ffff:ffff:ffff:ffff"),
            "ipv6:2001:db8:abcd:1234::/64",
        )
        self.assertEqual(
            auth._rate_limit_ip_value("2001:db8:abcd:1235::"),
            "ipv6:2001:db8:abcd:1235::/64",
        )
        self.assertNotEqual(
            auth._rate_limit_ip_value("2001:db8:abcd:1234:ffff:ffff:ffff:ffff"),
            auth._rate_limit_ip_value("2001:db8:abcd:1235::"),
        )

    def test_invalid_ip_fallback_is_explicit_stable_and_distinct(self) -> None:
        self.assertEqual(auth._rate_limit_ip_value("not-an-ip"), "invalid:not-an-ip")
        self.assertEqual(auth._rate_limit_ip_value(" not-an-ip "), "invalid:not-an-ip")
        self.assertEqual(auth._rate_limit_ip_value(""), "invalid:<empty>")
        self.assertNotEqual(
            auth._rate_limit_ip_value("not-an-ip"),
            auth._rate_limit_ip_value("another-invalid-value"),
        )

    def test_magic_link_rate_event_hashes_the_normalized_ip_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JOBNKILL_ENV": "test",
            "JOBNKILL_AUTH_RATE_SECRET": "normalization-test-secret",
            "JOBNKILL_AUTH_DEV_SHOW_LINK": "1",
        }):
            db_path = Path(directory) / "rate.db"
            initialize(db_path, force_migrate=True)
            with connect(db_path) as connection, patch(
                "jobandkill.auth._send_magic_link",
            ):
                auth.request_magic_link(
                    connection,
                    "rate-ip@example.test",
                    "2001:db8:abcd:1234::beef",
                    "intent_token_for_ip_rate_test",
                )
                subjects = {
                    row["subject_hash"]
                    for row in connection.execute(
                        "SELECT subject_hash FROM auth_request_events"
                    ).fetchall()
                }
                expected = auth._rate_digest("ip", "ipv6:2001:db8:abcd:1234::/64")
                legacy = auth._rate_digest("ip", "2001:db8:abcd:1234::beef")

        self.assertIn(expected, subjects)
        self.assertNotIn(legacy, subjects)


if __name__ == "__main__":
    unittest.main()
