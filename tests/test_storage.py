from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from jobandkill.db import connect, initialize
from jobandkill.ingest import (
    _claim_document,
    _ensure_store_namespace,
    _mark_document_outcome,
    _register_upload_intent,
    drain_document_gc,
    get_attachment_rights_review,
    ingest_records,
    process_documents,
    purge_restricted_documents,
    set_attachment_rights,
)
from jobandkill.storage import LocalDocumentStore, S3DocumentStore, object_key


class LocalStorageTests(unittest.TestCase):
    def test_content_addressed_store_is_idempotent(self) -> None:
        payload = b"official document"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            store = LocalDocumentStore(Path(directory))
            first = store.put(payload, digest, ".PDF", "application/pdf")
            second = store.put(payload, digest, ".pdf", "application/pdf")
            self.assertEqual(first.key, second.key)
            self.assertEqual(first.key, object_key(digest, ".pdf"))
            self.assertEqual((Path(directory) / first.key).read_bytes(), payload)
            store.delete(first.key)
            self.assertFalse((Path(directory) / first.key).exists())

    def test_rejects_invalid_digest(self) -> None:
        with self.assertRaises(ValueError):
            object_key("../escape", ".pdf")

    def test_store_rejects_digest_mismatch_and_repairs_corrupt_object(self) -> None:
        payload = b"verified official document"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            store = LocalDocumentStore(Path(directory))
            with self.assertRaises(ValueError):
                store.put(payload, "0" * 64, ".pdf", "application/pdf")
            key = object_key(digest, ".pdf")
            path = Path(directory) / key
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt")
            store.put(payload, digest, ".pdf", "application/pdf")
            self.assertEqual(path.read_bytes(), payload)

    def test_interrupted_local_upload_is_discoverable_by_final_key_cleanup(self) -> None:
        payload = b"interrupted document"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            store = LocalDocumentStore(Path(directory))
            key = store.key_for(digest, ".pdf")
            final_path = Path(directory) / key
            staging_path = final_path.with_suffix(final_path.suffix + ".staging")
            with patch("jobandkill.storage.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    store.put(payload, digest, ".pdf", "application/pdf")
            self.assertTrue(staging_path.exists())
            store.delete(key)
            self.assertFalse(staging_path.exists())
            self.assertFalse(final_path.exists())

    def test_s3_development_endpoint_requires_exact_loopback_host(self) -> None:
        with patch.dict(os.environ, {
            "JOBNKILL_ENV": "development",
            "JOBNKILL_S3_BUCKET": "test-bucket",
            "JOBNKILL_S3_ENDPOINT_URL": "http://localhost.evil.example",
        }):
            with self.assertRaises(ValueError):
                S3DocumentStore()


class RightsPurgeTests(unittest.TestCase):
    @staticmethod
    def _record(posting_id: str, attachment_id: str) -> dict:
        return {
            "id": posting_id, "institutionName": "한국테스트공사", "title": f"채용 {posting_id}",
            "url": f"https://job.alio.go.kr/{posting_id}", "positions": ["행정"],
            "attachments": [{
                "id": attachment_id, "name": "직무기술서.pdf",
                "url": f"https://www.ncs.go.kr/files/{attachment_id}.pdf", "licenseCode": "KOGL1",
            }],
        }

    def test_restricting_rights_removes_stored_document_and_extracted_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rights.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            record = {
                "id": "P-1", "institutionName": "한국테스트공사", "title": "채용",
                "url": "https://job.alio.go.kr/example", "positions": ["행정"],
                "attachments": [{
                    "id": "A-1", "name": "직무기술서.pdf",
                    "url": "https://www.ncs.go.kr/files/a.pdf", "licenseCode": "KOGL1",
                }],
            }
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
                attachment = connection.execute("SELECT id FROM attachments").fetchone()
                attachment_id = int(attachment["id"])
                payload = b"stored"
                digest = hashlib.sha256(payload).hexdigest()
                store = LocalDocumentStore(document_root)
                stored = store.put(payload, digest, ".pdf", "application/pdf")
                connection.execute(
                    "UPDATE attachments SET storage_backend='local', storage_key=?, storage_path=?, extracted_text='text', sha256=?, parser_status='parsed' WHERE id=?",
                    (stored.key, stored.display_path, digest, attachment_id),
                )
                connection.execute(
                    "INSERT INTO job_profiles(posting_id, attachment_id, institution_name, job_title) SELECT posting_id, id, '한국테스트공사', '행정 상세' FROM attachments WHERE id=?",
                    (attachment_id,),
                )
            with patch.dict(os.environ, {"JOBNKILL_DOCUMENT_DIR": str(document_root)}):
                set_attachment_rights(
                    attachment_id, "restricted", "기관 정책 변경으로 재배포 권리가 취소됨", "compliance@test", db_path
                )
            self.assertFalse((document_root / stored.key).exists())
            with connect(db_path) as connection:
                attachment = connection.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
                self.assertIsNone(attachment["storage_key"])
                self.assertIsNone(attachment["extracted_text"])
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM job_profiles WHERE attachment_id=?", (attachment_id,)
                ).fetchone()["count"]
                self.assertEqual(count, 0)

    def test_shared_object_is_deleted_only_after_last_allowed_reference_is_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "shared.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio",
                    [self._record("P-1", "A-1"), self._record("P-2", "A-2")],
                    "https://www.data.go.kr/",
                )
                attachment_ids = [int(row["id"]) for row in connection.execute(
                    "SELECT id FROM attachments ORDER BY id"
                ).fetchall()]
                payload = b"same official bytes"
                digest = hashlib.sha256(payload).hexdigest()
                store = LocalDocumentStore(document_root)
                stored = store.put(payload, digest, ".pdf", "application/pdf")
                for attachment_id in attachment_ids:
                    connection.execute(
                        """
                        UPDATE attachments SET storage_backend='local', storage_key=?, storage_path=?,
                          sha256=?, parser_status='parsed' WHERE id=?
                        """,
                        (stored.key, stored.display_path, digest, attachment_id),
                    )
            with patch.dict(os.environ, {"JOBNKILL_DOCUMENT_DIR": str(document_root)}):
                set_attachment_rights(
                    attachment_ids[0], "restricted", "첫 번째 문서의 재배포 권한이 철회됨", "compliance@test", db_path
                )
                self.assertTrue((document_root / stored.key).exists())
                set_attachment_rights(
                    attachment_ids[1], "restricted", "두 번째 문서의 재배포 권한이 철회됨", "compliance@test", db_path
                )
            self.assertFalse((document_root / stored.key).exists())

    def test_upstream_license_withdrawal_blocks_automatic_open_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "withdrawal.db"
            initialize(db_path)
            opened = self._record("P-1", "A-1")
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [opened], "https://www.data.go.kr/")
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(attachment["rights_status"], "open_document")
                connection.execute(
                    "INSERT INTO job_profiles(posting_id, attachment_id, institution_name, job_title) "
                    "SELECT posting_id, id, '한국테스트공사', '자동 추출' FROM attachments"
                )
                connection.execute(
                    "UPDATE attachments SET extracted_text='licensed text', parser_status='parsed'"
                )
                withdrawn = self._record("P-1", "A-1")
                withdrawn["attachments"][0].pop("licenseCode")
                ingest_records(connection, "data-go-kr-alio", [withdrawn], "https://www.data.go.kr/")
                current = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(current["rights_status"], "review_required")
                self.assertEqual(current["parser_status"], "blocked_by_rights")
                self.assertIsNone(current["extracted_text"])
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM job_profiles WHERE attachment_id=?", (current["id"],)
                ).fetchone()["count"]
                self.assertEqual(count, 0)

    def test_upgrade_cleanup_removes_profile_left_by_restricted_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "upgrade-rights.db"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
                connection.execute(
                    "INSERT INTO job_profiles(posting_id, attachment_id, institution_name, job_title, "
                    "extraction_status, rights_status) SELECT posting_id, id, '한국테스트공사', "
                    "'이전 추출 프로필', 'machine_extracted', 'open_document' FROM attachments WHERE id=?",
                    (attachment_id,),
                )
                connection.execute(
                    "UPDATE attachments SET rights_status='restricted', parser_status='blocked_by_rights', "
                    "extracted_text='legacy exposed text' WHERE id=?",
                    (attachment_id,),
                )
            cleanup = purge_restricted_documents(db_path)
            self.assertEqual(cleanup["failed"], 0)
            with connect(db_path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) AS count FROM job_profiles WHERE attachment_id=?", (attachment_id,)
                ).fetchone()["count"], 0)
                self.assertIsNone(connection.execute(
                    "SELECT extracted_text FROM attachments WHERE id=?", (attachment_id,)
                ).fetchone()["extracted_text"])

    def test_changed_attachment_requires_fresh_manual_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "authorization.db"
            initialize(db_path)
            original = self._record("P-1", "A-1")
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [original], "https://www.data.go.kr/")
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
            original_identity = get_attachment_rights_review(attachment_id, db_path)["identity"]
            set_attachment_rights(
                attachment_id, "authorized", "기관에서 해당 원문의 분석 이용을 승인함",
                "compliance@test", db_path,
                expected_identity=original_identity,
            )
            replacement = self._record("P-1", "A-1")
            replacement["attachments"][0]["url"] = "https://www.ncs.go.kr/files/replacement.pdf"
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [replacement], "https://www.data.go.kr/"
                )
            # A repeated snapshot must not silently reactivate the authorization
            # that belonged to the former attachment identity.
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [replacement], "https://www.data.go.kr/"
                )
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(attachment["rights_status"], "review_required")
                self.assertEqual(attachment["parser_status"], "blocked_by_rights")
            with self.assertRaises(ValueError):
                set_attachment_rights(
                    attachment_id, "authorized", "이전 URL에 대해 확인했던 이용 승인 근거",
                    "compliance@test", db_path, expected_identity=original_identity,
                )
            self.assertEqual(
                get_attachment_rights_review(attachment_id, db_path)["rights_status"],
                "review_required",
            )

    def test_authoritative_empty_attachment_list_withdraws_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "removed.db"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                removed = self._record("P-1", "A-1")
                removed["attachments"] = []
                ingest_records(
                    connection, "data-go-kr-alio", [removed], "https://www.data.go.kr/"
                )
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(attachment["rights_status"], "review_required")
                self.assertEqual(attachment["parser_status"], "blocked_by_rights")

    def test_malformed_attachment_container_does_not_withdraw_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "malformed.db"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                malformed = self._record("P-1", "A-1")
                malformed["attachments"] = {"item": "unexpected text"}
                ingest_records(
                    connection, "data-go-kr-alio", [malformed], "https://www.data.go.kr/"
                )
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(attachment["rights_status"], "open_document")
                self.assertEqual(attachment["parser_status"], "queued")

    def test_malformed_nonempty_attachment_item_does_not_withdraw_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "malformed-item.db"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                malformed = self._record("P-1", "A-1")
                malformed["attachments"] = [{
                    "id": "A-1", "name": "직무기술서.pdf", "licenseCode": "KOGL1",
                }]
                counters = ingest_records(
                    connection, "data-go-kr-alio", [malformed], "https://www.data.go.kr/"
                )
                attachments = connection.execute(
                    "SELECT * FROM attachments ORDER BY id"
                ).fetchall()

                self.assertEqual(counters["attachment_errors"], 1)
                self.assertEqual(counters["attachments"], 0)
                self.assertEqual(len(attachments), 1)
                self.assertEqual(attachments[0]["external_key"], "A-1")
                self.assertEqual(attachments[0]["rights_status"], "open_document")
                self.assertEqual(attachments[0]["parser_status"], "queued")

    def test_failed_deletion_is_durable_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "retry.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
                payload = b"retry deletion"
                digest = hashlib.sha256(payload).hexdigest()
                stored = LocalDocumentStore(document_root).put(
                    payload, digest, ".pdf", "application/pdf"
                )
                connection.execute(
                    "UPDATE attachments SET storage_backend='local', storage_key=?, storage_path=?, "
                    "sha256=?, parser_status='parsed' WHERE id=?",
                    (stored.key, stored.display_path, digest, attachment_id),
                )
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            with patch.dict(os.environ, environment), patch(
                "jobandkill.storage.LocalDocumentStore.delete", side_effect=OSError("temporary failure")
            ):
                with self.assertRaises(RuntimeError):
                    set_attachment_rights(
                        attachment_id, "restricted", "재배포 권리가 철회되어 원문 삭제가 필요함",
                        "compliance@test", db_path,
                    )
                # Backoff must not make an immediate repeat look successful.
                with self.assertRaises(RuntimeError):
                    set_attachment_rights(
                        attachment_id, "restricted", "재배포 권리가 철회되어 원문 삭제가 필요함",
                        "compliance@test", db_path,
                    )
            self.assertTrue((document_root / stored.key).exists())
            with connect(db_path) as connection:
                attachment = connection.execute(
                    "SELECT storage_key, rights_status FROM attachments WHERE id=?", (attachment_id,)
                ).fetchone()
                queued = connection.execute("SELECT * FROM document_gc_queue").fetchone()
                # Keep the restricted reference as durable target evidence until
                # the physical delete succeeds; public reads are already blocked.
                self.assertEqual(attachment["storage_key"], stored.key)
                self.assertEqual(attachment["rights_status"], "restricted")
                self.assertEqual(queued["attempts"], 1)
                self.assertTrue(queued["last_error"])
                connection.execute("UPDATE document_gc_queue SET next_attempt_at=NULL")
            with patch.dict(os.environ, environment):
                retry = drain_document_gc(db_path)
            self.assertEqual(retry["deleted"], 1)
            self.assertFalse((document_root / stored.key).exists())
            with connect(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) AS count FROM document_gc_queue").fetchone()["count"],
                    0,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT storage_key FROM attachments WHERE id=?", (attachment_id,)
                    ).fetchone()["storage_key"]
                )

    def test_legacy_shared_path_is_retained_for_allowed_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy-shared.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio",
                    [self._record("P-1", "A-1"), self._record("P-2", "A-2")],
                    "https://www.data.go.kr/",
                )
                attachment_ids = [int(row["id"]) for row in connection.execute(
                    "SELECT id FROM attachments ORDER BY id"
                ).fetchall()]
                payload = b"legacy shared bytes"
                digest = hashlib.sha256(payload).hexdigest()
                stored = LocalDocumentStore(document_root).put(
                    payload, digest, ".pdf", "application/pdf"
                )
                for attachment_id in attachment_ids:
                    connection.execute(
                        "UPDATE attachments SET storage_backend='', storage_key=NULL, storage_path=?, "
                        "sha256=?, parser_status='parsed' WHERE id=?",
                        (stored.display_path, digest, attachment_id),
                    )
            with patch.dict(os.environ, {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }):
                set_attachment_rights(
                    attachment_ids[0], "restricted", "첫 문서의 권리가 철회되어 정리가 필요함",
                    "compliance@test", db_path,
                )
            self.assertTrue((document_root / stored.key).exists())
            with connect(db_path) as connection:
                allowed = connection.execute(
                    "SELECT storage_backend, storage_key FROM attachments WHERE id=?",
                    (attachment_ids[1],),
                ).fetchone()
                self.assertEqual(allowed["storage_backend"], "local")
                self.assertEqual(allowed["storage_key"], stored.key)

    def test_unverifiable_legacy_path_is_blocked_without_rolling_back_withdrawal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy-withdrawal.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            opened = self._record("P-1", "A-1")
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [opened], "https://www.data.go.kr/")
                connection.execute(
                    "UPDATE attachments SET storage_backend='', storage_key=NULL, "
                    "storage_path='/outside/current/storage/legacy.pdf', extracted_text='old', "
                    "parser_status='parsed'"
                )
                withdrawn = self._record("P-1", "A-1")
                withdrawn["attachments"][0].pop("licenseCode")
                with patch.dict(os.environ, {"JOBNKILL_DOCUMENT_DIR": str(document_root)}):
                    ingest_records(
                        connection, "data-go-kr-alio", [withdrawn], "https://www.data.go.kr/"
                    )
                current = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(current["rights_status"], "review_required")
                self.assertEqual(current["parser_status"], "blocked_by_rights")
                self.assertIsNone(current["extracted_text"])

    def test_claim_token_fences_stale_worker_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "claim.db"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
            first = _claim_document(db_path, attachment_id)
            assert first is not None
            with connect(db_path) as connection:
                connection.execute(
                    "UPDATE attachments SET parser_status='failed', processing_started_at=NULL, "
                    "processing_token=NULL WHERE id=?",
                    (attachment_id,),
                )
            second = _claim_document(db_path, attachment_id)
            assert second is not None
            self.assertNotEqual(first["processing_token"], second["processing_token"])
            self.assertFalse(_mark_document_outcome(
                db_path, attachment_id, str(first["processing_token"]), "failed", " | stale"
            ))
            with connect(db_path) as connection:
                current = connection.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
                self.assertEqual(current["processing_token"], second["processing_token"])
                self.assertEqual(current["parser_status"], "not_requested")

    def test_process_document_persists_profile_and_object_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "process.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            pages = ["직무수행내용\n민원 자료를 분류한다\n필요기술\n스프레드시트 활용"]
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download",
                return_value=(b"%PDF fake", "application/pdf", "https://www.ncs.go.kr/files/A-1.pdf"),
            ), patch("jobandkill.ingest._parse_document", return_value=(pages, "test-parser-v1")):
                result = process_documents(db_path, 10)
            self.assertEqual(result["parsed"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["gc_failed"], 0)
            with connect(db_path) as connection:
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                registered = connection.execute("SELECT * FROM document_objects").fetchone()
                profiles = connection.execute(
                    "SELECT COUNT(*) AS count FROM job_profiles WHERE attachment_id=?", (attachment["id"],)
                ).fetchone()["count"]
                self.assertEqual(attachment["parser_status"], "parsed")
                self.assertIsNone(attachment["processing_token"])
                self.assertIsNone(attachment["processing_started_at"])
                self.assertEqual(registered["state"], "ready")
                self.assertEqual(profiles, 1)
                self.assertTrue((document_root / attachment["storage_key"]).exists())

    def test_unsafe_hwpx_fails_before_profile_evidence_or_object_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "unsafe-hwpx.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            record = self._record("P-1", "A-1")
            record["attachments"][0]["name"] = "직무기술서.hwpx"
            record["attachments"][0]["url"] = "https://www.ncs.go.kr/files/A-1.hwpx"
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr("Contents/section0.xml", b"<root><t>safe</t></root>")
                archive.writestr("../outside.xml", b"<root><t>unsafe</t></root>")
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download",
                return_value=(
                    archive_buffer.getvalue(), "application/octet-stream",
                    "https://www.ncs.go.kr/files/A-1.hwpx",
                ),
            ):
                result = process_documents(db_path, 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["parsed"], 0)
            with connect(db_path) as connection:
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                extracted_profiles = connection.execute(
                    "SELECT COUNT(*) AS count FROM job_profiles WHERE attachment_id IS NOT NULL"
                ).fetchone()["count"]
                evidence = connection.execute(
                    "SELECT COUNT(*) AS count FROM extraction_evidence"
                ).fetchone()["count"]
                objects = connection.execute(
                    "SELECT COUNT(*) AS count FROM document_objects"
                ).fetchone()["count"]
            self.assertEqual(attachment["parser_status"], "failed")
            self.assertIn("안전 제한을 충족하지 않는 HWPX", attachment["rights_reason"])
            self.assertIsNone(attachment["storage_key"])
            self.assertEqual(extracted_profiles, 0)
            self.assertEqual(evidence, 0)
            self.assertEqual(objects, 0)

    def test_unchanged_url_is_periodically_requeued_for_digest_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "recheck.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            record = self._record("P-1", "A-1")
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
                "JOBNKILL_DOCUMENT_RECHECK_DAYS": "7",
            }
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download",
                return_value=(b"%PDF stable", "application/pdf", "https://www.ncs.go.kr/files/A-1.pdf"),
            ), patch(
                "jobandkill.ingest._parse_document",
                return_value=(["직무수행내용\n정기 재검증"], "test"),
            ):
                self.assertEqual(process_documents(db_path, 1)["parsed"], 1)
                with connect(db_path) as connection:
                    connection.execute(
                        "UPDATE attachments SET collected_at='2000-01-01T00:00:00+00:00'"
                    )
                    ingest_records(
                        connection, "data-go-kr-alio", [record], "https://www.data.go.kr/"
                    )
                    current = connection.execute("SELECT parser_status FROM attachments").fetchone()
                    self.assertEqual(current["parser_status"], "queued")

    def test_authorized_document_byte_change_requires_fresh_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "digest-authorization.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            record = self._record("P-1", "A-1")
            with connect(db_path) as connection:
                ingest_records(connection, "data-go-kr-alio", [record], "https://www.data.go.kr/")
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
            set_attachment_rights(
                attachment_id, "authorized", "기관에서 현재 원문의 서비스 분석 이용을 승인함",
                "compliance@test", db_path,
                expected_identity=get_attachment_rights_review(attachment_id, db_path)["identity"],
            )
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            parse_result = (["직무수행내용\n승인된 직무 내용"], "test")
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download",
                return_value=(b"%PDF approved", "application/pdf", "https://www.ncs.go.kr/files/A-1.pdf"),
            ), patch("jobandkill.ingest._parse_document", return_value=parse_result):
                self.assertEqual(process_documents(db_path, 1)["parsed"], 1)
            with connect(db_path) as connection:
                old_key = str(connection.execute("SELECT storage_key FROM attachments").fetchone()["storage_key"])
                connection.execute("UPDATE attachments SET parser_status='queued'")
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download",
                return_value=(b"%PDF replaced", "application/pdf", "https://www.ncs.go.kr/files/A-1.pdf"),
            ), patch(
                "jobandkill.ingest._parse_document", return_value=parse_result
            ) as replacement_parser:
                result = process_documents(db_path, 1)
            self.assertEqual(result["rights_blocked"], 1)
            replacement_parser.assert_not_called()
            self.assertFalse((document_root / old_key).exists())
            with connect(db_path) as connection:
                current = connection.execute("SELECT * FROM attachments").fetchone()
                latest = connection.execute(
                    "SELECT * FROM rights_decisions ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(current["rights_status"], "review_required")
                self.assertEqual(current["parser_status"], "blocked_by_rights")
                self.assertIsNone(current["storage_key"])
                self.assertEqual(latest["decided_by"], "system:digest-revalidation")

    def test_rights_revocation_during_download_cannot_publish_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "race.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])

            def revoke_then_download(_url: str):
                set_attachment_rights(
                    attachment_id, "restricted", "다운로드 중 기관이 재배포 권리를 철회함",
                    "compliance@test", db_path,
                )
                return b"%PDF stale", "application/pdf", "https://www.ncs.go.kr/files/A-1.pdf"

            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            with patch.dict(os.environ, environment), patch(
                "jobandkill.ingest._download", side_effect=revoke_then_download
            ), patch(
                "jobandkill.ingest._parse_document", return_value=(["직무수행내용\n게시 금지"], "test")
            ):
                result = process_documents(db_path, 10)
            self.assertEqual(result["parsed"], 0)
            self.assertEqual(result["rights_blocked"], 1)
            with connect(db_path) as connection:
                attachment = connection.execute("SELECT * FROM attachments").fetchone()
                self.assertEqual(attachment["rights_status"], "restricted")
                self.assertIsNone(attachment["storage_key"])
                self.assertIsNone(attachment["extracted_text"])
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM job_profiles WHERE attachment_id=?", (attachment_id,)
                    ).fetchone()["count"],
                    0,
                )

    def test_restriction_cleans_owned_upload_after_crash_before_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "owned-upload.db"
            document_root = Path(directory) / "documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])

            claim = _claim_document(db_path, attachment_id)
            assert claim is not None
            payload = b"uploaded before claim finalization"
            digest = hashlib.sha256(payload).hexdigest()
            environment = {
                "JOBNKILL_DOCUMENT_DIR": str(document_root),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }
            with patch.dict(os.environ, environment):
                store = LocalDocumentStore(document_root)
                key = store.key_for(digest, ".pdf")
                _ensure_store_namespace(db_path, store)
                self.assertTrue(_register_upload_intent(
                    db_path, attachment_id, str(claim["processing_token"]),
                    store.backend, key, digest,
                ))
                store.put(payload, digest, ".pdf", "application/pdf")

                set_attachment_rights(
                    attachment_id, "restricted", "업로드 도중 원문 재배포 권리가 철회됨",
                    "compliance@test", db_path,
                )

            self.assertFalse((document_root / key).exists())
            with connect(db_path) as connection:
                attachment = connection.execute(
                    "SELECT rights_status, storage_key FROM attachments WHERE id=?", (attachment_id,)
                ).fetchone()
                self.assertEqual(attachment["rights_status"], "restricted")
                self.assertIsNone(attachment["storage_key"])
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) AS count FROM document_objects WHERE storage_key=?", (key,)
                ).fetchone()["count"], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) AS count FROM document_gc_queue WHERE storage_key=?", (key,)
                ).fetchone()["count"], 0)

    def test_fresh_staging_object_is_not_collected_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "fresh-staging.db"
            initialize(db_path)
            key = object_key("a" * 64, ".pdf")
            with connect(db_path) as connection:
                connection.execute(
                    "INSERT INTO document_objects(storage_backend, storage_key, sha256, state, "
                    "upload_started_at) VALUES ('local', ?, ?, 'staging', CURRENT_TIMESTAMP)",
                    (key, "a" * 64),
                )
            with patch.dict(os.environ, {
                "JOBNKILL_DOCUMENT_DIR": str(Path(directory) / "documents"),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }):
                result = drain_document_gc(db_path)
            self.assertEqual(result["seen"], 0)
            with connect(db_path) as connection:
                state = connection.execute("SELECT state FROM document_objects").fetchone()["state"]
                self.assertEqual(state, "staging")

    def test_gc_reports_due_backlog_beyond_batch_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "gc-backlog.db"
            initialize(db_path)
            with connect(db_path) as connection:
                connection.execute(
                    "INSERT INTO document_gc_queue(storage_backend, storage_key, reason) "
                    "VALUES ('local', 'aa/one.pdf', 'test'), ('local', 'bb/two.pdf', 'test')"
                )
            with patch.dict(os.environ, {
                "JOBNKILL_DOCUMENT_DIR": str(Path(directory) / "documents"),
                "JOBNKILL_STORAGE_BACKEND": "local",
            }), patch("jobandkill.ingest._ensure_store_namespace"):
                result = drain_document_gc(db_path, limit=1)
            self.assertEqual(result["seen"], 1)
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["due_remaining"], 1)
            self.assertEqual(result["pending_total"], 1)

    def test_storage_location_change_cannot_report_old_object_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "namespace.db"
            old_root = Path(directory) / "old-documents"
            new_root = Path(directory) / "new-documents"
            initialize(db_path)
            with connect(db_path) as connection:
                ingest_records(
                    connection, "data-go-kr-alio", [self._record("P-1", "A-1")],
                    "https://www.data.go.kr/",
                )
                attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
            with patch.dict(os.environ, {
                "JOBNKILL_DOCUMENT_DIR": str(old_root), "JOBNKILL_STORAGE_BACKEND": "local",
            }), patch(
                "jobandkill.ingest._download",
                return_value=(b"%PDF old namespace", "application/pdf", "https://www.ncs.go.kr/files/A-1.pdf"),
            ), patch(
                "jobandkill.ingest._parse_document",
                return_value=(["직무수행내용\n원문 위치 검증"], "test"),
            ):
                self.assertEqual(process_documents(db_path, 1)["parsed"], 1)
            with connect(db_path) as connection:
                key = str(connection.execute("SELECT storage_key FROM attachments").fetchone()["storage_key"])
            with patch.dict(os.environ, {
                "JOBNKILL_DOCUMENT_DIR": str(new_root), "JOBNKILL_STORAGE_BACKEND": "local",
            }):
                with self.assertRaises(RuntimeError):
                    set_attachment_rights(
                        attachment_id, "restricted", "기존 원문의 재배포 권리가 철회됨",
                        "compliance@test", db_path,
                    )
            self.assertTrue((old_root / key).exists())
            with connect(db_path) as connection:
                queued = connection.execute("SELECT * FROM document_gc_queue").fetchone()
                self.assertIsNotNone(queued)
                self.assertTrue(queued["last_error"])


if __name__ == "__main__":
    unittest.main()
