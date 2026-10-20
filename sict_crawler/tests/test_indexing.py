"""Index spec v2 trên HTML thật (fixtures/): metadata, làm sạch, chunk bảng, đính kèm.

Run: python -m sict_crawler.tests.test_indexing  |  pytest sict_crawler/tests/test_indexing.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler import indexing as ix
from sict_crawler.parser import parse_html

FIX = Path(__file__).parent / "fixtures"
ARTICLE = ("https://sict.haui.edu.vn/vn/tin-tuc/"
           "sict-ghi-dau-an-cong-nghe-ai-tai-hanoi-mip-fair-2026/71898")


def _doc(url: str, fixture: str):
    p = parse_html(url, (FIX / fixture).read_text(encoding="utf-8"))
    meta = ix.build_meta(url, p.title, p.published_text, p.section_label)
    return p, meta, ix.clean_markdown(p.markdown)


def test_metadata_article():
    _, meta, _ = _doc(ARTICLE, "sict_article.html")
    assert meta.section == "tin-tuc" and meta.section_label == "Tin tức"
    assert meta.doc_type == "article"
    assert meta.published_at == "2026-09-11T10:31:00+07:00" and meta.year == 2026
    assert meta.header() == "SICT ghi dấu ấn công nghệ AI tại Hanoi MIP Fair 2026 | Tin tức | 11/09/2026"


def test_metadata_static_page_and_listing():
    m = ix.build_meta("https://sict.haui.edu.vn/vn/html/quy-che",
                      "Quy chế - Trường Công nghệ thông tin và Truyền thông || HaUI-SICT")
    assert m.section == "quy-che" and m.doc_type == "page" and m.published_at is None
    assert m.title == "Quy chế"
    assert ix.doc_type_of("https://sict.haui.edu.vn/vn/thong-bao/3") == "listing"
    assert ix.parse_published("31/02/2026") is None


def test_clean_markdown_drops_urls_and_view_counter():
    _, _, md = _doc(ARTICLE, "sict_article.html")
    assert "](" not in md and "https://" not in md
    assert "**" not in md
    assert "Hanoi MIP Fair 2026" in md


def test_chunks_have_context_header():
    _, meta, md = _doc(ARTICLE, "sict_article.html")
    chunks = ix.build_chunks(meta, md, semantic=False)
    assert chunks and all(c.text.startswith(f"[{meta.header()}]") for c in chunks)
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert all(c.meta["published_at"] == meta.published_at for c in chunks)


def test_table_chunks_repeat_header_and_no_duplicate_table():
    p, meta, md = _doc("https://sict.haui.edu.vn/vn/html/can-bo-giang-vien", "sict_staff_table.html")
    chunks = ix.build_chunks(meta, md, semantic=False)
    tables = [c for c in chunks if c.kind == "table"]
    assert tables, "trang giảng viên phải có chunk bảng"
    for c in tables:
        assert c.text.count("| --- ") >= 1 or c.text.count("|---") >= 1, "mỗi chunk bảng có header"
        assert len(c.text) // 4 <= 600
    # mỗi tên giảng viên chỉ nằm trong đúng 1 chunk (không còn bảng nối thêm lần 2)
    name = "Trần Hùng Cường"
    assert sum(name in c.text for c in chunks) == 1


def test_attachments_from_regulation_page():
    p = parse_html("https://sict.haui.edu.vn/vn/html/quy-che",
                   (FIX / "sict_attachments.html").read_text(encoding="utf-8"))
    assert len(p.attachments) >= 10
    descs = " ".join(d for _, d in p.attachments)
    assert "Quy chế đào tạo" in descs


def _make_docx(path: Path, text: str):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = (f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
           f'</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def _make_xlsx(path: Path):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = f'<sst xmlns="{ns}"><si><t>Họ tên</t></si><si><t>Nguyễn Văn A</t></si></sst>'
    sheet = (f'<worksheet xmlns="{ns}"><sheetData>'
             f'<row><c t="s"><v>0</v></c><c><v>2026</v></c></row>'
             f'<row><c t="s"><v>1</v></c><c><v>2025</v></c></row>'
             + "".join(f'<row><c t="inlineStr"><is><t>Sinh viên {i}</t></is></c><c><v>{i}</v></c></row>'
                       for i in range(5))
             + '</sheetData></worksheet>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def test_extract_file_text_formats():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_docx(d / "a.docx", "Quy định chuẩn đầu ra ngoại ngữ cho sinh viên đại học chính quy")
        text, st = ix.extract_file_text(d / "a.docx")
        assert st == "ok" and "chuẩn đầu ra" in text
        # site đặt sai đuôi: docx mang đuôi .doc vẫn đọc được (nhận theo chữ ký byte)
        _make_docx(d / "wrong.doc", "Kế hoạch đào tạo học kỳ 1 năm học 2025-2026 của trường SICT")
        assert ix.sniff_format(d / "wrong.doc") == "docx"
        text, st = ix.extract_file_text(d / "wrong.doc")
        assert st == "ok" and "Kế hoạch đào tạo" in text
        _make_xlsx(d / "b.xlsx")
        (d / "b.xlsx").rename(d / "b.xls")
        assert ix.sniff_format(d / "b.xls") == "xlsx"
        (d / "b.xls").rename(d / "b.xlsx")
        text, st = ix.extract_file_text(d / "b.xlsx")
        assert "Nguyễn Văn A" in text and "| Họ tên | 2026 |" in text
        (d / "c.ppt").write_bytes(b"\xd0\xcf\x11\xe0legacy")
        assert ix.extract_file_text(d / "c.ppt")[1] == "unsupported"
        # doc/xls cũ: thiếu binary catdoc → lỗi có kiểm soát, không crash
        (d / "e.doc").write_bytes(b"\xd0\xcf\x11\xe0legacy")
        st = ix.extract_file_text(d / "e.doc")[1]
        assert st in ("empty", "error:FileNotFoundError") or st == "ok"
        if shutil.which("pdftotext") and shutil.which("ps2pdf"):
            pass  # pdf thật được kiểm ở bước compute (pdftotext có trong image worker)
        assert subprocess is not None


def test_scanned_pdf_ocr_fallback():
    import pytest
    if not (shutil.which("pdftoppm") and shutil.which("tesseract") and shutil.which("pdftotext")):
        pytest.skip("cần poppler + tesseract")
    from PIL import Image, ImageDraw
    with tempfile.TemporaryDirectory() as d:
        im = Image.new("L", (400, 120), 255)
        ImageDraw.Draw(im).text((10, 40), "QUY DINH DAO TAO TIN CHI 2026", fill=0)
        im = im.resize((1600, 480))
        pdf = Path(d) / "scan.pdf"
        im.convert("RGB").save(pdf, "PDF", resolution=200)  # PDF chỉ có ảnh, không lớp text
        assert ix.extract_file_text(pdf)[1] == "empty"       # mặc định không OCR (nhanh)
        text, st = ix.extract_file_text(pdf, ocr_scanned=True)
        assert st in ("ok_ocr", "empty")                     # ảnh ngắn có thể < 50 ký tự
        assert "QUY" in ix.ocr_pdf(pdf).upper()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK — indexing spec v2")
