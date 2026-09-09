# 전국 공공채용, 처음부터 합격까지

정대영·잡앤킬의 판매용 개정판 소스입니다. 전국 공공기관·공기업·지방공기업·지방출자출연기관에 지원하는 일반 취업준비생을 대상으로, 2026-09-09 KST 공식 채용 포털 스냅샷 779건과 조직의 판단 구조·지원판정·NCS·면접·지역·자격증 전략을 연결합니다.

## 판매용 개정판

- 저자: 정대영 · 잡앤킬
- 본문: 11pt / 기준선 20.35pt(185%)
- 제목: 19pt / 14pt / 12pt
- 형식: PDF · EPUB · 독립 HTML · 검색형 웹북 · XLSX · CSV
- 삽화: 외부 사진과 기관 로고 없이 자체 제작한 설명형 SVG
- 출간 원칙: 실제 지원 전 최신 공식 공고·첨부·직무기술서·FAQ·정정공고 확인

## 빌드

Node.js, Pandoc, XeLaTeX, Poppler, ImageMagick, Inkscape 또는 rsvg-convert, Python 3의 ReportLab·pypdf가 필요합니다.

    chmod +x scripts/build.sh
    ./scripts/build.sh

빌드 결과는 `dist/`에 생성되며 GitHub Actions 아티팩트에도 보존됩니다.

## 데이터 범위

- JOB-ALIO 진행 중: 419건, 127개 기관
- Cleaneye Job+ 모집 중: 360건, 183개 기관
- 합계: 779행
- JOB-ALIO 상세 페이지 검증: 우선공고 26건

779행은 기준일 현재 두 포털이 반환한 스냅샷이며 모든 기관의 영구 명부나 접수 가능성을 보증하지 않습니다.

## 라이선스

판매용 개정판 원고·편집·표지·추가 삽화는 `LICENSE-CONTENT.md`, 코드와 빌드 스크립트는 `LICENSE-CODE`를 따릅니다. 이전 공개 초판의 CC BY 4.0 허락은 해당 공개판에 한해 유지됩니다.
