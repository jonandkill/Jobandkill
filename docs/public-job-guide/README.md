# 전국 공공채용, 처음부터 합격까지

일반 취업준비생을 위한 대한민국 공공기관·공기업·지방공기업 데이터북 겸 실전 전자책입니다.

## 완성 산출물

- dist/public-sector-job-guide-ko-2026.epub
- dist/public-sector-job-guide-ko-2026.pdf
- dist/public-sector-job-guide-ko-2026.html
- site/ GitHub Pages 정적 웹북
- data/recruitments-2026-09-09.csv 전국 모집 중 채용 스냅샷
- data/대한민국_공공기관_전국채용_일반취준생_통합판_2026-09-09.xlsx

## 데이터 범위

- 기준시점: 2026-09-09 KST
- JOB-ALIO 진행 중: 419건, 127개 기관
- Cleaneye Job+ 모집 중: 360건, 183개 기관
- 합계: 779행
- JOB-ALIO 상세 페이지 검증: 일반 취준생 우선공고 26건

Cleaneye의 모집중 결과에는 중복 및 장기 미마감 표기가 포함됩니다. 원자료를 삭제하지 않고 데이터품질 열로 구분했습니다. 이 저장소의 제목 기반 분류는 탐색을 돕는 편집부 파생값이며 지원자격을 확정하지 않습니다.

## 로컬 빌드

Node.js, Pandoc, XeLaTeX, Poppler, ImageMagick, Python 3의 ReportLab·pypdf가 필요합니다.

    chmod +x scripts/build.sh
    ./scripts/build.sh

## 출간 원칙

지원 전 반드시 기관의 최신 공식 채용공고, 첨부 공고문, 직무기술서, FAQ와 정정공고를 확인해야 합니다. 이 책은 특정 기관의 공식 안내나 합격 보장이 아닙니다.

원고·삽화는 LICENSE-CONTENT.md, 사이트 코드와 빌드 스크립트는 LICENSE-CODE를 따릅니다.

## GitHub 배포

저장소의 `docs/public-job-guide/`에 이 디렉터리를 두고, 포함된 워크플로를 저장소 루트의 `.github/workflows/public-job-guide.yml`로 복사합니다. 풀 리퀘스트에서는 출간 파일을 빌드해 검증하고, 기본 브랜치에 병합하면 GitHub Pages 배포를 실행합니다. 저장소 설정에서 Pages 소스를 **GitHub Actions**로 지정해야 합니다.
