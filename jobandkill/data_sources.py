"""Audited source scope: an API catalogue entry is not a collected corpus.

Source metadata was checked against the primary publisher on 2026-09-07.
No keys, customer data, or invented government competency content belong here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .catalog import catalog_coverage
from .db import Connection


CHECKED_ON = "2026-09-07"
DATA_SOURCES = (
    {
        "slug": "ncs-common", "name": "NCS 능력단위 공통정보", "layer": "national_standard",
        "provider": "한국산업인력공단",
        "url": "https://www.data.go.kr/data/15150267/openapi.do",
        "implementation": "adapter_ready", "rights": "unrestricted",
        "fields": ["classification", "competency_code", "competency_name", "level", "definition", "source_date"],
        "not_provided": ["performance_criteria", "knowledge", "skills", "attitudes"],
        "credential_env": "JOBNKILL_NCS_SERVICE_KEY",
        "note": "승인된 키로 페이지별 전체 수집. 이 API의 전체 수집과 모든 NCS 내용 확보는 다릅니다.",
    },
    {
        "slug": "ncs-reference", "name": "NCS 기준정보 7종", "layer": "national_standard",
        "provider": "한국산업인력공단",
        "url": "https://www.data.go.kr/data/15128213/openapi.do",
        "implementation": "spec_verified", "rights": "unrestricted",
        "fields": ["large_class", "middle_class", "small_class", "subclass", "competency_unit", "competency_element", "keyword"],
        "not_provided": ["performance_criteria", "knowledge", "skills", "attitudes"],
        "note": "공식 NCS001~NCS007 명세 확인. 능력단위 요소명은 세부 수행준거·지식·기술·태도와 다릅니다. 수집기 연결 전입니다.",
    },
    {
        "slug": "ncs-training-2025", "name": "NCS 훈련기준 파일", "layer": "national_standard",
        "provider": "한국산업인력공단",
        "url": "https://www.data.go.kr/data/15083321/fileData.do",
        "implementation": "manual_import_ready", "rights": "kogl_type_1_attribution",
        "fields": ["competency_code", "competency_name", "level", "training_hours"],
        "not_provided": ["definition", "performance_criteria", "knowledge", "skills", "attitudes"],
        "source_date": "2025-12-31",
        "note": "한국산업인력공단 제공·공공누리 제1유형(출처표시). 기초 색인이며 훈련교재나 학습모듈 본문이 아닙니다.",
    },
    {
        "slug": "careernet-encyclopedia", "name": "커리어넷 직업백과", "layer": "occupation",
        "provider": "한국직업능력연구원",
        "url": "https://www.career.go.kr/cnet/front/openapi/jobCenter.do",
        "implementation": "spec_verified", "rights": "review_required",
        "fields": ["duties", "knowledge", "abilities", "work_environment", "qualifications", "training"],
        "not_provided": ["institution_specific_requirements"],
        "note": "최신 jobs.json 목록·job.json 상세를 사용합니다. 상업적 저장·재가공 목적과 하위 출처 권한 확인 후 활성화합니다.",
    },
    {
        "slug": "worknet-occupations", "name": "워크넷 직업정보", "layer": "occupation",
        "provider": "한국고용정보원",
        "url": "https://www.data.go.kr/data/3071087/openapi.do",
        "implementation": "not_connected", "rights": "restricted",
        "fields": ["occupation_classification", "occupation_detail"], "not_provided": [],
        "note": "현재 공식 목록 공공누리 제4유형(비영리·변경금지). 별도 허락 없는 상용 DB 수집 제외.",
    },
    {
        "slug": "worknet-dictionary", "name": "한국직업사전", "layer": "occupation",
        "provider": "한국고용정보원",
        "url": "https://www.data.go.kr/data/15037284/openapi.do",
        "implementation": "not_connected", "rights": "restricted",
        "fields": ["duties", "work_process", "occupation_classification"], "not_provided": [],
        "note": "현재 공식 목록 공공누리 제4유형. 공개 조회 가능과 상용 재가공 허용을 구분합니다.",
    },
    {
        "slug": "know-research", "name": "KNOW 재직자조사 성격·지식 원자료", "layer": "occupation",
        "provider": "한국고용정보원",
        "url": "https://www.data.go.kr/data/15114089/fileData.do",
        "implementation": "not_connected", "rights": "restricted",
        "fields": ["knowledge", "personality"], "not_provided": [],
        "note": "학술 목적 및 비영리·변경금지 조건. 상용 서비스 수집 대상에서 제외합니다.",
    },
    {
        "slug": "occupation-outlook-2023", "name": "직업전망·교육훈련·유사직업 파일", "layer": "occupation",
        "provider": "한국고용정보원",
        "url": "https://www.data.go.kr/data/15119098/fileData.do",
        "implementation": "not_connected", "rights": "unrestricted",
        "fields": ["outlook", "training", "education", "related_occupations"],
        "not_provided": ["institution_specific_requirements"],
        "source_date": "2023-08-18",
        "note": "1회성 과거 자료입니다. 최신 직무요건으로 간주하지 않고 원파일 검수 후 보조 자료로 사용합니다.",
    },
    {
        "slug": "data-go-kr-alio", "name": "공공기관 채용정보", "layer": "institution_posting",
        "provider": "재정경제부",
        "url": "https://www.data.go.kr/data/15125273/openapi.do",
        "implementation": "adapter_ready", "rights": "metadata_unrestricted_documents_review",
        "fields": ["institution", "posting", "recruitment_job", "attachment_metadata"],
        "not_provided": ["structured_knowledge", "structured_skills", "structured_attitudes"],
        "credential_env": "JOBNKILL_ALIO_SERVICE_KEY",
        "note": "공고와 첨부 메타를 수집합니다. 실제 직무기술서는 첨부별 이용권한 확인 후 추출합니다.",
    },
    {
        "slug": "ncs-job-description", "name": "기관별 NCS 직무기술서", "layer": "institution_posting",
        "provider": "한국산업인력공단·각 채용기관",
        "url": "https://m.ncs.go.kr/blind/bl04/JdsptList.do",
        "implementation": "metadata_adapter_only", "rights": "document_review_required",
        "fields": ["institution", "job_description_attachment"], "not_provided": [],
        "note": "전체 PDF 수집기가 아닙니다. 문서별 공개·권한·추출성공 여부를 구분합니다.",
    },
    {
        "slug": "cleaneye-job-plus", "name": "지방공공기관 채용정보", "layer": "institution_posting",
        "provider": "행정안전부·한국지역정보개발원",
        "url": "https://job.cleaneye.go.kr/",
        "implementation": "not_connected", "rights": "review_required",
        "fields": ["local_institution", "posting", "duties"], "not_provided": [],
        "note": "ALIO와 다른 기관 범위입니다. 공식 연계 명세·권한 확인 전 수집 완료로 표시하지 않습니다.",
    },
    {
        "slug": "ncs-learning-module", "name": "NCS 학습모듈", "layer": "learning_material",
        "provider": "NCS 학습모듈 제공기관",
        "url": "https://www.ncs.go.kr/unity/th01/selectPolicyPopView.do",
        "implementation": "not_connected", "rights": "restricted",
        "fields": ["learning_material"], "not_provided": [],
        "note": "일반 NCS 기준정보와 구분합니다. 별도 허락 없는 상업적 수집·AI 학습 대상에서 제외합니다.",
    },
)


def source_inventory() -> list[dict[str, Any]]:
    return [{**deepcopy(source), "checked_on": CHECKED_ON} for source in DATA_SOURCES]


def data_coverage(connection: Connection) -> dict[str, Any]:
    """Report observables, never infer missing government data from an error."""
    if connection.dialect == "postgres":
        connection.execute("SET LOCAL statement_timeout = '2000ms'")
    rows = connection.execute(
        """SELECT rights_status, parser_status, COUNT(*) AS count
        FROM attachments GROUP BY rights_status, parser_status"""
    ).fetchall()
    documents: dict[str, int] = {
        "total": 0, "rights_review_required": 0, "rights_restricted": 0,
        "parsed": 0, "parse_failed": 0, "unsupported_format": 0, "not_yet_parsed": 0,
    }
    for row in rows:
        count = int(row["count"])
        documents["total"] += count
        rights, status = row["rights_status"], row["parser_status"]
        if rights in {"review_required", "metadata_only"}:
            documents["rights_review_required"] += count
        elif rights == "restricted":
            documents["rights_restricted"] += count
        elif status == "parsed":
            documents["parsed"] += count
        elif status == "failed":
            documents["parse_failed"] += count
        elif status == "unsupported":
            documents["unsupported_format"] += count
        else:
            documents["not_yet_parsed"] += count
    return {
        "sources": source_inventory(),
        "catalog": catalog_coverage(connection),
        "documents": documents,
        "scope": "registered_sources_only",
        "all_government_data_complete": False,
        "unknown_scope_note": "정부 전체 자료 수는 아직 확인되지 않았습니다. 등록한 정보원별 제공 총수와 실제 적재 수를 대조합니다.",
        "ai_policy": {
            "required_for_collection": False, "required_for_composition": False,
            "official_data_completion_by_ai": False,
            "note": "AI는 선택적인 문장 다듬기에만 사용합니다. 누락된 공식 요건이나 사용자의 경험·실적을 만들어 사실로 저장하지 않습니다.",
        },
    }
