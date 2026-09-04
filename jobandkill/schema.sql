PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    sync_mode TEXT NOT NULL CHECK (sync_mode IN ('api', 'metadata', 'manual')),
    default_rights TEXT NOT NULL CHECK (
        default_rights IN ('open_metadata', 'open_document', 'metadata_only', 'restricted', 'review_required')
    ),
    license_note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_success_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_created INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    attachments_seen INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS institutions (
    id INTEGER PRIMARY KEY,
    alio_code TEXT UNIQUE,
    name TEXT NOT NULL,
    institution_type TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS institutions_name_unique
    ON institutions(name) WHERE alio_code IS NULL OR alio_code = '';

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    title TEXT NOT NULL,
    employment_type TEXT NOT NULL DEFAULT '',
    hiring_type TEXT NOT NULL DEFAULT '',
    education TEXT NOT NULL DEFAULT '',
    regions_json TEXT NOT NULL DEFAULT '[]',
    ncs_categories_json TEXT NOT NULL DEFAULT '[]',
    positions_json TEXT NOT NULL DEFAULT '[]',
    headcount_text TEXT NOT NULL DEFAULT '',
    qualifications TEXT NOT NULL DEFAULT '',
    preferences TEXT NOT NULL DEFAULT '',
    selection_process TEXT NOT NULL DEFAULT '',
    application_method TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    application_start TEXT,
    application_end TEXT,
    original_url TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS postings_application_end_idx ON postings(application_end);
CREATE INDEX IF NOT EXISTS postings_institution_idx ON postings(institution_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    external_key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    official_url TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    license_code TEXT NOT NULL DEFAULT '',
    rights_status TEXT NOT NULL CHECK (
        rights_status IN ('open_document', 'authorized', 'metadata_only', 'restricted', 'review_required')
    ),
    rights_reason TEXT NOT NULL DEFAULT '',
    storage_path TEXT,
    sha256 TEXT,
    parser_status TEXT NOT NULL DEFAULT 'not_requested' CHECK (
        parser_status IN ('not_requested', 'queued', 'parsed', 'unsupported', 'failed', 'blocked_by_rights')
    ),
    extracted_text TEXT,
    collected_at TEXT,
    last_checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(posting_id, external_key)
);
CREATE INDEX IF NOT EXISTS attachments_rights_idx ON attachments(rights_status, parser_status);

CREATE TABLE IF NOT EXISTS job_profiles (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    attachment_id INTEGER REFERENCES attachments(id) ON DELETE SET NULL,
    institution_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    ncs_code TEXT NOT NULL DEFAULT '',
    ncs_path TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    duties_json TEXT NOT NULL DEFAULT '[]',
    knowledge_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '[]',
    attitudes_json TEXT NOT NULL DEFAULT '[]',
    qualifications_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    extraction_status TEXT NOT NULL DEFAULT 'metadata' CHECK (
        extraction_status IN ('metadata', 'machine_extracted', 'reviewed', 'rejected')
    ),
    rights_status TEXT NOT NULL DEFAULT 'open_metadata',
    parser_version TEXT NOT NULL DEFAULT 'metadata-v1',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(posting_id, attachment_id, job_title, ncs_code)
);
CREATE INDEX IF NOT EXISTS job_profiles_posting_idx ON job_profiles(posting_id);

CREATE VIRTUAL TABLE IF NOT EXISTS job_profiles_fts USING fts5(
    job_title,
    institution_name,
    ncs_path,
    summary,
    duties_json,
    knowledge_json,
    skills_json,
    attitudes_json,
    content='job_profiles',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS job_profiles_ai AFTER INSERT ON job_profiles BEGIN
    INSERT INTO job_profiles_fts(
        rowid, job_title, institution_name, ncs_path, summary,
        duties_json, knowledge_json, skills_json, attitudes_json
    ) VALUES (
        new.id, new.job_title, new.institution_name, new.ncs_path, new.summary,
        new.duties_json, new.knowledge_json, new.skills_json, new.attitudes_json
    );
END;
CREATE TRIGGER IF NOT EXISTS job_profiles_ad AFTER DELETE ON job_profiles BEGIN
    INSERT INTO job_profiles_fts(
        job_profiles_fts, rowid, job_title, institution_name, ncs_path, summary,
        duties_json, knowledge_json, skills_json, attitudes_json
    ) VALUES (
        'delete', old.id, old.job_title, old.institution_name, old.ncs_path, old.summary,
        old.duties_json, old.knowledge_json, old.skills_json, old.attitudes_json
    );
END;
CREATE TRIGGER IF NOT EXISTS job_profiles_au AFTER UPDATE ON job_profiles BEGIN
    INSERT INTO job_profiles_fts(
        job_profiles_fts, rowid, job_title, institution_name, ncs_path, summary,
        duties_json, knowledge_json, skills_json, attitudes_json
    ) VALUES (
        'delete', old.id, old.job_title, old.institution_name, old.ncs_path, old.summary,
        old.duties_json, old.knowledge_json, old.skills_json, old.attitudes_json
    );
    INSERT INTO job_profiles_fts(
        rowid, job_title, institution_name, ncs_path, summary,
        duties_json, knowledge_json, skills_json, attitudes_json
    ) VALUES (
        new.id, new.job_title, new.institution_name, new.ncs_path, new.summary,
        new.duties_json, new.knowledge_json, new.skills_json, new.attitudes_json
    );
END;

CREATE TABLE IF NOT EXISTS extraction_evidence (
    id INTEGER PRIMARY KEY,
    job_profile_id INTEGER NOT NULL REFERENCES job_profiles(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    page_number INTEGER,
    source_url TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS extraction_evidence_profile_idx ON extraction_evidence(job_profile_id);

CREATE TABLE IF NOT EXISTS rights_decisions (
    id INTEGER PRIMARY KEY,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO sources(
    slug, name, provider, base_url, sync_mode, default_rights, license_note, enabled
) VALUES
(
    'data-go-kr-alio',
    '공공기관 채용정보 조회서비스',
    '재정경제부·공공데이터포털',
    'https://www.data.go.kr/data/15125273/openapi.do',
    'api',
    'open_metadata',
    '공공데이터포털 표시상 이용허락범위 제한 없음. 첨부문서 권리는 문서별 별도 판정.',
    1
),
(
    'job-alio',
    'JOB-ALIO 채용공고',
    '재정경제부',
    'https://job.alio.go.kr/',
    'metadata',
    'review_required',
    '공개 열람 가능 여부와 재배포 권한은 다름. 원문 링크 우선.',
    0
),
(
    'ncs-job-description',
    'NCS 등록 직무기술서',
    '한국산업인력공단',
    'https://m.ncs.go.kr/blind/bl04/JdsptList.do',
    'metadata',
    'review_required',
    '공공누리 표시 및 제3자 권리 여부를 문서별 확인. 정책 페이지 직접 연결.',
    0
),
(
    'ncs-learning-module',
    'NCS 학습모듈',
    '한국산업인력공단',
    'https://www.ncs.go.kr/th01/TH-102-001-02.scdo',
    'manual',
    'restricted',
    '공공누리 제2유형: 상업적 이용 금지. 본 서비스 수집·재배포 대상에서 제외.',
    0
),
(
    'cleaneye-job-plus',
    'Cleaneye Job+ 지방공공기관 채용정보',
    '행정안전부',
    'https://job.cleaneye.go.kr/',
    'metadata',
    'review_required',
    '지방공기업·지방출자출연기관 범위. 공식 연계 및 사이트 이용정책 확인 후 커넥터 활성화.',
    0
);
