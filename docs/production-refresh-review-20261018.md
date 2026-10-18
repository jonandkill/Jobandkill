# PR #5 적용 검증 · 2026-10-18

## 판정

**코드 보완 후 CI를 통과하면 병합 후보로 검토할 수 있다. 운영 정상화는 확인되지 않았다.**
기본 브랜치 병합은 다음 예약 실행에 DB 초기화·수집·문서 처리를 적용하므로, 운영 대상과 설정을 확인하고 PR 브랜치에서 읽기 진단을 먼저 수행한다. 현재 검증에서는 유료 자원 생성, 운영 DB 초기화/쓰기, S3 Put/Delete, 실제 채용정보 수집을 실행하지 않았다.

## 대조한 증거

- PR: https://github.com/jonandkill/Jobandkill/pull/5
- 최초 검토 head: `80059dc773a9220779a2bf3155e8037ec515f009`
- 기본 브랜치/실패 실행 SHA: `62dd284ebab1c0ce83b387902d6efd9eaa93b570`
- 실패 실행: https://github.com/jonandkill/Jobandkill/actions/runs/39068648988
- 실패 job: `117501461956`, `Check configuration without printing secrets`, exit 2.
- 실행 생성: 2026-10-17 20:53:51 UTC = **2026-10-18 05:53:51 KST**. '10월 17일 실행'은 UTC 날짜다.
- 로그의 DB URL, S3 버킷, API URL 템플릿, API 키는 빈 문자열이다. 이는 실행에 미전달된 증거이며 설정 저장소에서 삭제되었다는 증거는 아니다.
- 구 워크플로의 해당 진단 단계에는 S3 접근키 매핑 자체가 없다. 이 로그만으로 S3 키 존재 여부/유효성을 판단할 수 없다.
- init / metadata sync / full sync / process-documents는 전부 skipped.
- PR은 검토 시 open/Draft/미병합, GitHub는 mergeable=true, 충돌 없음. 계정 `jonandkill`의 저장소 admin/push 권한은 확인했다.
- 기존 PR CI https://github.com/jonandkill/Jobandkill/actions/runs/38488639326 의 Python 3.11, Python 3.12, PostgreSQL, container 작업 모두 success.
- 기존 PostgreSQL job `115608298346` 실제 로그에서 4 tests / OK를 확인했다. PR 작성자의 로컬 4 skip 기록과 별개의 CI 실행 증거다. 운영 DB 검증을 의미하지 않는다.

## 구현 상태와 이번 보완

| 대상 | 기존 PR에 구현된 동작 | 이번 보완 / 남은 한계 |
|---|---|---|
| 필수 비밀변수 | 6개 이름별 전달 여부·고정 메시지·Summary, 누락 시 exit 2 | ASIA 임시 AWS 키의 session token 누락 검사. DB TLS 중복/빈 옵션과 URI 접속 대상 덮어쓰기, 잘못된 S3 이름·endpoint port 차단 |
| PostgreSQL | connect_timeout=10, read-only 기본 트랜잭션, SELECT 1 | SHOW transaction_read_only=on과 SELECT 결과 직접 확인. DDL/DML·스키마 호환성·운영 DB 대상은 미검증 |
| S3 | GetBucketVersioning, Enabled/Suspended 거부, session token 전달 | 연결/읽기 각 10초·최대 2회 시도. 권한거부·토큰만료·버킷없음 오류 회귀검증. Put/Delete·공개 차단·암호화 정책은 미검증 |
| 공식 API | 정확한 list URL, 서비스키 치환, 첫 페이지 1행 조회, 오류코드·redirect 차단 | 성공코드·totalCount·명시적 목록·일련번호/기관명/제목을 검증. 혼합/손상 레코드·모순된 건수·하위 레코드 totalCount 오인 차단. HTTP/전송 예외 체인 비노출. 고용유형/학력/지역/NCS의 실제 명칭 필드 매핑 추가 |
| 수동 진단 | diagnose_only=true 기본값과 쓰기 단계 if 조건 | 별도 실행 모드 해석: 누락/null/0/오타 입력은 읽기 전용. 수동 실행은 명시적 false만 쓰기 허용. 4개 변경 단계 전부 동일한 write_enabled 가드. 진단 제한 5분, checkout 인증정보 비보존 |
| 결과 구분 | 진단 성공과 동기화 성공 차이를 notice에 안내 | read_only_diagnostics_passed와 collection_performed=false를 별도 출력 |

구현되지 않은 범위: `/detail` 기반 새 첨부/전형 수집, API 운영승인 확인, 운영 DB DDL/DML, S3 Put/Delete, 전체 채용공고 수집 완료. 목록 `files=[]`는 상세 첨부의 권리 철회로 취급하지 않는다.

## 직접 수행한 검증

- 기존 PR 전체 테스트: 83개 중 79 통과, PostgreSQL 4 skip.
- 보완본 전체 테스트: **108개 중 103 통과, PostgreSQL 5 skip** (로컬 별도 테스트 DB 없음).
- 신규 안전성 회귀 테스트 24개 통과. PostgreSQL CI용 읽기 전용 실제 트랜잭션 검사 1개 추가.
- CI의 Python 테스트 작업도 production 의존성을 설치하도록 변경해 psycopg/boto3 테스트를 실제로 실행한다.
- S3는 botocore Stubber를 사용해 실제 SDK의 GetBucketVersioning 호출·오류를 검증한다. 유료 인프라나 실 버킷을 만들지 않는다.
- API redirect는 격리된 localhost HTTP 서버에 302를 반환시켜 두 번째 경로로 요청이 전달되지 않는지 확인했다. 테스트용 문자열은 외부 API에 보내지 않았다.
- 원본 preflight에 `sslmode=require&sslmode=`와 `s3://...` 버킷을 동시에 넣으면 configuration_ready=true인 문제를 합성 입력으로 재현했다. 보완본은 연결 전에 거부한다.
- 고정 오류 출력/JSON/Summary/예외 체인에 합성 sentinel 비밀값이 없는지 검증했다.
- compileall, git diff --check, 워크플로 YAML 구조 검사 통과.
- 이 결과는 코드/자동 테스트 검증이며 전면 침투 테스트나 의존성 취약점 전수 감사는 아니다.

## 실제 진단을 막고 있는 조건

1. GitHub 연결 도구는 secrets 목록/설정 관리와 새 workflow_dispatch 실행 기능을 제공하지 않는다. 저장소 계정 권한과 연결 도구 기능은 별개다.
2. 브라우저의 GitHub 설정 화면은 로그인되지 않은 상태다. 로그인 전에는 production secrets/variables의 이름·배포 보호 규칙을 확인하거나 수동 진단을 실행할 수 없다.
3. 로컬 실행 환경에는 운영 DB·S3·API 인증정보가 없다. 다른 서비스의 Render DB를 추측하여 사용하지 않는다.
4. 공공데이터 API의 개발 자동승인과 운영 심의승인은 구분한다. 키를 이용한 HTTP 성공도 계정의 운영승인 증명이 아니다.

비밀값을 채팅·커밋·워크플로 입력·로그에 넣지 않는다. 필요한 등록명은 [복구 절차](production-refresh-recovery.md)의 표를 사용한다.

## 다음 안전한 실행

설정/접근 확인 후 기존 workflow_dispatch가 정의된 `sync.yml`을 **PR 브랜치의 새 실행**으로 시작한다. 과거 실패 실행의 Re-run은 구 SHA이므로 수정 검증에 사용하지 않는다.

```bash
gh workflow run sync.yml --repo jonandkill/Jobandkill \
  --ref fix/production-refresh-diagnostics-20261014 \
  -f diagnose_only=true -f full_sync=false
```

이 명령은 실행 절차 설명이며 이번 로컬 검증에서 호출하지 않았다. 실행 후 실제 head SHA, 수동 진단 모드, 연결 항목 결과, 4개 쓰기 단계 skipped를 확인한다. 필수 설정이 누락되면 exit 2와 미전달 이름을 확인하고 연결 성공으로 보고하지 않는다.

실제 수집 검증은 운영 대상/권한·백업 및 기존 데이터 삭제를 포함할 수 있는 문서 정리 동작을 검토한 뒤 별도로 진행한다. 모든 진단이 통과해도 collection_performed=false이면 수집 성공이 아니다.

## 명세 근거

- 공공 API 공식 Swagger: https://www.data.go.kr/data/15125273/openapi.do (2026-10-18 확인: /1051000/recruitment/list, resultCode=0, result/item, totalCount, *NmLst 명칭 필드).
- GitHub 표현식·빈 값 형변환: https://docs.github.com/en/actions/reference/workflows-and-actions/expressions
- PostgreSQL URI·쿼리 매개변수: https://www.postgresql.org/docs/current/libpq-connect.html
- S3 버킷 이름: https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
