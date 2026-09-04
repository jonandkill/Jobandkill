from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobandkill.db import connect, get_profile, initialize, public_stats, search_profiles
from jobandkill.ingest import (
    extract_sections,
    ingest_records,
    normalize_api_record,
    parse_api_payload,
    set_attachment_rights,
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
        )
        with connect(self.db_path) as connection:
            attachment = connection.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            audit = connection.execute("SELECT * FROM rights_decisions WHERE attachment_id=?", (attachment_id,)).fetchone()
            self.assertEqual(attachment["parser_status"], "queued")
            self.assertEqual(audit["new_status"], "authorized")


class ParsingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
