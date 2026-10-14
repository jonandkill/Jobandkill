# Production data refresh 복구 기록 · 2026-10-14

## 확인한 장애

- 저장소: jonandkill/Jobandkill, 기본 브랜치 jobandkill1.
- 조사 기준 커밋: `62dd284ebab1c0ce83b387902d6efd9eaa93b570`.
- 사용자 제시 실행: https://github.com/jonandkill/Jobandkill/actions/runs/38329700577
- 추가 확인 실행: https://github.com/jonandkill/Jobandkill/actions/runs/38482028419 (2026-10-14 08:05 KST 시작).
- 두 실행 모두 requirements-production.txt 설치 성공, doctor 단계 exit 2, init/sync/process-documents 미실행.
- 전달되지 않은 값: JOBNKILL_DATABASE_URL, JOBNKILL_S3_BUCKET, JOBNKILL_ALIO_API_URL_TEMPLATE, JOBNKILL_ALIO_SERVICE_KEY.
- 이는 DB 접속 오류·S3 장애·API 승인 거절 응답이 아니라 **연결을 시도하기 전 설정 누락**이다.
- GitHub 목록에서 최신 30건의 Production data refresh가 모두 failure이며 같은 커밋이다. 개별 로그 대조는 위 2건에 한정한다.
- secrets 표현식은 미설정/사용 불가일 때 빈 문자열이다. 로그는 미전달 사실만 증명한다. 미등록, 다른 Environment, Variables/Dependabot secrets에 잘못 등록, 접근 범위 문제를 구별하려면 설정 목록을 확인해야 한다.
- 현재 GitHub 연결 도구는 secrets 관리 API를 지원하지 않는다. 계정의 저장소 admin/push 권한과 도구의 secrets 관리 능력은 별개다.
- Render 도구는 workspace 선택 확인을 요구한다. 기존 jobnkill-susi-db와 이 서비스의 DB가 같다는 근거는 확보하지 못했다.

## 필수 등록 위치와 정확한 이름

[GitHub Settings → Environments](https://github.com/jonandkill/Jobandkill/settings/environments) → **production** → Environment secrets / Environment variables.
Repository Actions secrets도 접근 가능하면 사용할 수 있으나 운영 값은 production에 모으는 것을 기준으로 한다. 같은 이름의 상위 범위 값이 있다면 우선순위를 확인한다. Render 환경변수는 GitHub로 자동 복사되지 않는다.

| 구분 | GitHub 이름 | 종류 | 필요한 실제 값 / 조건 |
|---|---|---|---|
| PostgreSQL | JOBNKILL_DATABASE_URL | Secret, 필수 | 이 공공채용 서비스 전용 운영 DB의 외부 접속 URL. postgresql:// 또는 postgres://. sslmode=require 이상. 가능하면 verify-full+CA. GitHub runner의 네트워크 접근·DB 만료·계정 권한 확인 |
| S3 | JOBNKILL_S3_BUCKET | Secret, 필수 | 공개 접근 차단된 전용 버킷 이름 (s3:// URL 아님) |
| S3 | JOBNKILL_S3_ACCESS_KEY_ID | Secret, 필수 | 전용 IAM 접근키. 실행 시 AWS_ACCESS_KEY_ID로 전달 |
| S3 | JOBNKILL_S3_SECRET_ACCESS_KEY | Secret, 필수 | 전용 IAM 비밀키. 실행 시 AWS_SECRET_ACCESS_KEY로 전달 |
| S3 | JOBNKILL_S3_SESSION_TOKEN | Secret, 임시 자격증명 사용 시 | 수정본에서 AWS_SESSION_TOKEN으로 전달. STS 키는 만료 전에 갱신 필요 |
| S3 | JOBNKILL_S3_ENDPOINT_URL | Secret, 선택 | AWS S3 기본 엔드포인트 사용 시 비워둠. 호환 저장소는 HTTPS와 GetBucketVersioning/SSE 계약 확인 |
| S3 | JOBNKILL_S3_REGION | Variable | 실제 버킷 리전. 미입력 기본 ap-northeast-2 |
| S3 | JOBNKILL_S3_PREFIX | Variable | 실제 문서 prefix. 미입력 기본 documents. 기존 운영 저장 위치를 임의 변경하지 않음 |
| 공공 API | JOBNKILL_ALIO_API_URL_TEMPLATE | Secret, 필수 | 아래 공식 명세 기반 템플릿. 실제 인증키를 이 URL에 직접 삽입하지 않음 |
| 공공 API | JOBNKILL_ALIO_SERVICE_KEY | Secret, 필수 | 해당 서비스에 승인된 실제 서비스키. 운영 승인·유효기간·쿼터 별도 확인 |

JOBNKILL_ENV=production과 JOBNKILL_STORAGE_BACKEND=s3는 workflow가 지정한다. SMTP·로그인·공개 웹 URL은 이 collector 작업의 필수값이 아니다.

S3 최소권한은 버킷의 `s3:GetBucketVersioning`, 대상 prefix의 `s3:PutObject`, `s3:DeleteObject`이다. 조직 정책/KMS 사용 시 추가 권한을 검토한다. 현재 어댑터는 버전 관리가 Enabled 또는 Suspended인 버킷을 거부하므로 한 번도 버전 관리를 켜지 않은 전용 버킷이 필요하다. 기존 다른 서비스의 버킷 설정을 변경하지 않는다. 읽기 진단은 Put/Delete, 공개 차단, 암호화 정책 전체를 검증하지 않는다.

## 공식 API 명세와 승인 상태

출처: https://www.data.go.kr/data/15125273/openapi.do

2026-10-14 공식 페이지의 내장 Swagger 명세를 직접 확인했다. host는 apis.data.go.kr/1051000/recruitment, 목록 operation은 /list, 매개변수는 serviceKey, pageNo, numOfRows, resultType이다.

```text
https://apis.data.go.kr/1051000/recruitment/list?serviceKey={service_key}&pageNo={page}&numOfRows={page_size}&resultType=json
```

기존 .env.example/README의 생략 주소와 type=json은 그대로 운영에 쓰지 않는다. 위 주소는 공식 명세에 근거한 설정이며 **승인키를 사용한 실응답 검증 완료를 뜻하지 않는다**.

공식 페이지: 개발단계 자동승인 / 운영단계 심의승인, 개발계정 트래픽 1,000, 운영계정은 활용사례 등록 후 증설 신청 가능. 키가 존재하거나 HTTP 조회가 성공한다고 운영승인이 증명되지는 않는다. 계정의 활용신청 상세에서 서비스 15125273, 운영 승인 상태, 이용기간, 트래픽을 확인해야 한다. 현재 계정의 승인 여부는 미확인이다.

현재 /list는 files와 steps를 빈 배열로 제공하며 /detail에서 별도 조회하도록 명시한다. 이번 수정은 목록 갱신 시 이 빈 files를 권리 철회/삭제로 처리하지 않는다. **상세 첨부 수집 신규 구현은 이번 복구 범위에 포함하지 않는다.** 문서 처리 단계가 0건이어도 첨부 수집 완료로 보고하지 않는다.

## 코드로 해결한 문제

1. `python -m jobandkill.preflight --check-connections --summary`: 여섯 필수 secret의 미전달 이름, DB TLS/외부 호스트 형식, 정확한 API operation/매개변수, S3 형식·드라이버를 점검한다.
2. 모든 정적 설정이 통과한 경우에만 PostgreSQL SELECT 1(읽기 전용), S3 GetBucketVersioning, 공식 API 첫 페이지 1행 조회를 수행한다. DB 초기화/쓰기와 S3 Put/Delete는 수행하지 않는다. API 조회는 호출량을 사용하며 재시도 시 최대 3회 요청할 수 있다.
3. 외부 오류 본문·예외 문자열·DSN·키·완성 API URL을 출력하지 않고 고정 진단문과 boolean만 출력한다. GitHub Summary에도 같은 정보만 기록한다.
4. HTTP 200 안의 JSON/XML 오류 응답, 구조 불명 빈 응답을 실패로 처리한다. 공식 result/item 래퍼를 해제하고 목록 files=[]의 삭제 오인을 막는다.
5. API 리다이렉트는 차단한다. 다른 호스트로 서비스키가 전달되지 않게 한다.
6. 수동 실행에 diagnose_only=true 기본값을 추가한다. true면 init/sync/process-documents를 모두 건너뛴다. 예약 실행의 실제 갱신 동작과 동시실행 잠금은 유지한다.
7. preflight에도 S3 키·리전·endpoint를 전달하고 임시 AWS 자격증명용 session token 전달을 추가한다.

미등록 인증정보의 생성/복구, 계정의 공식 API 운영승인, 실제 DB·버킷 선택은 코드 수정으로 대체할 수 없다. 기존 미병합 PR #1의 대규모 기능 변경은 병합하거나 수정하지 않는다.

## 재현 방법 (인증값 없이)

```bash
env -u DATABASE_URL JOBNKILL_ENV=production JOBNKILL_DATABASE_URL='' JOBNKILL_STORAGE_BACKEND=s3 JOBNKILL_S3_BUCKET='' JOBNKILL_ALIO_API_URL_TEMPLATE='' JOBNKILL_ALIO_SERVICE_KEY='' python -m jobandkill doctor --production --require-api --collector-only
```

실제 로컬 재현: database=false, document_storage=false, s3_driver=true, official_api=false, exit 2. 운영 DB/S3/API 호출 없음.

## 안전한 복구 순서

1. 실제 사용할 공공채용 운영 DB와 버킷을 확정한다. 수시 서비스 DB를 추측해 사용하지 않는다. DB 백업/대상 스키마 및 외부 접속 정책을 확인한다.
2. 위 6개 필수 Secret과 실제 리전/prefix를 production에 등록한다. 승인키는 채팅이나 workflow 입력에 넣지 않는다. 등록 목록만 볼 때는 `gh secret list --repo jonandkill/Jobandkill --env production`를 사용한다. 값을 표시하지 않는다.
3. 공식 포털에서 운영승인·유효기간·쿼터를 확인한다. 준비되지 않았다면 스케줄을 임시 Disable workflow 할 수 있으나 복구 성공으로 기록하지 않는다. 이번 작업은 기존 스케줄을 임의 중지하지 않는다.
4. 수정본 CI 확인 후 jobandkill1에 반영한다. Actions → Production data refresh → Run workflow → jobandkill1 → diagnose_only=true, full_sync=false로 새 실행을 만든다. 과거 실패 실행의 Re-run은 과거 SHA를 사용하므로 수정 코드 검증에는 쓰지 않는다.
5. 진단 Summary의 모든 항목이 통과했는지 확인한다. 이 단계의 녹색은 읽기 진단 성공일 뿐 실제 갱신 완료가 아니다. S3 Put/Delete와 DB init에 필요한 DDL/수집 DML 권한, API 실제 필드·건수를 검토한다.
6. diagnose_only=false, full_sync=false로 제한된 갱신을 실행한다. 최대 10페이지인 기존 기본값과 API 쿼터를 고려한다. 데이터가 10페이지를 넘으면 partial/exit 3이 될 수 있으며 완전 수집으로 표시하지 않는다.
7. 범위·쿼터를 확인한 후 필요한 경우에만 full_sync=true로 최초 전체 수집한다 (기본 최대 1,000페이지). 기존 로직은 페이지 checkpoint 재개가 아니라 앞에서부터 멱등 재조회한다. 모든 일일 실행을 완전한 델타 수집이라고 부르지 않는다.
8. success, truncated=false, attachment_errors=0, cleanup_failed/due/pending_attachments=0, reported_total와 seen 및 처리 건수 합계를 대조한다. process-documents의 실패/정리/격리 건수도 확인한다. 다음 예약 실행까지 성공해야 반복 실패 복구 완료다.

## 검증 한계

자동 테스트는 외부 호출을 mock 처리한 오류/정상 응답 fixture이다. 가짜 자격증명을 운영 설정에 등록하거나 네트워크로 전송하지 않았다. 운영 인증정보가 없어 실 DB/S3/API 통합 실행은 검증하지 않았다. PostgreSQL 통합 테스트는 별도 테스트 DB가 없으면 건너뛴다.

참고:
- https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs
