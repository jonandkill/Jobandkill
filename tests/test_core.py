from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from jobandkill.db import connect, get_profile, initialize, public_stats, search_profiles
from jobandkill.ingest import (
    _hwpx_pages,
    extract_sections,
    get_attachment_rights_review,
    ingest_records,
    normalize_api_record,
    parse_api_payload,
    set_attachment_rights,
    sync_source,
)
from jobandkill.writer import DraftValidationError, compose


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "test.db"
        initialize(self.db_path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def sample_record(self) -> dict:
        return {
            "recrutPblntSn": "OFFICIAL-100",
            "instNm": "한국테스트공사",
            "instCd": "T100",
            "recrutPbancTtl": "2026년 사무행정 공개채용",
            "recrutPbancUrl": "https://job.alio.go.kr/recruit/example",
            "hireTypeNm": "정규직",
            "recrutJobsNm": ["사무행정"],
            "ncsCdNm": ["경영·회계·사무 > 총무·인사 > 일반사무"],
            "pbancBgngYmd": "20260901",
            "pbancEndYmd": "20260915",
            "attachments": [
                {
                    "fileNo": "A-1",
                    "fileName": "NCS 직무기술서.pdf",
                    "fileUrl": "https://www.ncs.go.kr/files/example.pdf",
                }
            ],
        }

    def test_normalizes_official_api_aliases(self) -> None:
        item = normalize_api_record(self.sample_record(), "https://www.data.go.kr/")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.external_id, "OFFICIAL-100")
        self.assertEqual(item.positions, ["사무행정"])
        self.assertEqual(item.application_end, "2026-09-15")
        self.assertEqual(item.attachments[0].kind, "job_description")

    def test_normalizes_xml_style_single_attachment_container(self) -> None:
        record = self.sample_record()
        record["attachments"] = {"item": record["attachments"][0]}
        item = normalize_api_record(record, "https://www.data.go.kr/")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertTrue(item.attachments_authoritative)
        self.assertEqual(len(item.attachments), 1)
        self.assertEqual(item.attachments[0].external_key, "A-1")

    def test_ingest_is_idempotent_and_searchable(self) -> None:
        with connect(self.db_path) as connection:
            first = ingest_records(
                connection, "data-go-kr-alio", [self.sample_record()], "https://www.data.go.kr/"
            )
            second = ingest_records(
                connection, "data-go-kr-alio", [self.sample_record()], "https://www.data.go.kr/"
            )
            connection.commit()
            self.assertEqual(first["created"], 1)
            self.assertEqual(second["created"], 0)
            self.assertEqual(public_stats(connection)["postings"], 1)
            profiles = search_profiles(connection, "사무행정")
            self.assertEqual(len(profiles), 1)
            fts_rows = connection.execute(
                "SELECT rowid FROM job_profiles_fts WHERE job_profiles_fts MATCH ?", ("사무행정",)
            ).fetchall()
            self.assertEqual(len(fts_rows), 1)
            profile = get_profile(connection, profiles[0]["id"])
            self.assertEqual(profile["institution_name"], "한국테스트공사")
            attachment = connection.execute("SELECT * FROM attachments").fetchone()
            self.assertEqual(attachment["rights_status"], "review_required")
            self.assertEqual(attachment["parser_status"], "blocked_by_rights")

    def test_rights_decision_is_audited(self) -> None:
        with connect(self.db_path) as connection:
            ingest_records(connection, "data-go-kr-alio", [self.sample_record()], "https://www.data.go.kr/")
            connection.commit()
            attachment_id = connection.execute("SELECT id FROM attachments").fetchone()["id"]
        set_attachment_rights(
            attachment_id,
            "authorized",
            "기관으로부터 서비스 내 분석 이용 허가를 받음",
            "compliance@example.test",
            self.db_path,
            expected_identity=get_attachment_rights_review(attachment_id, self.db_path)["identity"],
        )
        with connect(self.db_path) as connection:
            attachment = connection.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            audit = connection.execute("SELECT * FROM rights_decisions WHERE attachment_id=?", (attachment_id,)).fetchone()
            self.assertEqual(attachment["parser_status"], "queued")
            self.assertEqual(audit["new_status"], "authorized")

    def test_orphaned_attachment_profile_is_never_treated_as_metadata(self) -> None:
        with connect(self.db_path) as connection:
            ingest_records(
                connection, "data-go-kr-alio", [self.sample_record()], "https://www.data.go.kr/"
            )
            posting_id = int(connection.execute("SELECT id FROM postings").fetchone()["id"])
            attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
            cursor = connection.execute(
                """
                INSERT INTO job_profiles(
                  posting_id, attachment_id, institution_name, job_title, summary,
                  extraction_status, rights_status, parser_version
                ) VALUES (?, ?, '한국테스트공사', '고유파생직무', '삭제된 첨부에서 추출',
                          'machine_extracted', 'open_document', 'test')
                """,
                (posting_id, attachment_id),
            )
            profile_id = int(cursor.lastrowid)
            # Simulate an upgraded legacy schema whose FK used ON DELETE SET NULL.
            connection.execute(
                "UPDATE job_profiles SET attachment_id=NULL WHERE id=?", (profile_id,)
            )
            self.assertEqual(search_profiles(connection, "고유파생직무"), [])
            self.assertIsNone(get_profile(connection, profile_id))

    def test_sync_continues_pages_and_reports_partial_after_cleanup_error(self) -> None:
        first = self.sample_record()
        first["attachments"] = [{"fileNo": "A-bad", "fileName": "URL 누락.pdf"}]
        second = self.sample_record()
        second["recrutPblntSn"] = "OFFICIAL-200"
        second["recrutPbancTtl"] = "2026년 전산 공개채용"

        class TwoPageSource:
            truncated = False
            total_count = 2

            def fetch(self):
                yield [first]
                yield [second]

        failed_cleanup = {
            "failed": 1, "due_remaining": 0, "pending_total": 1,
            "pending_attachments": 0, "quarantined": 0,
        }
        clean_cleanup = {
            "failed": 0, "due_remaining": 0, "pending_total": 0,
            "pending_attachments": 0, "quarantined": 0,
        }
        with patch.dict(
            "jobandkill.ingest.SOURCE_FACTORIES", {"data-go-kr-alio": TwoPageSource}
        ), patch(
            "jobandkill.ingest.purge_restricted_documents",
            side_effect=[failed_cleanup, clean_cleanup, clean_cleanup],
        ):
            result = sync_source("data-go-kr-alio", self.db_path)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["seen"], 2)
        self.assertEqual(result["attachment_errors"], 1)
        self.assertEqual(result["cleanup_failed"], 1)
        with connect(self.db_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS count FROM postings").fetchone()["count"], 2
            )

    def test_stale_review_token_cannot_override_newer_restriction(self) -> None:
        with connect(self.db_path) as connection:
            ingest_records(
                connection, "data-go-kr-alio", [self.sample_record()], "https://www.data.go.kr/"
            )
            attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
        stale_identity = get_attachment_rights_review(attachment_id, self.db_path)["identity"]
        set_attachment_rights(
            attachment_id, "restricted", "기관 정책상 상업 서비스 재이용이 금지됨",
            "compliance@example.test", self.db_path,
        )
        with self.assertRaises(ValueError):
            set_attachment_rights(
                attachment_id, "authorized", "이전에 검토했던 허용 증빙을 뒤늦게 적용",
                "compliance@example.test", self.db_path,
                expected_identity=stale_identity,
            )
        self.assertEqual(
            get_attachment_rights_review(attachment_id, self.db_path)["rights_status"],
            "restricted",
        )

    def test_review_token_expires_when_feed_removes_already_blocked_file(self) -> None:
        record = self.sample_record()
        with connect(self.db_path) as connection:
            ingest_records(
                connection, "data-go-kr-alio", [record], "https://www.data.go.kr/"
            )
            attachment_id = int(connection.execute("SELECT id FROM attachments").fetchone()["id"])
        stale_identity = get_attachment_rights_review(attachment_id, self.db_path)["identity"]
        removed = self.sample_record()
        removed["attachments"] = []
        with connect(self.db_path) as connection:
            ingest_records(
                connection, "data-go-kr-alio", [removed], "https://www.data.go.kr/"
            )
        with self.assertRaises(ValueError):
            set_attachment_rights(
                attachment_id, "authorized", "삭제 전 검토했던 이용 승인 근거를 적용",
                "compliance@example.test", self.db_path,
                expected_identity=stale_identity,
            )


class ParsingTests(unittest.TestCase):
    @staticmethod
    def hwpx(entries: list[tuple[str, bytes]], compression: int = zipfile.ZIP_STORED) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=compression) as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
        return output.getvalue()

    def test_json_and_xml_payloads(self) -> None:
        json_records, total = parse_api_payload(
            json.dumps({"response": {"body": {"items": [{"id": 1}], "totalCount": 1}}}).encode()
        )
        self.assertEqual(json_records[0]["id"], 1)
        self.assertEqual(total, 1)
        xml_records, xml_total = parse_api_payload(
            b"<response><body><items><item><id>2</id></item></items><totalCount>1</totalCount></body></response>"
        )
        self.assertEqual(xml_records[0]["id"], "2")
        self.assertEqual(xml_total, 1)

    def test_extracts_ncs_sections_with_page_evidence(self) -> None:
        sections, evidence = extract_sections(
            [
                "직무수행내용\n○ 민원 자료를 분류한다\n○ 처리 현황을 보고한다",
                "필요지식\n개인정보보호 관련 규정\n필요기술\n스프레드시트 활용 능력\n직무수행태도\n정확한 기록 태도",
            ]
        )
        self.assertIn("민원 자료를 분류한다", sections["duties"])
        self.assertIn("스프레드시트 활용 능력", sections["skills"])
        self.assertTrue(any(item["page_number"] == 2 for item in evidence))

    def test_extraction_rejects_evidence_overflow_instead_of_silently_truncating(self) -> None:
        page = "직무수행내용\n" + "\n".join(f"서로 다른 업무 항목 {index}" for index in range(101))
        with self.assertRaisesRegex(ValueError, "추출 항목이 안전 한도"):
            extract_sections([page])

    def test_hwpx_accepts_bounded_section_xml_and_ignores_other_regular_files(self) -> None:
        payload = self.hwpx([
            ("mimetype", b"application/hwp+zip"),
            ("Contents/section0.xml", "<root><t>직무수행내용</t><t>자료 분석</t></root>".encode()),
        ])
        self.assertEqual(_hwpx_pages(payload), ["직무수행내용\n자료 분석"])

    def test_hwpx_rejects_oversized_archive_before_opening_members(self) -> None:
        payload = self.hwpx([("Contents/section0.xml", b"<root><t>safe</t></root>")])
        with patch("jobandkill.ingest.MAX_DOCUMENT_BYTES", len(payload) - 1):
            with self.assertRaisesRegex(ValueError, "25MB 압축 파일 크기 제한"):
                _hwpx_pages(payload)

    def test_hwpx_rejects_member_count_and_unsafe_paths(self) -> None:
        many = self.hwpx([
            ("Contents/section0.xml", b"<root><t>safe</t></root>"),
            ("mimetype", b"application/hwp+zip"),
        ])
        with patch("jobandkill.ingest.MAX_HWPX_MEMBERS", 1):
            with self.assertRaisesRegex(ValueError, "압축 항목 수 제한"):
                _hwpx_pages(many)
        traversal = self.hwpx([
            ("Contents/section0.xml", b"<root><t>safe</t></root>"),
            ("../outside.xml", b"<root/>")
        ])
        with self.assertRaisesRegex(ValueError, "허용되지 않은 압축 항목 경로"):
            _hwpx_pages(traversal)

    def test_hwpx_rejects_member_and_total_uncompressed_limits(self) -> None:
        oversized_member = self.hwpx([
            ("Contents/section0.xml", b"<root><t>0123456789</t></root>")
        ])
        with patch("jobandkill.ingest.MAX_HWPX_MEMBER_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "개별 압축 항목 크기 제한"):
                _hwpx_pages(oversized_member)
        oversized_total = self.hwpx([
            ("Contents/section0.xml", b"<root><t>a</t></root>"),
            ("Preview/PrvText.txt", b"b" * 30),
        ])
        with patch("jobandkill.ingest.MAX_HWPX_UNCOMPRESSED_BYTES", 40):
            with self.assertRaisesRegex(ValueError, "전체 압축 해제 크기 제한"):
                _hwpx_pages(oversized_total)

    def test_hwpx_rejects_unsupported_compression_and_extreme_ratio(self) -> None:
        unsupported = self.hwpx(
            [("Contents/section0.xml", b"<root><t>safe</t></root>")],
            compression=zipfile.ZIP_BZIP2,
        )
        with self.assertRaisesRegex(ValueError, "지원하지 않는 압축 방식"):
            _hwpx_pages(unsupported)
        symlink_buffer = io.BytesIO()
        symlink = zipfile.ZipInfo("Contents/section0.xml")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(symlink_buffer, "w") as archive:
            archive.writestr(symlink, b"mimetype")
        with self.assertRaisesRegex(ValueError, "일반 파일이 아닌 압축 항목"):
            _hwpx_pages(symlink_buffer.getvalue())
        high_ratio = self.hwpx(
            [("Contents/section0.xml", b"<root><t>" + b"a" * 2_000 + b"</t></root>")],
            compression=zipfile.ZIP_DEFLATED,
        )
        with patch("jobandkill.ingest.MAX_HWPX_COMPRESSION_RATIO", 5):
            with self.assertRaisesRegex(ValueError, "압축비 제한"):
                _hwpx_pages(high_ratio)


class WriterTests(unittest.TestCase):
    def base_draft(self) -> dict:
        return {
            "document_type": "experience",
            "style": "narrative",
            "target_length": 800,
            "institution": "한국테스트공사",
            "target_job": "사무행정",
            "experience_title": "민원 처리 기준 정비",
            "organization": "고객지원팀 인턴",
            "period_start": "2025-03",
            "period_end": "2025-08",
            "role": "민원 자료 분류와 개선안 초안 작성",
            "situation": "담당자마다 분류 기준이 달라 처리 현황 집계가 지연되는",
            "objective": "공통 분류 기준을 마련하는 것",
            "judgment": "실제 반복 민원을 먼저 수치로 확인해야 한다는 점",
            "actions": ["최근 3개월 민원 240건을 유형별로 분류", "담당자 검토를 받아 분류표를 보완"],
            "tools": ["Excel 피벗테이블"],
            "collaboration": "담당자 3명에게 예외 사례를 확인",
            "result": "월간 집계에 같은 분류표가 사용되는 변화",
            "evidence": "팀 공유 폴더의 최종 분류표와 월간 보고서",
            "contribution": "초기 데이터 분류와 분류표 초안을 직접 담당하고 최종 승인은 팀장에게 받음",
            "learning": "현장 사용자의 예외 사례까지 확인해야 기준이 정착된다는 점",
            "facts_confirmed": True,
        }

    def test_requires_fact_confirmation(self) -> None:
        draft = self.base_draft()
        draft["facts_confirmed"] = False
        with self.assertRaises(DraftValidationError):
            compose(draft)

    def test_narrative_uses_only_provided_facts(self) -> None:
        result = compose(self.base_draft())
        self.assertEqual(result["fact_coverage"], 100)
        self.assertIn("민원 240건", result["output"])
        self.assertIn("최종 승인은 팀장", result["output"])
        self.assertNotIn("[확인 필요]", result["output"])

    def test_missing_fact_remains_explicit(self) -> None:
        draft = self.base_draft()
        draft["style"] = "bullet"
        draft["result"] = ""
        result = compose(draft)
        self.assertIn("- 결과: [확인 필요]", result["output"])
        self.assertIn("결과", result["missing_fields"])

    def test_oversized_input_is_rejected_instead_of_silently_truncated(self) -> None:
        draft = self.base_draft()
        draft["situation"] = "가" * 4_001
        with self.assertRaises(DraftValidationError) as raised:
            compose(draft)
        self.assertIn("situation", raised.exception.errors)

        draft = self.base_draft()
        draft["actions"] = ["가" * 1_001]
        with self.assertRaises(DraftValidationError) as raised:
            compose(draft)
        self.assertIn("actions", raised.exception.errors)

    def test_resident_registration_number_is_rejected(self) -> None:
        for identifier in ("900101-1234567", "９００１０１ - ５２３４５６７", "900101   8234567"):
            with self.subTest(identifier=identifier):
                draft = self.base_draft()
                draft["evidence"] = f"확인용 {identifier}"
                with self.assertRaises(DraftValidationError) as raised:
                    compose(draft)
                self.assertIn("personal_information", raised.exception.errors)


if __name__ == "__main__":
    unittest.main()
