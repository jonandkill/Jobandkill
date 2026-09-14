from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobandkill.config import configuration_report
from jobandkill.db import connect, initialize
from jobandkill.privacy import (
    ConsentRequirement,
    PrivacyError,
    login_consents,
    privacy_configuration_check,
    public_privacy_config,
    record_login_consents,
    require_current_consents,
    verify_login_consents,
)


class PrivacyConsentTests(unittest.TestCase):
    def production_environment(self, **overrides: str) -> dict[str, str]:
        values = {
            "JOBNKILL_ENV": "production",
            "JOBNKILL_PRIVACY_POLICY_URL": "https://example.test/privacy",
            "JOBNKILL_PRIVACY_POLICY_VERSION": "2026-09-01",
            "JOBNKILL_PRIVACY_POLICY_SHA256": "a" * 64,
            "JOBNKILL_OVERSEAS_TRANSFER_REQUIRED": "0",
            "JOBNKILL_OVERSEAS_TRANSFER_POLICY_URL": "",
            "JOBNKILL_OVERSEAS_TRANSFER_VERSION": "",
            "JOBNKILL_OVERSEAS_TRANSFER_SHA256": "",
        }
        values.update(overrides)
        return values

    def test_production_requires_configured_matching_policy_consent(self) -> None:
        with patch.dict(os.environ, self.production_environment(), clear=False):
            with self.assertRaises(PrivacyError) as missing:
                login_consents({})
            self.assertEqual(missing.exception.code, "consent_required")
            with self.assertRaises(PrivacyError) as mismatched:
                login_consents({"privacy_policy_accepted": True, "privacy_policy_version": "old"})
            self.assertEqual(mismatched.exception.code, "consent_version")
            self.assertEqual(
                login_consents({"privacy_policy_accepted": True, "privacy_policy_version": "2026-09-01"})[0].consent_type,
                "privacy_policy",
            )

    def test_overseas_consent_is_separate_when_enabled(self) -> None:
        environment = self.production_environment(
            JOBNKILL_OVERSEAS_TRANSFER_REQUIRED="1",
            JOBNKILL_OVERSEAS_TRANSFER_POLICY_URL="https://example.test/overseas-transfer",
            JOBNKILL_OVERSEAS_TRANSFER_VERSION="2026-09-01",
            JOBNKILL_OVERSEAS_TRANSFER_SHA256="b" * 64,
        )
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(PrivacyError) as required:
                login_consents({"privacy_policy_accepted": True, "privacy_policy_version": "2026-09-01"})
            self.assertEqual(required.exception.code, "consent_required")
            self.assertEqual(len(login_consents({
                "privacy_policy_accepted": True, "privacy_policy_version": "2026-09-01",
                "overseas_transfer_accepted": True, "overseas_transfer_version": "2026-09-01",
            })), 2)

    def test_production_refuses_missing_notice_configuration(self) -> None:
        with patch.dict(os.environ, self.production_environment(JOBNKILL_PRIVACY_POLICY_URL=""), clear=False):
            with self.assertRaises(PrivacyError) as raised:
                public_privacy_config()
        self.assertEqual(raised.exception.code, "privacy_config")

    def test_production_requires_explicit_overseas_transfer_decision(self) -> None:
        environment = self.production_environment()
        environment.pop("JOBNKILL_OVERSEAS_TRANSFER_REQUIRED")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(PrivacyError) as missing:
                public_privacy_config()
        self.assertEqual(missing.exception.code, "privacy_config")

        with patch.dict(
            os.environ,
            self.production_environment(JOBNKILL_OVERSEAS_TRANSFER_REQUIRED="maybe"),
            clear=True,
        ):
            with self.assertRaises(PrivacyError) as invalid:
                public_privacy_config()
        self.assertEqual(invalid.exception.code, "privacy_config")

    def test_configuration_report_blocks_production_startup_without_notices(self) -> None:
        environment = self.production_environment(
            JOBNKILL_PRIVACY_POLICY_URL="", JOBNKILL_PRIVACY_POLICY_VERSION="",
        )
        with patch.dict(os.environ, environment, clear=False):
            report = configuration_report(production=True, require_storage=False)
            ready, _ = privacy_configuration_check(True)
        privacy_check = next(item for item in report["checks"] if item["name"] == "privacy_notices")
        self.assertFalse(ready)
        self.assertFalse(privacy_check["ready"])

    def test_development_does_not_require_placeholder_consent(self) -> None:
        with patch.dict(os.environ, {"JOBNKILL_ENV": "test"}, clear=False):
            self.assertEqual(login_consents({}), [])

    def test_audit_rows_are_linked_to_user_and_exclude_ip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "privacy.db"
            initialize(database)
            with connect(database) as connection:
                connection.execute("INSERT INTO users(id, email) VALUES (?, ?)", ("user-1", "writer@example.test"))
                # Exercise the storage path with an explicit approved requirement.
                record_login_consents(
                    connection,
                    "user-1",
                    "token-hash",
                    [ConsentRequirement("privacy_policy", "v1", "https://example.test/privacy", "a" * 64)],
                )
                row = connection.execute(
                    """
                    SELECT user_id, consent_type, policy_version, notice_url, notice_sha256,
                           request_token_hash, verified_at, metadata_json
                    FROM user_consents
                    """
                ).fetchone()
            self.assertEqual((row["user_id"], row["consent_type"], row["policy_version"]), ("user-1", "privacy_policy", "v1"))
            self.assertEqual(row["notice_url"], "https://example.test/privacy")
            self.assertEqual(row["notice_sha256"], "a" * 64)
            self.assertEqual(row["request_token_hash"], "token-hash")
            self.assertIsNone(row["verified_at"])
            self.assertNotIn("ip", row["metadata_json"].lower())

    def test_pending_consent_and_any_notice_identity_change_block_writes(self) -> None:
        environment = self.production_environment()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ), patch(
            "jobandkill.db._production_environment", return_value=False,
        ):
            database = Path(directory) / "consent-version.db"
            # Production runtime roles intentionally cannot run DDL.  This test
            # performs the separate one-time administrator migration first.
            initialize(database, force_migrate=True)
            requirement = ConsentRequirement(
                "privacy_policy", "2026-09-01", "https://example.test/privacy", "a" * 64
            )
            with connect(database) as connection:
                connection.execute(
                    "INSERT INTO users(id, email) VALUES (?, ?)",
                    ("user-1", "writer@example.test"),
                )
                record_login_consents(connection, "user-1", "token-hash", [requirement])
                with self.assertRaises(PrivacyError) as pending:
                    require_current_consents(connection, "user-1")
                self.assertEqual(pending.exception.code, "consent_refresh_required")
                verify_login_consents(connection, "user-1", "token-hash")
                require_current_consents(connection, "user-1")

            with patch.dict(
                os.environ,
                {**environment, "JOBNKILL_PRIVACY_POLICY_VERSION": "2026-10-01"},
                clear=True,
            ), connect(database) as connection:
                with self.assertRaises(PrivacyError) as stale:
                    require_current_consents(connection, "user-1")
            self.assertEqual(stale.exception.code, "consent_refresh_required")

            for name, override in (
                (
                    "notice URL",
                    {"JOBNKILL_PRIVACY_POLICY_URL": "https://example.test/privacy-revised"},
                ),
                ("notice SHA-256", {"JOBNKILL_PRIVACY_POLICY_SHA256": "b" * 64}),
            ):
                with self.subTest(change=name), patch.dict(
                    os.environ,
                    {**environment, **override},
                    clear=True,
                ), connect(database) as connection:
                    with self.assertRaises(PrivacyError) as changed_notice:
                        require_current_consents(connection, "user-1")
                    self.assertEqual(
                        changed_notice.exception.code, "consent_refresh_required"
                    )


if __name__ == "__main__":
    unittest.main()
