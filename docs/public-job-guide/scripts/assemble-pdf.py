#!/usr/bin/env python3
from pathlib import Path
import sys

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def make_cover(image_path: Path, cover_pdf: Path) -> None:
    page_w, page_h = A4
    image = ImageReader(str(image_path))
    image_w, image_h = image.getSize()
    scale = min(page_w / image_w, page_h / image_h)
    draw_w, draw_h = image_w * scale, image_h * scale
    pdf = canvas.Canvas(str(cover_pdf), pagesize=A4)
    pdf.setTitle("전국 공공채용, 처음부터 합격까지")
    pdf.setAuthor("정대영 · 잡앤킬")
    pdf.setSubject("기관의 판단 구조를 읽고 지원자격·전형·면접으로 연결하는 2026 실전 지도")
    pdf.drawImage(
        image,
        (page_w - draw_w) / 2,
        (page_h - draw_h) / 2,
        width=draw_w,
        height=draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )
    pdf.showPage()
    pdf.save()


def assemble(image_path: Path, interior_path: Path, output_path: Path) -> None:
    cover_pdf = output_path.with_suffix(".cover.pdf")
    make_cover(image_path, cover_pdf)
    cover = PdfReader(str(cover_pdf))
    interior = PdfReader(str(interior_path))
    writer = PdfWriter()
    writer.clone_document_from_reader(interior)
    writer.insert_page(cover.pages[0], 0)
    metadata = dict(interior.metadata or {})
    metadata.update({
        "/Title": "전국 공공채용, 처음부터 합격까지",
        "/Author": "정대영 · 잡앤킬",
        "/Subject": "기관의 판단 구조를 읽고 지원자격·전형·면접으로 연결하는 2026 실전 지도",
        "/Keywords": "공공기관, 공기업, 지방공기업, NCS, 면접, 사회복지사, 장애인 채용",
    })
    writer.add_metadata({k: str(v) for k, v in metadata.items() if k.startswith("/") and v is not None})
    with output_path.open("wb") as stream:
        writer.write(stream)
    cover_pdf.unlink()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: assemble-pdf.py COVER.png INTERIOR.pdf OUTPUT.pdf")
    assemble(*(Path(arg).resolve() for arg in sys.argv[1:]))
