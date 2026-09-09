#!/usr/bin/env python3
"""판매용 전자책 산출물의 구조·메타데이터·빈 페이지를 자동 검수한다."""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PDF = DIST / "public-sector-job-guide-ko-2026.pdf"
EPUB = DIST / "public-sector-job-guide-ko-2026.epub"
HTML = DIST / "public-sector-job-guide-ko-2026.html"
SALE_ZIP = DIST / "대한민국_공공채용_전자책_판매용_2026.zip"
GUIDE = ROOT / "manuscript" / "02-guide.md"
HEADER = ROOT / "styles" / "header.tex"

errors: list[str] = []
warnings: list[str] = []
checks: dict[str, object] = {}


def require(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


for artifact in (PDF, EPUB, HTML, SALE_ZIP):
    require(artifact.is_file() and artifact.stat().st_size > 0, f"산출물 누락 또는 빈 파일: {artifact.name}")

reader = PdfReader(str(PDF))
checks["pdf_pages"] = len(reader.pages)
checks["pdf_size_bytes"] = PDF.stat().st_size
metadata = reader.metadata or {}
author = str(metadata.get("/Author", ""))
checks["pdf_author"] = author
require("정대영" in author and "잡앤킬" in author, "PDF 저자 메타데이터가 최종 표기와 다릅니다.")

blank_pages: list[int] = []
low_density_pages: list[dict[str, int]] = []
heading_tail_pages: list[int] = []
heading_pattern = re.compile(r"^(?:제?\s*\d+장|\d+[.)]\s+|\d+\.\s+|부록|들어가며|이 책을)")
for page_number, page in enumerate(reader.pages, start=1):
    text = (page.extract_text() or "").strip()
    if page_number > 1 and not text:
        blank_pages.append(page_number)
    compact = re.sub(r"\s+", "", text)
    if page_number > 1 and 0 < len(compact) < 45:
        low_density_pages.append({"page": page_number, "characters": len(compact)})
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if page_number > 1 and lines and heading_pattern.match(lines[-1]) and len(lines[-1]) < 70:
        heading_tail_pages.append(page_number)

checks["blank_pages"] = blank_pages
checks["low_density_pages_review"] = low_density_pages
checks["possible_heading_orphans_review"] = heading_tail_pages
require(not blank_pages, f"텍스트가 없는 내부 페이지 발견: {blank_pages}")
if heading_tail_pages:
    warnings.append(f"페이지 끝 제목 후보 수동 확인 권장: {heading_tail_pages}")

outline = getattr(reader, "outline", [])
def count_outline(items: object) -> int:
    if not isinstance(items, list):
        return 0
    return sum(1 + (count_outline(item) if isinstance(item, list) else 0) for item in items)
outline_count = count_outline(outline)
checks["pdf_outline_items"] = outline_count
require(outline_count > 0, "PDF 책갈피(목차 아웃라인)가 없습니다.")

with zipfile.ZipFile(EPUB) as zf:
    bad_epub = zf.testzip()
    names = zf.namelist()
    first = names[0] if names else ""
    mime = zf.read("mimetype").decode("ascii", errors="replace") if "mimetype" in names else ""
    image_count = sum(name.lower().endswith((".png", ".jpg", ".jpeg", ".svg")) for name in names)
checks["epub_first_entry"] = first
checks["epub_mimetype"] = mime
checks["epub_image_entries"] = image_count
require(bad_epub is None, f"EPUB ZIP 손상: {bad_epub}")
require(first == "mimetype", "EPUB 첫 항목이 mimetype이 아닙니다.")
require(mime == "application/epub+zip", "EPUB mimetype이 올바르지 않습니다.")
require(image_count >= 7, f"EPUB 삽화 수가 예상보다 적습니다: {image_count}")

html_text = HTML.read_text(encoding="utf-8")
require("정대영 · 잡앤킬" in html_text, "HTML 저자 표기가 없습니다.")
for forbidden in ("편집·출간용 주의 문구", "제작 크레딧과 검수기록", "시방서"):
    require(forbidden not in html_text, f"판매본에 내부 제작 문구가 남아 있습니다: {forbidden}")

guide_text = GUIDE.read_text(encoding="utf-8")
chapter_numbers = [int(n) for n in re.findall(r"^#\s+(\d+)장\.", guide_text, flags=re.MULTILINE)]
checks["guide_chapter_numbers"] = chapter_numbers
require(chapter_numbers == list(range(1, 14)), f"장 번호가 1~13 연속이 아닙니다: {chapter_numbers}")

header_text = HEADER.read_text(encoding="utf-8")
style_tokens = (
    r"\@setfontsize\normalsize{11pt}{20.35pt}",
    r"\fontsize{19pt}{25pt}",
    r"\fontsize{14pt}{20pt}",
    r"\fontsize{12pt}{18pt}",
    r"\clubpenalty=10000",
    r"\widowpenalty=10000",
    r"\let\cleardoublepage\clearpage",
)
missing_tokens = [token for token in style_tokens if token not in header_text]
checks["typography_tokens_present"] = not missing_tokens
require(not missing_tokens, f"조판 기준 누락: {missing_tokens}")

svg_files = sorted((ROOT / "assets" / "images").glob("*.svg"))
svg_accessibility_missing = []
for svg in svg_files:
    source = svg.read_text(encoding="utf-8")
    if "<title" not in source or "<desc" not in source:
        svg_accessibility_missing.append(svg.name)
checks["svg_count"] = len(svg_files)
checks["svg_accessibility_missing"] = svg_accessibility_missing
require(not svg_accessibility_missing, f"SVG 접근성 설명 누락: {svg_accessibility_missing}")

with zipfile.ZipFile(SALE_ZIP) as zf:
    bad_sale_zip = zf.testzip()
    sale_names = set(zf.namelist())
checks["sale_zip_files"] = sorted(sale_names)
require(bad_sale_zip is None, f"판매 패키지 ZIP 손상: {bad_sale_zip}")
required_suffixes = (".pdf", ".epub", ".html", ".csv", ".xlsx")
for suffix in required_suffixes:
    require(any(name.lower().endswith(suffix) for name in sale_names), f"판매 패키지에 {suffix} 파일이 없습니다.")

report = {
    "status": "passed" if not errors else "failed",
    "checks": checks,
    "warnings": warnings,
    "errors": errors,
}
(DIST / "validation-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(report, ensure_ascii=False, indent=2))
if errors:
    sys.exit(1)
