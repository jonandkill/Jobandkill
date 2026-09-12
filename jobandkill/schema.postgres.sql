CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    sync_mode TEXT NOT NULL CHECK (sync_mode IN ('api', 'metadata', 'manual')),
    default_rights TEXT NOT NULL CHECK (
        default_rights IN ('open_metadata', 'open_document', 'metadata_only', 'restricted', 'review_required')
    ),
    license_note TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_success_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- National occupational standards are a reference catalog, never fake job ads.
CREATE TABLE IF NOT EXISTS occupation_catalog (
    id TEXT PRIMARY KEY,
    source_slug TEXT NOT NULL,
    external_key TEXT NOT NULL,
    standard_code TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    job_title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    ncs_path TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT '',
    training_hours TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    source_updated_at TEXT NOT NULL DEFAULT '',
    collected_at TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_run_id TEXT NOT NULL DEFAULT '',
    UNIQUE(source_slug, external_key)
);
CREATE INDEX IF NOT EXISTS occupation_catalog_source_idx
    ON occupation_catalog(source_slug, standard_code);
CREATE INDEX IF NOT EXISTS occupation_catalog_seen_idx
    ON occupation_catalog(last_seen_run_id);

CREATE TABLE IF NOT EXISTS catalog_sync_runs (
    id TEXT PRIMARY KEY,
    source_slug TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('api', 'import')),
    status TEXT NOT NULL CHECK (status IN ('running', 'partial', 'completed', 'failed', 'imported')),
    next_page INTEGER NOT NULL DEFAULT 1,
    reported_total INTEGER,
    records_seen INTEGER NOT NULL DEFAULT 0,
    unique_count INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    source_file_hash TEXT NOT NULL DEFAULT '',
    error_summary TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS catalog_sync_source_idx
    ON catalog_sync_runs(source_slug, mode, started_at);

CREATE TABLE IF NOT EXISTS catalog_sync_pages (
    run_id TEXT NOT NULL REFERENCES catalog_sync_runs(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    page_hash TEXT NOT NULL,
    records_seen INTEGER NOT NULL,
    PRIMARY KEY(run_id, page_no),
    UNIQUE(run_id, page_hash)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_created INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    attachments_seen INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS institutions (
    id BIGSERIAL PRIMARY KEY,
    alio_code TEXT UNIQUE,
    name TEXT NOT NULL,
    institution_type TEXT NOT NULL DEFAULT '',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS institutions_name_unique
    ON institutions(name) WHERE alio_code IS NULL OR alio_code = '';

CREATE TABLE IF NOT EXISTS postings (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    institution_id BIGINT NOT NULL REFERENCES institutions(id),
    title TEXT NOT NULL,
    employment_type TEXT NOT NULL DEFAULT '',
    hiring_type TEXT NOT NULL DEFAULT '',
    education TEXT NOT NULL DEFAULT '',
    regions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ncs_categories_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    positions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    headcount_text TEXT NOT NULL DEFAULT '',
    qualifications TEXT NOT NULL DEFAULT '',
    preferences TEXT NOT NULL DEFAULT '',
    selection_process TEXT NOT NULL DEFAULT '',
    application_method TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    application_start TEXT,
    application_end TEXT,
    original_url TEXT NOT NULL,
    raw_json JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS postings_application_end_idx ON postings(application_end);
CREATE INDEX IF NOT EXISTS postings_institution_idx ON postings(institution_id);

CREATE TABLE IF NOT EXISTS attachments (
    id BIGSERIAL PRIMARY KEY,
    posting_id BIGINT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
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
    rights_revision BIGINT NOT NULL DEFAULT 0,
    storage_path TEXT,
    storage_backend TEXT NOT NULL DEFAULT '',
    storage_key TEXT,
    sha256 TEXT,
    parser_status TEXT NOT NULL DEFAULT 'not_requested' CHECK (
        parser_status IN ('not_requested', 'queued', 'parsed', 'unsupported', 'failed', 'blocked_by_rights')
    ),
    extracted_text TEXT,
    collected_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    processing_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(posting_id, external_key)
);
CREATE INDEX IF NOT EXISTS attachments_rights_idx ON attachments(rights_status, parser_status);

CREATE TABLE IF NOT EXISTS document_objects (
    storage_backend TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('staging', 'ready', 'delete_pending')),
    upload_attachment_id BIGINT,
    upload_claim_token TEXT,
    upload_started_at TIMESTAMPTZ,
    ready_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(storage_backend, storage_key)
);
CREATE INDEX IF NOT EXISTS document_objects_state_idx ON document_objects(state, updated_at);

CREATE TABLE IF NOT EXISTS document_gc_queue (
    storage_backend TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(storage_backend, storage_key)
);
CREATE INDEX IF NOT EXISTS document_gc_due_idx ON document_gc_queue(next_attempt_at, updated_at);

CREATE TABLE IF NOT EXISTS storage_namespaces (
    storage_backend TEXT PRIMARY KEY,
    location_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_profiles (
    id BIGSERIAL PRIMARY KEY,
    posting_id BIGINT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    attachment_id BIGINT REFERENCES attachments(id) ON DELETE CASCADE,
    institution_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    ncs_code TEXT NOT NULL DEFAULT '',
    ncs_path TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    duties_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    knowledge_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    skills_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    attitudes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    qualifications_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    keywords_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    extraction_status TEXT NOT NULL DEFAULT 'metadata' CHECK (
        extraction_status IN ('metadata', 'machine_extracted', 'reviewed', 'rejected')
    ),
    rights_status TEXT NOT NULL DEFAULT 'open_metadata',
    parser_version TEXT NOT NULL DEFAULT 'metadata-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(posting_id, attachment_id, job_title, ncs_code)
);
CREATE INDEX IF NOT EXISTS job_profiles_posting_idx ON job_profiles(posting_id);
CREATE UNIQUE INDEX IF NOT EXISTS job_profiles_metadata_unique
    ON job_profiles(posting_id, job_title, ncs_code) WHERE attachment_id IS NULL;
CREATE INDEX IF NOT EXISTS job_profiles_search_idx ON job_profiles USING GIN (
    to_tsvector('simple',
        COALESCE(job_title, '') || ' ' || COALESCE(institution_name, '') || ' ' ||
        COALESCE(ncs_path, '') || ' ' || COALESCE(summary, '') || ' ' ||
        COALESCE(duties_json::text, '') || ' ' || COALESCE(skills_json::text, '')
    )
);

CREATE TABLE IF NOT EXISTS extraction_evidence (
    id BIGSERIAL PRIMARY KEY,
    job_profile_id BIGINT NOT NULL REFERENCES job_profiles(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    page_number INTEGER,
    source_url TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS extraction_evidence_profile_idx ON extraction_evidence(job_profile_id);

CREATE TABLE IF NOT EXISTS rights_decisions (
    id BIGSERIAL PRIMARY KEY,
    attachment_id BIGINT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    email_verified_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_consents (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    consent_type TEXT NOT NULL CHECK (consent_type IN ('privacy_policy', 'overseas_transfer')),
    policy_version TEXT NOT NULL,
    notice_url TEXT NOT NULL,
    notice_sha256 TEXT NOT NULL,
    request_token_hash TEXT NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS user_consents_user_accepted_idx ON user_consents(user_id, accepted_at);
CREATE INDEX IF NOT EXISTS user_consents_request_idx ON user_consents(request_token_hash);
CREATE INDEX IF NOT EXISTS user_consents_pending_idx ON user_consents(verified_at, accepted_at);

CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    request_subject_hash TEXT NOT NULL DEFAULT '',
    intent_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS login_tokens_expiry_idx ON login_tokens(expires_at);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    idle_expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS user_drafts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0 CHECK (current_step BETWEEN 0 AND 7),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, client_key)
);
CREATE INDEX IF NOT EXISTS user_drafts_user_updated_idx ON user_drafts(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS auth_request_events (
    id BIGSERIAL PRIMARY KEY,
    subject_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_request_events_subject_idx ON auth_request_events(subject_hash, created_at);

INSERT INTO sources(slug, name, provider, base_url, sync_mode, default_rights, license_note, enabled)
VALUES
('data-go-kr-alio', '공공기관 채용정보 조회서비스', '재정경제부·공공데이터포털',
 'https://www.data.go.kr/data/15125273/openapi.do', 'api', 'open_metadata',
 '공공데이터포털 표시상 이용허락범위 제한 없음. 첨부문서 권리는 문서별 별도 판정.', TRUE),
('job-alio', 'JOB-ALIO 채용공고', '재정경제부', 'https://job.alio.go.kr/', 'metadata', 'review_required',
 '공개 열람 가능 여부와 재배포 권한은 다름. 원문 링크 우선.', FALSE),
('ncs-job-description', 'NCS 등록 직무기술서', '한국산업인력공단',
 'https://m.ncs.go.kr/blind/bl04/JdsptList.do', 'metadata', 'review_required',
 '공공누리 표시 및 제3자 권리 여부를 문서별 확인. 정책 페이지 직접 연결.', FALSE),
('ncs-learning-module', 'NCS 학습모듈', '한국산업인력공단',
 'https://www.ncs.go.kr/th01/TH-102-001-02.scdo', 'manual', 'restricted',
 '공공누리 제2유형: 상업적 이용 금지. 본 서비스 수집·재배포 대상에서 제외.', FALSE),
('cleaneye-job-plus', 'Cleaneye Job+ 지방공공기관 채용정보', '행정안전부',
 'https://job.cleaneye.go.kr/', 'metadata', 'review_required',
 '지방공기업·지방출자출연기관 범위. 공식 연계 및 사이트 이용정책 확인 후 커넥터 활성화.', FALSE)
ON CONFLICT(slug) DO NOTHING;
