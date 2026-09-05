from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from jobandkill import auth
from jobandkill.config import draft_retention_days
from jobandkill.db import connect, initialize


class PersonalDataRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "retention.db"
        initialize(self.db_path)
        self.now = auth.utcnow()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def add_user(self, *, verified: bool = False, age_days: int = 0) -> str:
        user_id = str(uuid.uuid4())
        created_at = auth.iso(self.now - timedelta(days=age_days))
        with connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO users(id, email, email_verified_at, created_at) VALUES (?, ?, ?, ?)",
                (user_id, f"{user_id}@example.test", created_at if verified else None, created_at),
            )
        return user_id

    def add_draft(self, user_id: str, updated_at: str) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO user_drafts(id, user_id, client_key, payload_json, updated_at)
                VALUES (?, ?, ?, '{}', ?)
                """,
                (str(uuid.uuid4()), user_id, str(uuid.uuid4()), updated_at),
            )

    def test_dry_run_counts_without_deleting_and_execute_uses_strict_draft_boundary(self) -> None:
        old_user = self.add_user(verified=True)
        boundary_user = self.add_user(verified=True)
        self.add_draft(old_user, auth.iso(self.now - timedelta(days=31)))
        self.add_draft(boundary_user, auth.iso(self.now - timedelta(days=30)))

        with connect(self.db_path) as connection:
            result = auth.cleanup_personal_data(connection, 30, now=self.now)
        self.assertEqual(result["drafts"], 1)
        with connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM user_drafts").fetchone()["count"], 2)

        with connect(self.db_path) as connection:
            result = auth.cleanup_personal_data(connection, 30, execute=True, now=self.now)
        self.assertEqual(result["drafts"], 1)
        with connect(self.db_path) as connection:
            remaining = connection.execute("SELECT user_id FROM user_drafts").fetchall()
        self.assertEqual([row["user_id"] for row in remaining], [boundary_user])

    def test_cleanup_removes_only_operational_records_past_their_boundaries(self) -> None:
        user = self.add_user(verified=True)
        old_token = "old-token"
        boundary_token = "boundary-token"
        active_token = "active-token"
        expired_session = "expired-session"
        active_session = "active-session"
        with connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO login_tokens(token_hash, user_id, expires_at, used_at) VALUES (?, ?, ?, ?)",
                (old_token, user, auth.iso(self.now - timedelta(days=2)), None),
            )
            connection.execute(
                "INSERT INTO login_tokens(token_hash, user_id, expires_at, used_at) VALUES (?, ?, ?, ?)",
                (boundary_token, user, auth.iso(self.now + timedelta(days=2)), auth.iso(self.now - timedelta(days=1))),
            )
            connection.execute(
                "INSERT INTO login_tokens(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (active_token, user, auth.iso(self.now + timedelta(minutes=15))),
            )
            for token_hash, expires_at in ((expired_session, self.now), (active_session, self.now + timedelta(days=1))):
                connection.execute(
                    """
                    INSERT INTO sessions(token_hash, user_id, csrf_hash, expires_at, idle_expires_at, last_seen_at)
                    VALUES (?, ?, 'csrf', ?, ?, ?)
                    """,
                    (token_hash, user, auth.iso(expires_at), auth.iso(expires_at), auth.iso(self.now)),
                )
            connection.execute(
                "INSERT INTO auth_request_events(subject_hash, created_at) VALUES (?, ?)",
                ("old-rate-event", auth.iso(self.now - timedelta(days=2))),
            )
            connection.execute(
                "INSERT INTO auth_request_events(subject_hash, created_at) VALUES (?, ?)",
                ("boundary-rate-event", auth.iso(self.now - timedelta(days=1))),
            )
            for version, accepted_at, verified_at in (
                ("old-pending", self.now - timedelta(days=2), None),
                ("boundary-pending", self.now - timedelta(days=1), None),
                ("old-verified", self.now - timedelta(days=30), self.now - timedelta(days=29)),
            ):
                connection.execute(
                    """
                    INSERT INTO user_consents(
                      user_id, consent_type, policy_version, notice_url, notice_sha256,
                      request_token_hash, accepted_at, verified_at
                    ) VALUES (?, 'privacy_policy', ?, 'https://example.test/privacy', ?, ?, ?, ?)
                    """,
                    (
                        user,
                        version,
                        "a" * 64,
                        f"token-{version}",
                        auth.iso(accepted_at),
                        auth.iso(verified_at) if verified_at else None,
                    ),
                )
            result = auth.cleanup_personal_data(connection, 30, execute=True, now=self.now)
        self.assertEqual(result["login_tokens"], 1)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["auth_request_events"], 1)
        self.assertEqual(result["pending_consents"], 1)
        with connect(self.db_path) as connection:
            tokens = {row["token_hash"] for row in connection.execute("SELECT token_hash FROM login_tokens")}
            sessions = {row["token_hash"] for row in connection.execute("SELECT token_hash FROM sessions")}
            rate_events = {
                row["subject_hash"]
                for row in connection.execute("SELECT subject_hash FROM auth_request_events")
            }
            consent_versions = {
                row["policy_version"]
                for row in connection.execute("SELECT policy_version FROM user_consents")
            }
        self.assertEqual(tokens, {boundary_token, active_token})
        self.assertEqual(sessions, {active_session})
        self.assertEqual(rate_events, {"boundary-rate-event"})
        self.assertEqual(consent_versions, {"boundary-pending", "old-verified"})

    def test_unverified_account_requires_grace_no_drafts_and_no_live_token(self) -> None:
        eligible = self.add_user(age_days=8)
        verified = self.add_user(verified=True, age_days=90)
        boundary = self.add_user(age_days=7)
        with_draft = self.add_user(age_days=8)
        expired_draft = self.add_user(age_days=8)
        live_token = self.add_user(age_days=8)
        self.add_draft(with_draft, auth.iso(self.now))
        self.add_draft(expired_draft, auth.iso(self.now - timedelta(days=31)))
        with connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO login_tokens(token_hash, user_id, expires_at) VALUES ('live', ?, ?)",
                (live_token, auth.iso(self.now + timedelta(minutes=15))),
            )
            preview = auth.cleanup_personal_data(connection, 30, now=self.now)
        self.assertEqual(preview["unverified_accounts"], 2)
        with connect(self.db_path) as connection:
            result = auth.cleanup_personal_data(connection, 30, execute=True, now=self.now)
        self.assertEqual(result["unverified_accounts"], 2)
        with connect(self.db_path) as connection:
            users = {row["id"] for row in connection.execute("SELECT id FROM users")}
        self.assertNotIn(eligible, users)
        self.assertNotIn(expired_draft, users)
        self.assertTrue({verified, boundary, with_draft, live_token}.issubset(users))

    def test_production_retention_requires_an_explicit_positive_integer(self) -> None:
        with patch.dict(os.environ, {"JOBNKILL_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "JOBNKILL_DRAFT_RETENTION_DAYS"):
                draft_retention_days()
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production", "JOBNKILL_DRAFT_RETENTION_DAYS": "30",
        }, clear=True):
            self.assertEqual(draft_retention_days(), 30)
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "production", "JOBNKILL_DRAFT_RETENTION_DAYS": "0",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "양의 정수"):
                draft_retention_days()


if __name__ == "__main__":
    unittest.main()
