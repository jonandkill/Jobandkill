#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

mkdir -p dist site/downloads site/assets/images assets/images
export FONTCONFIG_FILE="$ROOT/fontconfig-local.conf"
export SOURCE_DATE_EPOCH=1788879600

node scripts/generate-data.mjs

for svg in assets/images/*.svg; do
  name=$(basename "$svg" .svg)
  png="assets/images/$name.png"
  if [ ! -s "$png" ] || [ "$svg" -nt "$png" ]; then
    if command -v rsvg-convert >/dev/null 2>&1; then
      rsvg-convert -w 1600 "$svg" -o "$png"
    elif command -v inkscape >/dev/null 2>&1; then
      inkscape "$svg" --export-type=png --export-filename="$png" --export-width=1600 >/dev/null
    else
      echo "SVG 변환기(rsvg-convert 또는 inkscape)가 필요합니다." >&2
      exit 1
    fi
  fi
  convert "$png" null: >/dev/null
done

sync

cp assets/images/*.png site/assets/images/

pandoc \
  --from=markdown+pipe_tables+raw_tex+smart \
  --metadata-file=metadata/book.yaml \
  --resource-path="$ROOT" \
  --toc --toc-depth=2 \
  --epub-cover-image=assets/images/cover.png \
  --css=styles/epub.css \
  manuscript/00-frontmatter.md \
  manuscript/01-data-snapshot.md \
  manuscript/02-guide.md \
  manuscript/03-credits.md \
  -o dist/public-sector-job-guide-ko-2026.epub

pandoc \
  --from=markdown+pipe_tables+raw_tex+smart \
  --metadata-file=metadata/book.yaml \
  --resource-path="$ROOT" \
  --pdf-engine=xelatex \
  --top-level-division=chapter \
  --toc --toc-depth=2 \
  --include-in-header=styles/header.tex \
  -V documentclass=book \
  -V papersize=a4 \
  -V geometry:margin=22mm \
  -V fontsize=11pt \
  -V colorlinks=true \
  manuscript/00-frontmatter.md \
  manuscript/01-data-snapshot.md \
  manuscript/02-guide.md \
  manuscript/03-credits.md \
  -o dist/public-sector-job-guide-ko-2026.interior.pdf

python3 scripts/assemble-pdf.py \
  assets/images/cover.png \
  dist/public-sector-job-guide-ko-2026.interior.pdf \
  dist/public-sector-job-guide-ko-2026.pdf

pandoc \
  --from=markdown+pipe_tables+raw_tex+smart \
  --metadata-file=metadata/book.yaml \
  --resource-path="$ROOT" \
  --standalone --toc --toc-depth=2 \
  --template=site/book-template.html \
  manuscript/00-frontmatter.md \
  manuscript/01-data-snapshot.md \
  manuscript/02-guide.md \
  manuscript/03-credits.md \
  -o site/book.html

pandoc \
  --from=markdown+pipe_tables+raw_tex+smart \
  --metadata-file=metadata/book.yaml \
  --resource-path="$ROOT" \
  --standalone --toc --toc-depth=2 \
  --embed-resources \
  --css=site/assets/styles.css \
  manuscript/00-frontmatter.md \
  manuscript/01-data-snapshot.md \
  manuscript/02-guide.md \
  manuscript/03-credits.md \
  -o dist/public-sector-job-guide-ko-2026.html

cp dist/public-sector-job-guide-ko-2026.pdf site/downloads/
cp dist/public-sector-job-guide-ko-2026.epub site/downloads/
cp data/recruitments-2026-09-09.csv site/downloads/
if [ -f data/대한민국_공공기관_전국채용_일반취준생_통합판_2026-09-09.xlsx ]; then
  cp data/대한민국_공공기관_전국채용_일반취준생_통합판_2026-09-09.xlsx site/downloads/
fi
cp LICENSE-CONTENT.md LICENSE-CODE LICENSE-FONT CHANGELOG.md QA_REPORT.md site/downloads/

unzip -t dist/public-sector-job-guide-ko-2026.epub > dist/epub-validation.txt
if command -v epubcheck >/dev/null 2>&1; then
  epubcheck dist/public-sector-job-guide-ko-2026.epub > dist/epubcheck.txt
fi
pdfinfo dist/public-sector-job-guide-ko-2026.pdf > dist/pdf-info.txt
sha256sum \
  dist/public-sector-job-guide-ko-2026.epub \
  dist/public-sector-job-guide-ko-2026.pdf \
  dist/public-sector-job-guide-ko-2026.html \
  > dist/SHA256SUMS
if [ -f data/대한민국_공공기관_전국채용_일반취준생_통합판_2026-09-09.xlsx ]; then
  sha256sum data/대한민국_공공기관_전국채용_일반취준생_통합판_2026-09-09.xlsx >> dist/SHA256SUMS
fi

test "$(node -p "require('./data/summary.json').totalRows")" = "779"
test -s site/assets/recruitments.js
test -s site/book.html
test -s dist/public-sector-job-guide-ko-2026.epub
test -s dist/public-sector-job-guide-ko-2026.pdf
test -s dist/public-sector-job-guide-ko-2026.html

rm -f dist/public-sector-job-guide-ko-2026.interior.pdf
echo "빌드 완료: $ROOT/dist"
