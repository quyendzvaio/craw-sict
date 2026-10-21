"""D24: số trang thật cho file PDF/PPTX — tách trang, chunk không vắt qua trang, payload `page`."""
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest

from sict_crawler import indexing as ix
from sict_crawler.compute import chunk_row, doc_chunks
from sict_crawler.config import get_settings


def make_pdf(path: Path, pages: list[str]) -> None:
    """PDF tối thiểu có lớp chữ (Helvetica, ASCII), mỗi phần tử = 1 trang."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{3 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>"]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def _long(tag: str) -> str:
    return " ".join(f"{tag} noi dung quy che dao tao so {i} cua truong." for i in range(8))


needs_poppler = pytest.mark.skipif(not shutil.which("pdftotext"), reason="cần poppler")


@needs_poppler
def test_pdf_pages_numbered_and_blank_page_keeps_numbering():
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "q.pdf"
        make_pdf(pdf, [_long("ALPHA"), "", _long("GAMMA")])
        pages, st = ix.extract_file_pages(pdf)
        assert st == "ok" and [n for n, _ in pages] == [1, 3]            # trang 2 trống → số 3 vẫn đúng
        assert "ALPHA" in pages[0][1] and "GAMMA" in pages[1][1]
        assert ix.extract_file_text(pdf)[0] == "\n\n".join(t for _, t in pages)   # extract_file_text = nối các trang


@needs_poppler
def test_paged_chunks_never_cross_pages(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "q.pdf"
        make_pdf(pdf, [_long("ALPHA"), _long("BETA"), _long("GAMMA")])
        pages, _ = ix.extract_file_pages(pdf)
    doc = ix.build_meta("https://sict.haui.edu.vn/media/q.pdf", "Quy che", doc_type="attachment",
                        parent_url="https://sict.haui.edu.vn/vn/thong-bao/a/1").to_dict()
    ch = get_settings().chunking
    monkeypatch.setattr(ch, "structured", True)
    chunks = doc_chunks(doc, "\n\n".join(t for _, t in pages), semantic=False, pages=pages)
    assert {c.page for c in chunks} == {1, 2, 3} and [c.idx for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        tag = {1: "ALPHA", 2: "BETA", 3: "GAMMA"}[c.page]
        assert f"[trang {c.page}]" in c.text and tag in c.text
        assert all(o not in c.text for o in ({"ALPHA", "BETA", "GAMMA"} - {tag}))   # không lẫn chữ trang khác
    assert chunk_row(chunks[0])["page"] == 1


def test_structured_off_keeps_text_chunks(monkeypatch):
    doc = ix.build_meta("https://sict.haui.edu.vn/media/q.pdf", "Quy che", doc_type="attachment").to_dict()
    assert get_settings().chunking.structured is False
    chunks = doc_chunks(doc, _long("ALPHA"), semantic=False, pages=[(1, _long("ALPHA"))])
    assert all(c.page is None and "[trang" not in c.text for c in chunks)   # v2 không đổi


def test_pptx_slides_numeric_order_and_pages():
    def slide(t):
        return ('<p:sld xmlns:p="x" xmlns:a="y"><a:p><a:r><a:t>%s</a:t></a:r></a:p></p:sld>' % t)
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.pptx"
        with zipfile.ZipFile(f, "w") as z:
            z.writestr("ppt/presentation.xml", "<x/>")
            for n in (1, 2, 10):
                z.writestr(f"ppt/slides/slide{n}.xml", slide(f"Noi dung slide so {n} " * 4))
        pages, st = ix.extract_file_pages(f)
        assert st == "ok" and [n for n, _ in pages] == [1, 2, 3]
        assert "slide so 10" in pages[2][1]                              # slide10 sau slide2, không phải trước
    assert pages[0][1].startswith("Noi dung slide so 1")


def test_docx_has_no_page_numbers():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.docx"
        body = "".join(f"<w:p><w:r><w:t>Doan van ban so {i} kha dai de du dieu kien.</w:t></w:r></w:p>" for i in range(4))
        with zipfile.ZipFile(f, "w") as z:
            z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
        pages, st = ix.extract_file_pages(f)
        assert st == "ok" and [n for n, _ in pages] == [None]
