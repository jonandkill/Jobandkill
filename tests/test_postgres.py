from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from jobandkill.auth import AuthError, Session, create_draft, delete_draft, update_draft, utcnow
from jobandkill.db import connect, initialize, search_profiles
from jobandkill.ingest import ingest_records, process_documents, set_attachment_rights


POSTGRES_URL = os.getenv("JOBNKILL_TEST_POSTGRES_URL", "")


@unittest.skipUnless(POSTGRES_URL, "JOBNKILL_TEST_POSTGRES_URL not configured")
class PostgreSqlContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        initialize(POSTGRES_URL)
        initialize(POSTGRES_URL)

    def test_schema_seed_is_idempotent(self) -> None:
        with connect(POSTGRES_URL) as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM sources").fetchone()["count"]
        self.assertEqual(count, 5)

    def test_ingest_json_search_and_idempotency(self) -> None:
        marker = uuid.uuid4().hex
        record = {
            "id": f"PG-{marker}",
            "institutionName": f"한국Postgres공사-{marker}",
            "institutionCode": f"PG-{marker}",
            "title": "사무행정 공개채용",
            "positions": ["사무행정"],
            "ncsCategories": ["경영 회계 사무"],
            "url": "https://job.alio.go.kr/example",
            "applicationEnd": "2099-12-31",
        }
        with connect(POSTGRES_URL) as connection:
            first = ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
            second = ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
            self.assertEqual(first["created"], 1)
            self.assertEqual(second["created"], 0)
            found = search_profiles(connection, marker)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["positions"], ["사무행정"])
            connection.execute("DELETE FROM postings WHERE external_id=?", (f"PG-{marker}",))
            connection.execute("DELETE FROM institutions WHERE alio_code=?", (f"PG-{marker}",))

    def test_jsonb_draft_lifecycle_and_device_identity_conflict(self) -> None:
        marker = uuid.uuid4().hex
        user_id = str(uuid.uuid4())
        email = f"postgres-{marker}@example.test"
        client_key = f"browser_{marker}"
        session = Session(user_id, email, "session-hash", "csrf-hash", utcnow())
        payload = {
            "document_type": "career",
            "style": "bullet",
            "target_length": 800,
            "experience_title": "공공 데이터 정비",
            "actions": ["데이터를 검증함"],
            "facts_confirmed": False,
        }
        with connect(POSTGRES_URL) as connection:
            connection.execute("INSERT INTO users(id, email) VALUES (?, ?)", (user_id, email))
            created = create_draft(connection, session, {
                "client_key": client_key, "payload": payload, "current_step": 3,
            })
            self.assertIsInstance(created["payload"], dict)
            self.assertEqual(created["revision"], 1)
            updated = update_draft(connection, session, created["id"], {
                "client_key": client_key,
                "payload": {**payload, "experience_title": "공공 데이터 품질 정비"},
                "current_step": 4,
                "revision": 1,
            })
            self.assertEqual(updated["revision"], 2)
            self.assertEqual(updated["current_step"], 4)
            with self.assertRaises(AuthError) as raised:
                update_draft(connection, session, created["id"], {
                    "client_key": f"other_{marker}", "payload": payload,
                    "current_step": 4, "revision": 2,
                })
            self.assertEqual(raised.exception.code, "draft_identity_conflict")
            delete_draft(connection, session, created["id"])
            connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    def test_document_registry_claim_and_cleanup_contract(self) -> None:
        marker = uuid.uuid4().hex
        record = {
            "id": f"PG-DOC-{marker}",
            "institutionName": f"한국문서공사-{marker}",
            "institutionCode": f"PG-DOC-{marker}",
            "title": "문서 처리 계약 테스트",
            "positions": ["사무행정"],
            "url": "https://job.alio.go.kr/example",
            "attachments": [{
                "id": f"A-{marker}", "name": "직무기술서.pdf",
                "url": "https://www.ncs.go.kr/files/contract.pdf", "licenseCode": "KOGL1",
            }],
        }
        with connect(POSTGRES_URL) as connection:
            ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
            attachment_id = int(connection.execute(
                "SELECT a.id FROM attachments a JOIN postings p ON p.id=a.posting_id "
                "WHERE p.external_id=?", (record["id"],)
            ).fetchone()["id"])
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(Path(directory) / "documents"),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download",
                return_value=(b"%PDF postgres", "application/pdf", record["attachments"][0]["url"]),
            ), patch(
                "jobandkill.ingest._parse_document",
                return_value=(["직무수행내용\nPostgreSQL 계약 검증"], "pg-test"),
            ):
                processed = process_documents(POSTGRES_URL, 1)
                self.assertEqual(processed["parsed"], 1)
                with connect(POSTGRES_URL) as connection:
                    attachment = connection.execute(
                        "SELECT * FROM attachments WHERE id=?", (attachment_id,)
                    ).fetchone()
                    registry = connection.execute(
                        "SELECT state FROM document_objects WHERE storage_key=?",
                        (attachment["storage_key"],),
                    ).fetchone()
                    self.assertEqual(attachment["parser_status"], "parsed")
                    self.assertIsNone(attachment["processing_token"])
                    self.assertEqual(registry["state"], "ready")
                set_attachment_rights(
                    attachment_id, "restricted", "PostgreSQL 원문 정리 계약 테스트",
                    "ci@test", POSTGRES_URL,
                )
                with connect(POSTGRES_URL) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM document_gc_queue"
                        ).fetchone()["count"],
                        0,
                    )
                    connection.execute("DELETE FROM postings WHERE external_id=?", (record["id"],))
                    connection.execute(
                        "DELETE FROM institutions WHERE alio_code=?", (record["institutionCode"],)
                    )
                    connection.execute("DELETE FROM storage_namespaces WHERE storage_backend='local'")


if __name__ == "__main__":
    unittest.main()
