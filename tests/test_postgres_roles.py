from __future__ import annotations

import os
import unittest
import uuid


class PostgreSqlRuntimeRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        names = {
            "web": "JOBNKILL_TEST_WEB_POSTGRES_URL",
            "collector": "JOBNKILL_TEST_COLLECTOR_POSTGRES_URL",
            "cleanup": "JOBNKILL_TEST_CLEANUP_POSTGRES_URL",
        }
        if any(not os.getenv(name, "").strip() for name in names.values()):
            raise unittest.SkipTest("least-privilege PostgreSQL URLs not configured")
        try:
            import psycopg
        except ImportError as error:
            raise unittest.SkipTest("psycopg not installed") from error
        cls.psycopg = psycopg
        cls.urls = {role: os.environ[name] for role, name in names.items()}

    def execute(self, role: str, statement: str, params: tuple[object, ...] = ()) -> list[tuple]:
        with self.psycopg.connect(self.urls[role], autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
                return cursor.fetchall() if cursor.description else []

    def assert_denied(self, role: str, statement: str) -> None:
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            self.execute(role, statement)

    def test_web_role_can_use_app_tables_but_cannot_create_objects(self) -> None:
        self.assertEqual(self.execute("web", "SELECT current_user")[0][0], "jobandkill_web")
        self.execute("web", "SELECT value FROM app_metadata WHERE key='schema_version'")
        self.execute("web", "SELECT id FROM sources LIMIT 0")
        user_id = f"role-test-{uuid.uuid4()}"
        self.execute(
            "web", "INSERT INTO users(id, email) VALUES (%s, %s)",
            (user_id, f"{user_id}@example.invalid"),
        )
        self.execute("web", "DELETE FROM users WHERE id=%s", (user_id,))
        self.assert_denied("web", "CREATE TABLE role_escape_test(id INTEGER)")

    def test_collector_cannot_read_or_modify_personal_tables(self) -> None:
        self.assertEqual(
            self.execute("collector", "SELECT current_user")[0][0],
            "jobandkill_collector",
        )
        self.execute("collector", "SELECT value FROM app_metadata WHERE key='schema_version'")
        self.execute("collector", "SELECT id FROM sources LIMIT 0")
        self.execute("collector", "UPDATE sources SET enabled=enabled WHERE FALSE")
        self.assert_denied("collector", "SELECT id FROM users LIMIT 0")
        self.assert_denied("collector", "DELETE FROM user_drafts WHERE FALSE")

    def test_cleanup_can_filter_and_delete_but_not_read_payloads_or_email(self) -> None:
        self.assertEqual(
            self.execute("cleanup", "SELECT current_user")[0][0],
            "jobandkill_cleanup",
        )
        self.execute("cleanup", "SELECT value FROM app_metadata WHERE key='schema_version'")
        self.execute("cleanup", "SELECT id, email_verified_at, created_at FROM users LIMIT 0")
        self.execute("cleanup", "SELECT user_id, updated_at FROM user_drafts LIMIT 0")
        self.execute("cleanup", "DELETE FROM user_drafts WHERE FALSE")
        self.assert_denied("cleanup", "SELECT email FROM users LIMIT 0")
        self.assert_denied("cleanup", "SELECT payload_json FROM user_drafts LIMIT 0")
        self.assert_denied("cleanup", "SELECT id FROM sources LIMIT 0")


if __name__ == "__main__":
    unittest.main()
