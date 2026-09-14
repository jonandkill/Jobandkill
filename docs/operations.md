# Job&Kill 운영 연결 절차

## 1. 고객 배포 상태와 결정 게이트

Blueprint는 운영용 자원 구성을 정의하지만, **실제 고객 배포는 아직 보류**입니다. 공개 또는 실제 고객 데이터 처리 전에 아래 항목의 결정·승인 기록이 있어야 합니다.

- 싱가포르 리전과 S3 저장 위치가 데이터 레지던시 요구사항에 맞는지, 그리고 유료 Render 자원 및 메일·저장소 비용을 누가 승인·부담하는지
- 대한민국 개인정보 관련 국외 이전 적용 여부와 필요한 고지·동의 또는 다른 적법 근거, 이전 국가·수탁자·이전 항목·보유기간·보호조치
- Render, Resend, AWS 등 각 처리자의 DPA, 운영자/수탁자 역할, 접근 통제, 사고 대응, 보관·파기 책임
- PostgreSQL 백업·복구 검증, S3 비공개·암호화·삭제 정책, 첨부문서 권리 검토

이 문서는 위 법률·계약·정책의 사실을 확정하거나 대신하지 않습니다. 확인되지 않은 한국 국외 이전 정책, DPA 내용, 운영자 책임 또는 제공자 처리 위치를 추정해 기재하지 않습니다. 현재 운영 중인 공개 URL도 주장하지 않습니다.

실제 비밀값은 채팅, Git 커밋, CI 변수 또는 문서에 적지 않습니다. 각 값은 발급한 공급자의 Dashboard와 해당 Render 서비스의 비밀 환경변수 화면에만 입력합니다.

## 2. 현재 배포 토폴로지

[`render.yaml`](../render.yaml)은 `jobandkill1` 브랜치에서 Web과 두 Cron 서비스를 정의합니다. PostgreSQL은 역할 URL을 먼저 만들 수 있도록 Dashboard에서 별도로 선생성합니다.

| 자원 | 구성 | 연결·역할 |
|---|---|---|
| Web | `1c-2g` | 공개 HTTPS 앱, 매직링크 로그인 |
| PostgreSQL | `0.5c-1g`, 5GB, 자동 스토리지 확장 | 수동 선생성, 사설 연결, 생성 직후 외부 허용목록 잠금 필수 |
| 수집 Cron | `0.5c-512mb`, `35 18 * * *` | 매일 18:35 UTC에 공고 동기화·허용 문서 처리 |
| 개인정보 파기 Cron | `0.5c-512mb`, `20 18 * * *` | 매일 18:20 UTC에 만료된 초안·인증자료 정리 |

세 실행 서비스는 각각 `jobandkill_web`, `jobandkill_collector`, `jobandkill_cleanup` 역할의 서로 다른 비밀 사설 URL을 사용합니다. 수집 역할에는 회원·동의·로그인·세션·초안 테이블 권한이 없고, 파기 역할은 초안 본문이나 이메일을 읽을 수 없습니다. 데이터베이스 소유자 URL은 어떤 런타임 서비스에도 저장하지 않습니다. Blueprint는 새 이름의 `JOBNKILL_WEB_DATABASE_URL`, `JOBNKILL_COLLECTOR_DATABASE_URL`, `JOBNKILL_CLEANUP_DATABASE_URL`만 받으며, 시작 래퍼가 선택한 URL 하나만 애플리케이션에 전달합니다.

## 3. Render 설정 순서

1. 고객 배포 게이트를 승인한 뒤 `앤킬's workspace`의 Render Dashboard에서 PostgreSQL 16을 먼저 별도로 만듭니다. 지역 `Singapore`, 요금제 `0.5 CPU / 1 GB`, 디스크 5GB와 자동 확장을 선택하고, 이름 `jobandkill-db`, 데이터베이스 이름 `jobandkill`, 소유자 사용자명 `jobandkill`을 정확히 설정합니다. 운영 초기화는 이 소유자 사용자명을 검증하므로 다른 이름을 사용하면 안전하게 중단됩니다. Render의 새 PostgreSQL은 외부 허용목록이 기본 `0.0.0.0/0`일 수 있으므로, **최초 외부 연결 전에** 이 항목을 삭제하고 신뢰하는 관리자 단말의 현재 공인 IPv4 한 개(`/32`)로 교체합니다. 아직 Web이나 Cron을 만들지 않습니다.

2. DB 허용목록에 관리자 `/32`만 있고 `0.0.0.0/0`이나 다른 범위가 없음을 다시 확인합니다. Render의 외부 **소유자** URL을 채팅·Git·명령행 인수에 남기지 말고 일회성 비밀 환경변수로 주입한 다음, 저장소 코드와 정확히 같은 버전에서 관리자 초기화와 권한 설정을 실행합니다.

   ```bash
   JOBNKILL_ENV=production JOBNKILL_DATABASE_ROLE=admin \
     JOBNKILL_AUTO_MIGRATE=0 python -m jobandkill init
   psql --no-psqlrc --set ON_ERROR_STOP=1 \
     --file scripts/provision_database_roles.sql
   psql --no-psqlrc
   # 열린 psql 프롬프트에서 아래 메타명령을 하나씩 실행하고 각기 다른 비밀번호를 입력합니다.
   \password jobandkill_web
   \password jobandkill_collector
   \password jobandkill_cleanup
   \q
   ```

   첫 명령에는 `JOBNKILL_DATABASE_URL` 소유자 URL이 필요하고, 두 psql 명령에는 같은 DB의 소유자 연결 환경이 필요합니다. 역할 ACL 스크립트는 비밀번호를 받거나 변경하지 않습니다. 실제 비밀번호는 평문 `ALTER ROLE` SQL 대신 PostgreSQL이 권장하는 대화형 `\password`로만 설정해 명령 기록과 서버 SQL 로그에 남지 않게 하고, 세 값을 비밀 관리자에 보관합니다. 작업 직후 DB 외부 허용목록을 빈 목록으로 바꿔 외부 접속을 완전히 차단합니다. 모든 스키마 변경 배포에서는 `init`과 권한 스크립트를 다시 실행하되, 비밀번호 회전이 필요할 때만 `\password`를 다시 실행합니다.

3. Render의 사설 DB 호스트·데이터베이스 이름과 각 전용 역할로 서로 다른 PostgreSQL URL 세 개를 만듭니다. 비밀번호에 예약문자가 있으면 사용자명·비밀번호를 URI percent-encoding 하고, 세 URL 모두 `sslmode=require`를 넣습니다.

4. 아래 링크에서 Web·두 Cron을 생성합니다. Web에는 `jobandkill_web` URL을 `JOBNKILL_WEB_DATABASE_URL`, 수집 Cron에는 `jobandkill_collector` URL을 `JOBNKILL_COLLECTOR_DATABASE_URL`, 개인정보 파기 Cron에는 `jobandkill_cleanup` URL을 `JOBNKILL_CLEANUP_DATABASE_URL`로 각각 입력합니다. Web의 `JOBNKILL_PUBLIC_URL`에는 고객이 실제로 접속할 최종 HTTPS origin만 입력합니다. 아직 정확한 `onrender.com` 주소나 검증된 커스텀 도메인이 없다면 빈 임시값으로 우회하지 말고 첫 시작 실패를 유지한 채, 서비스가 생성되어 정확한 주소가 확인된 후 값을 설정하고 수동 재배포합니다.

   [Render에서 Job&Kill 서비스 Blueprint 열기](https://render.com/deploy?repo=https://github.com/jonandkill/Jobandkill/tree/jobandkill1)

   기존 시험 서비스에 예전 `JOBNKILL_DATABASE_URL` 또는 `DATABASE_URL`이 있다면 **Blueprint 동기화나 빌드 전에 Dashboard에서 먼저 삭제**합니다. 시작 래퍼도 이를 덮어쓰고 제거하지만, 소유자 비밀값이 빌드 환경에 남는 것 자체를 허용하지 않습니다. 세 서비스 모두 `JOBNKILL_AUTO_MIGRATE=0`이어야 합니다.

5. Web에 Resend에서 발급한 `JOBNKILL_RESEND_API_KEY`와 검증한 발신 주소 `JOBNKILL_RESEND_FROM`을 입력합니다. `JOBNKILL_AUTH_RATE_SECRET`은 Blueprint가 생성합니다.

6. Web에 실제 공개한 HTTPS 개인정보 처리방침 URL·버전·본문 SHA-256인 `JOBNKILL_PRIVACY_POLICY_URL`, `JOBNKILL_PRIVACY_POLICY_VERSION`, `JOBNKILL_PRIVACY_POLICY_SHA256`을 입력합니다. 싱가포르 Blueprint는 `JOBNKILL_OVERSEAS_TRANSFER_REQUIRED=1`이므로 별도 안내 URL·버전·본문 SHA-256도 필수입니다. 안내에는 확인된 수탁자 법인명과 연락처, 국가, 이전 항목·시기·방법, 목적·보유기간, 거부 방법과 영향을 사실대로 적습니다. URL·버전·본문 해시 중 하나라도 바뀌면 기존 사용자의 새 서버 초안 저장은 재동의 전까지 차단됩니다.

7. 승인한 서버 초안 자동 파기기간을 Web과 개인정보 파기 Cron의 `JOBNKILL_DRAFT_RETENTION_DAYS`에 동일하게 입력합니다. 개인정보 처리방침에는 계정·초안·세션·로그·백업별 실제 보유기간과 파기 방식을 각각 기재합니다.

8. 수집 Cron에 공공데이터포털의 `JOBNKILL_ALIO_API_URL_TEMPLATE`, `JOBNKILL_ALIO_SERVICE_KEY`와 S3의 `JOBNKILL_S3_BUCKET`, 선택적 엔드포인트 및 AWS 접근 자격증명을 입력합니다. Web이나 파기 Cron에는 이 값을 넣지 않고, 수집 Cron에는 Resend·개인정보 고지 비밀값을 넣지 않습니다.

9. SMTP는 암호화 설정을 지정한 경우에만 대체 전송 방식으로 지원합니다. 운영 Blueprint 기본값은 Resend HTTPS API입니다.

필수 환경변수의 이름과 개발용 예시는 `.env.example`에 있습니다. 값 자체를 문서화하거나 복사하지 않습니다.

## 4. 배포 전 점검

아래 설정 검사는 수집 Cron 실행 환경에서 수행합니다. 이 서비스에만 공식 API와 S3 자격증명이 있으므로 `--collector-only`를 사용합니다. 관리자 `init`은 이미 끝나 있어야 하며 런타임 역할로 다시 실행하지 않습니다.

```bash
python -m pip install -r requirements-production.txt
scripts/run_with_database_role.sh python -m jobandkill doctor --production --collector-only --require-api
scripts/run_with_database_role.sh python -m jobandkill status
```

`doctor`는 값 자체를 출력하지 않고 구성 여부만 표시합니다. `init`도 PostgreSQL DSN을 출력하지 않습니다. Web은 Gunicorn으로 실행되며, 처리방침·국외 이전 안내·보존기간을 포함한 운영 필수값이 없으면 시작되지 않습니다. 배포 후 `/api/health` 응답과 Resend 매직링크 한 건으로 별도 확인하되, 문서가 특정 공개 URL의 존재를 전제하지 않습니다.

## 5. 최초 전체 동기화

수집 Cron은 매일 18:35 UTC에 증분 갱신과 허용 문서 처리를 수행하고, 별도 개인정보 파기 Cron은 18:20 UTC에 실행됩니다. 최초 전체 수집은 Cron의 기본 명령이 아니므로, 고객 배포 게이트 승인 후 수집 Cron 환경에서 한 번만 아래 명령을 실행합니다. GitHub Actions에 DB·S3·공식 API 비밀값을 저장하거나 외부 DB 연결을 열지 않습니다.

```bash
scripts/run_with_database_role.sh python -m jobandkill sync --source data-go-kr-alio --full
```

결과에서 다음을 확인합니다.

- `status`가 `success`
- `truncated`가 `false`
- `attachment_errors`가 `0`
- `cleanup_failed`, `cleanup_due`, `cleanup_pending_attachments`가 모두 `0`
- `reported_total`과 `seen`이 일치
- `created + updated + unchanged + skipped`가 `seen`과 일치

`partial`, `truncated: true`, 비정상 첨부 항목 또는 정리 대기열이 하나라도 있으면 전체 수집 완료로 표시하지 말고 운영 승인량·페이지 크기·실패 레코드·객체 삭제 로그를 확인한 뒤 다시 실행합니다. `sync` 명령은 모든 페이지의 메타데이터와 권리 철회를 가능한 범위까지 반영한 후 종료 코드 `3`을 반환합니다. upsert 방식이므로 재실행해도 동일 공고가 중복 저장되지 않습니다.

## 6. 문서 권리와 저장

첨부문서는 기본적으로 `review_required`이며 다운로드하지 않습니다. 문서별 공공누리 표시 또는 별도 이용허가를 확인한 뒤 CLI로 판정 이력을 남깁니다.

```bash
scripts/run_with_database_role.sh python -m jobandkill rights-info 42
scripts/run_with_database_role.sh python -m jobandkill rights 42 authorized \
  --reason '기관의 2026-09-01 서면 허가 확인' \
  --by 'content-admin@example.com' \
  --expected-identity 'rights-info가 표시한 identity 값'
scripts/run_with_database_role.sh python -m jobandkill process-documents --limit 50 --fail-on-error
```

허용 판정 전 `rights-info`에 표시된 현재 공식 URL·제목·라이선스·원문 해시와 권리/피드 리비전을 증빙과 대조합니다. `identity`는 이 값들의 해시이며 허용 트랜잭션의 비교 조건으로 사용됩니다. 검토 이후 첨부가 교체·제거되거나 더 최신 제한 판정이 기록되면 허용 명령이 거부되므로 현재 상태를 다시 검토해야 합니다. 제한·철회는 안전을 위해 `identity` 없이도 즉시 적용됩니다.

`--fail-on-error`를 사용하면 문서 추출 실패, 객체 삭제 재시도 실패, 배치 한도를 넘긴 즉시 처리 대기열, 검증 불가능한 이전 객체 격리가 하나 이상일 때 종료 코드 `4`를 반환하므로 자동화가 불완전한 정리를 성공으로 기록하지 않습니다. `unsupported` 문서는 별도 검토 대상으로 집계되며 이 옵션의 실패 조건에는 포함되지 않습니다.

S3 객체는 원래 파일명이 아닌 `documents/{sha256 앞 2자}/{sha256}.확장자`로 저장되고 서버 측 암호화를 요청합니다. 업로드 의도와 삭제 작업은 데이터베이스에 먼저 기록되며, 업로드 의도에는 첨부 ID와 작업 토큰이 포함됩니다. 따라서 업로드와 권리 철회가 겹쳐도 철회 작업이 대상 객체를 기다렸다가 정리하며, 일시적 실패는 지수 백오프로 재시도됩니다. 권리를 제한 상태로 되돌리면 추출 텍스트·파생 프로필을 먼저 차단하고 원문 객체를 정리합니다. 동일 URL도 `JOBNKILL_DOCUMENT_RECHECK_DAYS`(기본 7일)마다 해시를 다시 확인하고, 기존 별도 승인 원문과 해시가 다르면 파싱 전에 차단합니다.

`JOBNKILL_DOCUMENT_DIR`, S3 버킷 또는 엔드포인트는 최초 객체 저장 시 데이터베이스에 고정됩니다. 값을 임의로 바꾸면 삭제가 성공한 것처럼 오판하지 않고 작업이 실패합니다. 저장소 이전은 기존 위치에서 대기 중 삭제를 모두 완료하고 별도의 검증된 마이그레이션 절차로 수행합니다.

[NCS 저작권정책](https://www.ncs.go.kr/unity/th01/selectPolicyPopView.do)에 따라 상업 이용이 제한된 NCS 학습모듈은 수집하지 않습니다. NCS 등록 직무기술서도 문서별 권리를 확인하기 전에는 메타데이터와 공식 링크만 유지합니다.

## 7. 운영 검증

- PostgreSQL 백업을 별도 환경에 실제 복원
- Render와 같은 비슈퍼유저 소유자 조건의 빈 검증 DB에서 `init`·역할 ACL 스크립트·세 역할 접근 거부 테스트를 실제 실행
- S3 공개 접근 차단, 암호화, 삭제 권한 확인
- S3 버전 관리 비활성화 및 `GetBucketVersioning` 확인 권한 검증
- 실제 매직링크 메일 1건 발송과 만료 전 로그인 확인
- 공개 로그인 앞단의 CAPTCHA 또는 WAF 속도 제한, Resend 일일 비용 한도·이상발송 경보 확인
- 매직링크 만료·재사용 차단, 로그아웃, CSRF 거부 확인
- 동일 이메일·IPv4·IPv6 `/64`의 속도 제한과 분산 요청 대응 확인
- 같은 초안을 두 탭에서 수정했을 때 `409` 충돌과 사용자 선택 확인
- 전체 건수와 공식 API `totalCount` 대조
- 320px, 390px, 768px, 1440px에서 로그인·직무 검색·8단계 작성·결과 모달 시각 QA
- 스캔 PDF와 바이너리 HWP는 자동 공개하지 않고 별도 검토 큐로 분리
- `document_gc_queue`의 실패·재시도 건수와 `quarantined` 건수 0 확인

## 8. 초안·계정 데이터와 운영 확인

- 브라우저 초안은 기본적으로 현재 브라우저 세션에만 저장됩니다. 사용자가 “이 기기에 30일 동안 초안 보관”을 명시적으로 선택했을 때만 해당 기기의 로컬 저장소에 최대 30일 보관합니다.
- 로그인 후에도 사용자가 계정 저장에 동의하기 전에는 브라우저 초안을 계정으로 보내지 않습니다.
- 로그인한 사용자는 계정 데이터 내보내기와 계정·저장 초안의 운영 DB 삭제를 실행할 수 있습니다. 삭제는 확인 문구와 최근 로그인 재인증을 요구합니다. 공급자 백업 사본은 승인된 백업 보존기간이 만료될 때 제거되며, 다른 기기의 브라우저 초안은 각 기기에서 별도로 삭제해야 합니다.
- 실제 발송 전 Resend에서 한 건의 매직링크를 검증하고, 만료·재사용 차단·로그아웃·CSRF 거부를 확인합니다.
- PostgreSQL 백업을 별도 환경에 실제 복원하고, S3 공개 접근 차단·암호화·삭제 권한·버전 관리 비활성화를 확인합니다.
- `document_gc_queue`의 실패·재시도와 `quarantined` 건수가 0인지, 공식 API 총건수와 수집 결과가 일치하는지 확인합니다.

운영 전에는 먼저 삭제 예정 건수를 확인한 뒤 실행 결과를 대조합니다. 명령 출력은 건수만 포함하며 이메일·초안 본문·세션값을 출력하지 않습니다.

```bash
scripts/run_with_database_role.sh python -m jobandkill cleanup-personal-data
scripts/run_with_database_role.sh python -m jobandkill cleanup-personal-data --execute
```

자동 정리는 보존기간이 지난 서버 초안, 만료·사용완료 로그인 토큰, 만료·유휴·폐기 세션, 초안과 유효 토큰이 없는 오래된 미인증 계정을 대상으로 합니다. 인증된 계정은 단순 비활동만으로 자동 삭제하지 않으며, 사용자는 화면에서 내보내기와 계정 삭제를 직접 실행할 수 있습니다.

현재 일일 Cron은 공식 API의 앞부분을 멱등 재조회하는 갱신입니다. 공식 API가 수정일 커서의 의미와 정렬 안정성을 문서로 보장하기 전에는 이를 완전한 델타 동기화라고 부르지 않습니다.
