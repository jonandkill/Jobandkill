# Job&Kill 운영 연결 절차

실제 서비스키·DB 비밀번호·S3 키·SMTP 비밀번호는 채팅, Git 커밋, workflow 입력값에 넣지 않습니다. 배포 플랫폼의 비밀 저장소 또는 GitHub Environment secrets에만 설정합니다.

## 1. 운영 자원

1. 파일럿에는 Render 무료 PostgreSQL을 사용할 수 있지만, 공개 운영 전에는 관리형 백업을 지원하는 유료 PostgreSQL과 복구 절차를 준비합니다.
2. 공개 접근이 차단된 전용 AWS S3 버킷을 만들고 `documents/` prefix에 필요한 최소 권한만 부여합니다. 권리 철회 시 실제 버전까지 남지 않도록 버킷 버전 관리를 한 번도 활성화하지 않습니다. 실행 역할에는 암호화된 객체 업로드·삭제와 `GetBucketVersioning` 확인 권한이 필요합니다. 현재 어댑터는 이 API와 서버 측 암호화 계약이 다른 S3 호환 서비스까지 보장하지 않습니다.
3. HTTPS 서비스 주소와 SMTP 발신 계정을 준비합니다.
4. [공공기관 채용정보 조회서비스](https://www.data.go.kr/data/15125273/openapi.do) 활용신청·운영 승인을 완료합니다.

필수 환경변수의 이름은 `.env.example`에 있습니다. 운영에는 최소한 다음 값이 필요합니다.

- `JOBNKILL_ENV=production`
- `JOBNKILL_DATABASE_URL` (`sslmode=require` 이상, 가능하면 `verify-full`과 CA 검증)
- `JOBNKILL_STORAGE_BACKEND=s3`, `JOBNKILL_S3_BUCKET`
- `JOBNKILL_PUBLIC_URL`의 HTTPS 주소
- `JOBNKILL_AUTH_RATE_SECRET`
- `JOBNKILL_TRUSTED_PROXY_CIDRS` (배포 프록시의 실제 CIDR만 지정)
- `JOBNKILL_SMTP_HOST`, `JOBNKILL_SMTP_FROM` 및 공급자별 인증값
- `JOBNKILL_ALIO_API_URL_TEMPLATE`, `JOBNKILL_ALIO_SERVICE_KEY`

GitHub Actions에서는 이 값들을 하나의 `production` Environment에 저장합니다. workflow는 비밀값을 작업 전체에 노출하지 않고 `doctor`, `init`, `sync`, `process-documents` 중 각 값을 실제로 사용하는 단계에만 전달합니다. 동기화 단계도 권리 철회 원문을 정리할 수 있도록 S3 최소 권한을 받습니다. `JOBNKILL_S3_REGION`과 `JOBNKILL_S3_PREFIX`는 Environment variables로 설정할 수 있으며, 비어 있으면 각각 `ap-northeast-2`와 `documents`를 사용합니다.

## 2. Render Blueprint 파일럿 배포

[`render.yaml`](../render.yaml)은 `jobandkill1` 브랜치에서 Python 3.12 웹 서비스와 같은 싱가포르 리전의 PostgreSQL을 만듭니다. 아래 링크로 Blueprint를 열고 Dashboard가 요청하면 `jobandkill1` 브랜치를 선택합니다.

[Render에서 Job&Kill Blueprint 열기](https://render.com/deploy?repo=https://github.com/jonandkill/Jobandkill/tree/jobandkill1)

Blueprint 적용 화면에서 다음 `sync: false` 로그인 메일 값을 입력합니다. 실제 값은 Render Dashboard에만 넣습니다. AWS S3 키는 수집 작업에만 필요하므로 공개 웹 서비스에 주입하지 않고 아래 GitHub `production` Environment에만 저장합니다.

| 환경변수 | 입력값 |
|---|---|
| `JOBNKILL_SMTP_HOST` | SMTP 공급자 호스트 |
| `JOBNKILL_SMTP_PORT` | 무료 Render가 허용하고 공급자가 지원하는 STARTTLS 대체 포트(예: 2525) |
| `JOBNKILL_SMTP_USERNAME` | SMTP 사용자명 |
| `JOBNKILL_SMTP_PASSWORD` | SMTP 비밀번호 |
| `JOBNKILL_SMTP_FROM` | 검증된 발신 주소 |

Render는 웹 서비스에 내부 PostgreSQL 연결 문자열과 공개 `onrender.com` HTTPS 주소를 주입합니다. 앱은 `RENDER_EXTERNAL_URL`을 매직링크 기준 주소로 사용하며 `JOBNKILL_AUTH_RATE_SECRET`은 Blueprint가 생성합니다. 따라서 이 세 값은 복사해서 입력하지 않습니다.

무료 웹 서비스는 SMTP 25/465/587 포트로 외부 연결할 수 없습니다. 공급자가 2525 같은 STARTTLS 대체 포트를 제공하지 않으면 현재 SMTP 방식 로그인은 무료 웹 서비스에서 동작하지 않으므로, 유료 인스턴스 또는 별도 HTTPS 메일 API 구현이 필요합니다.

웹 서비스는 Render 내부 DB 주소를 사용하지만 GitHub Actions 동기화 작업은 Render 밖에서 실행됩니다. GitHub `production` Environment의 `JOBNKILL_DATABASE_URL`에는 Render 외부 DB 주소와 `sslmode=require` 이상을 설정해야 합니다. 공식 API의 `JOBNKILL_ALIO_API_URL_TEMPLATE`, `JOBNKILL_ALIO_SERVICE_KEY`도 Render 웹 서비스가 아니라 같은 GitHub Environment에 저장합니다.

| GitHub Environment 종류 | 이름 |
|---|---|
| Secret | `JOBNKILL_DATABASE_URL` |
| Secret | `JOBNKILL_ALIO_API_URL_TEMPLATE` |
| Secret | `JOBNKILL_ALIO_SERVICE_KEY` |
| Secret | `JOBNKILL_S3_BUCKET` |
| Secret | `JOBNKILL_S3_ACCESS_KEY_ID` |
| Secret | `JOBNKILL_S3_SECRET_ACCESS_KEY` |
| 선택 Secret | `JOBNKILL_S3_ENDPOINT_URL` |
| Variable | `JOBNKILL_S3_REGION` |
| Variable | `JOBNKILL_S3_PREFIX` |

GitHub의 `JOBNKILL_S3_ACCESS_KEY_ID`와 `JOBNKILL_S3_SECRET_ACCESS_KEY`에는 수집 작업 전용 최소권한 IAM 자격증명을 저장합니다. 이 자격증명은 Render 웹 서비스에 복제하지 않습니다.

이 구성은 비용 승인을 요구하지 않는 파일럿 기본값입니다. 무료 PostgreSQL은 30일 후 만료되고 관리형 백업이 없으며, GitHub Actions 연결을 위해 외부 DB 주소를 사용할 때는 강한 자동 생성 자격증명과 TLS에 의존합니다. 실제 운영 전에는 유료 DB와 복구 시험을 준비하고, 동기화 작업을 같은 Render 사설망으로 옮기거나 승인된 외부 IP만 허용하도록 네트워크 경계를 확정합니다.

## 3. 배포 전 점검

```bash
python -m pip install -r requirements-production.txt
python -m jobandkill doctor --production --require-api
python -m jobandkill init
python -m jobandkill status
```

`doctor`는 값 자체를 출력하지 않고 구성 여부만 표시합니다. `init`도 PostgreSQL DSN을 출력하지 않습니다.

## 4. 최초 전체 동기화

GitHub의 단일 `production` Environment에 필요한 secrets와 variables를 설정하고 보호 규칙에 승인자를 지정합니다. 최초 전체 동기화도 별도 Environment로 전환하지 않으므로 같은 운영 비밀값과 보호 규칙을 사용합니다. **Production data refresh** workflow를 수동 실행하고 `full_sync`를 선택합니다. 이 보호 규칙은 수동 실행과 예약 실행 모두에 적용되므로, 예약 갱신을 무인 실행해야 한다면 GitHub의 배포 보호 정책에 맞는 별도 승인 방식을 먼저 설계해야 합니다. CLI로 실행할 때는 다음과 같습니다.

```bash
python -m jobandkill sync --source data-go-kr-alio --full
```

결과에서 다음을 확인합니다.

- `status`가 `success`
- `truncated`가 `false`
- `attachment_errors`가 `0`
- `cleanup_failed`, `cleanup_due`, `cleanup_pending_attachments`가 모두 `0`
- `reported_total`과 `seen`이 일치
- `created + updated + unchanged + skipped`가 `seen`과 일치

`partial`, `truncated: true`, 비정상 첨부 항목 또는 정리 대기열이 하나라도 있으면 전체 수집 완료로 표시하지 말고 운영 승인량·페이지 크기·실패 레코드·객체 삭제 로그를 확인한 뒤 다시 실행합니다. `sync` 명령은 모든 페이지의 메타데이터와 권리 철회를 가능한 범위까지 반영한 후 종료 코드 `3`을 반환합니다. upsert 방식이므로 재실행해도 동일 공고가 중복 저장되지 않습니다.

## 5. 문서 권리와 저장

첨부문서는 기본적으로 `review_required`이며 다운로드하지 않습니다. 문서별 공공누리 표시 또는 별도 이용허가를 확인한 뒤 CLI로 판정 이력을 남깁니다.

```bash
python -m jobandkill rights-info 42
python -m jobandkill rights 42 authorized \
  --reason '기관의 2026-09-01 서면 허가 확인' \
  --by 'content-admin@example.com' \
  --expected-identity 'rights-info가 표시한 identity 값'
python -m jobandkill process-documents --limit 50 --fail-on-error
```

허용 판정 전 `rights-info`에 표시된 현재 공식 URL·제목·라이선스·원문 해시와 권리/피드 리비전을 증빙과 대조합니다. `identity`는 이 값들의 해시이며 허용 트랜잭션의 비교 조건으로 사용됩니다. 검토 이후 첨부가 교체·제거되거나 더 최신 제한 판정이 기록되면 허용 명령이 거부되므로 현재 상태를 다시 검토해야 합니다. 제한·철회는 안전을 위해 `identity` 없이도 즉시 적용됩니다.

`--fail-on-error`를 사용하면 문서 추출 실패, 객체 삭제 재시도 실패, 배치 한도를 넘긴 즉시 처리 대기열, 검증 불가능한 이전 객체 격리가 하나 이상일 때 종료 코드 `4`를 반환하므로 자동화가 불완전한 정리를 성공으로 기록하지 않습니다. `unsupported` 문서는 별도 검토 대상으로 집계되며 이 옵션의 실패 조건에는 포함되지 않습니다.

S3 객체는 원래 파일명이 아닌 `documents/{sha256 앞 2자}/{sha256}.확장자`로 저장되고 서버 측 암호화를 요청합니다. 업로드 의도와 삭제 작업은 데이터베이스에 먼저 기록되며, 업로드 의도에는 첨부 ID와 작업 토큰이 포함됩니다. 따라서 업로드와 권리 철회가 겹쳐도 철회 작업이 대상 객체를 기다렸다가 정리하며, 일시적 실패는 지수 백오프로 재시도됩니다. 권리를 제한 상태로 되돌리면 추출 텍스트·파생 프로필을 먼저 차단하고 원문 객체를 정리합니다. 동일 URL도 `JOBNKILL_DOCUMENT_RECHECK_DAYS`(기본 7일)마다 해시를 다시 확인하고, 기존 별도 승인 원문과 해시가 다르면 파싱 전에 차단합니다.

`JOBNKILL_DOCUMENT_DIR`, S3 버킷 또는 엔드포인트는 최초 객체 저장 시 데이터베이스에 고정됩니다. 값을 임의로 바꾸면 삭제가 성공한 것처럼 오판하지 않고 작업이 실패합니다. 저장소 이전은 기존 위치에서 대기 중 삭제를 모두 완료하고 별도의 검증된 마이그레이션 절차로 수행합니다.

[NCS 저작권정책](https://www.ncs.go.kr/unity/th01/selectPolicyPopView.do)에 따라 상업 이용이 제한된 NCS 학습모듈은 수집하지 않습니다. NCS 등록 직무기술서도 문서별 권리를 확인하기 전에는 메타데이터와 공식 링크만 유지합니다.

## 6. 운영 검증

- PostgreSQL 백업을 별도 환경에 실제 복원
- S3 공개 접근 차단, 암호화, 삭제 권한 확인
- S3 버전 관리 비활성화 및 `GetBucketVersioning` 확인 권한 검증
- 실제 매직링크 메일 1건 발송과 만료 전 로그인 확인
- 매직링크 만료·재사용 차단, 로그아웃, CSRF 거부 확인
- 같은 초안을 두 탭에서 수정했을 때 `409` 충돌과 사용자 선택 확인
- 전체 건수와 공식 API `totalCount` 대조
- 320px, 390px, 768px, 1440px에서 로그인·직무 검색·8단계 작성·결과 모달 시각 QA
- 스캔 PDF와 바이너리 HWP는 자동 공개하지 않고 별도 검토 큐로 분리
- `document_gc_queue`의 실패·재시도 건수와 `quarantined` 건수 0 확인

현재 일일 workflow는 공식 API의 앞부분을 멱등 재조회하는 갱신입니다. 공식 API가 수정일 커서의 의미와 정렬 안정성을 문서로 보장하기 전에는 이를 완전한 델타 동기화라고 부르지 않습니다.
