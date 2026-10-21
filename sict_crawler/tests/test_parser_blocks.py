"""D23: cây block theo thứ tự DOM — heading_path, vị trí ảnh/video/file, bảng thừa hưởng heading, markdown không đổi."""
import re

from bs4 import BeautifulSoup

from sict_crawler.parser import build_blocks, parse_html

BASE = "https://sict.haui.edu.vn/vn/tin-tuc/a/1"
HTML = """<html><head><title>T</title></head><body><div class="irs-blog-single-col">
<p>Mở đầu bài viết.</p>
<h2>Phần A</h2><p>Nội dung A.</p>
<p><img src="/media/1/a1.jpg" alt="ảnh A1"></p>
<h3>Mục A.1</h3>
<table><tr><th>Tên</th><th>Điểm</th></tr><tr><td>Nam</td><td>9</td></tr></table>
<p>Sau bảng vẫn thuộc Mục A.1. <a href="/media/9/quyche.pdf">Tải về</a></p>
<h2>Phần B</h2>
<ul><li>một</li><li>hai</li></ul>
<figure><img src="/media/2/b1.jpg"><figcaption>Chú thích B1</figcaption></figure>
<iframe src="https://www.youtube.com/embed/abc"></iframe>
<div>chữ trần trong div<br>dòng hai</div>
</div></body></html>"""


def _blocks():
    return parse_html(BASE, HTML).blocks


def test_order_types_and_heading_path():
    b = _blocks()
    assert [x["idx"] for x in b] == list(range(len(b)))
    types = [x["type"] for x in b]
    assert types[:4] == ["paragraph", "heading", "paragraph", "image"]
    h = [x for x in b if x["type"] == "heading"]
    assert [(x["level"], x["text"]) for x in h] == [(2, "Phần A"), (3, "Mục A.1"), (2, "Phần B")]
    assert b[0]["heading_path"] == []                                     # trước heading đầu: rỗng
    assert next(x for x in b if x["text"] == "Nội dung A.")["heading_path"] == ["Phần A"]
    assert h[1]["heading_path"] == ["Phần A", "Mục A.1"]                  # heading tự nằm trong đường dẫn
    assert h[2]["heading_path"] == ["Phần B"]                             # cấp 2 mới cắt Mục A.1


def test_table_and_following_text_keep_section():
    b = _blocks()
    t = next(x for x in b if x["type"] == "table")
    assert t["heading_path"] == ["Phần A", "Mục A.1"] and t["attrs"]["rows"] == 2
    assert t["text"] == "Tên | Điểm\nNam | 9"
    after = b[b.index(t) + 1]
    assert after["type"] == "paragraph" and after["heading_path"] == ["Phần A", "Mục A.1"]   # không mất heading sau bảng


def test_media_positions():
    b = _blocks()
    img = [x for x in b if x["type"] == "image"]
    assert [x["attrs"]["url"] for x in img] == ["https://sict.haui.edu.vn/media/1/a1.jpg",
                                                 "https://sict.haui.edu.vn/media/2/b1.jpg"]
    assert img[0]["attrs"]["alt"] == "ảnh A1" and img[0]["heading_path"] == ["Phần A"]
    assert img[1]["heading_path"] == ["Phần B"]
    att = next(x for x in b if x["type"] == "attachment")
    assert att["attrs"]["url"].endswith("/media/9/quyche.pdf") and att["heading_path"] == ["Phần A", "Mục A.1"]
    vid = next(x for x in b if x["type"] == "video")
    assert "youtube.com/embed/abc" in vid["attrs"]["url"] and vid["heading_path"] == ["Phần B"]
    assert any(x["type"] == "paragraph" and x["text"] == "Chú thích B1" for x in b)
    assert next(x for x in b if x["type"] == "list")["text"].startswith("một")
    assert any(x["text"].startswith("chữ trần trong div") for x in b)


def test_blocks_do_not_change_markdown_or_flat_lists():
    p = parse_html(BASE, HTML)
    soup = BeautifulSoup(HTML, "lxml")
    before = str(soup)
    build_blocks(soup.select_one("div.irs-blog-single-col"), BASE)
    assert str(soup) == before                                            # không sửa DOM → hash_v2 giữ nguyên
    assert [b["attrs"]["url"] for b in p.blocks if b["type"] == "image"] == p.image_urls
    assert [b["attrs"]["url"] for b in p.blocks if b["type"] == "attachment"] == [u for u, _ in p.attachments]


def test_real_fixtures_have_blocks_matching_flat_lists():
    import pathlib
    d = pathlib.Path(__file__).parent / "fixtures"
    for name in ("sict_article", "sict_attachments", "sict_staff_table"):
        p = parse_html(BASE, (d / f"{name}.html").read_text())
        assert p.blocks, name
        urls = list(dict.fromkeys(b["attrs"]["url"] for b in p.blocks if b["type"] == "image"))
        assert urls == p.image_urls, name
        assert list(dict.fromkeys(b["attrs"]["url"] for b in p.blocks if b["type"] == "attachment")) \
            == [u for u, _ in p.attachments], name


# ---- chunk theo cấu trúc + hash_struct ----
from sict_crawler import indexing as ix
from sict_crawler.compute import chunk_row, doc_chunks, page_doc
from sict_crawler.config import get_settings


def _doc(html=HTML, url=BASE):
    status, doc, md, p = page_doc(url, html, include_fallback=True)
    assert status == "ok"
    return doc, md, p


def test_structured_chunks_heading_path_and_table_section():
    doc, md, p = _doc()
    ch = get_settings().chunking
    old = (ch.structured, ch.min_tokens)
    ch.structured, ch.min_tokens = True, 1                 # min nhỏ → ngắt ở mọi heading (mục nhỏ không bị gộp)
    try:
        chunks = doc_chunks(doc, md, blocks=p.blocks)
    finally:
        ch.structured, ch.min_tokens = old
    tbl = [c for c in chunks if c.kind == "table"]
    assert len(tbl) == 1 and tbl[0].heading_path == ["Phần A", "Mục A.1"]
    assert "[Phần A › Mục A.1] [Bảng] | Tên | Điểm |" in tbl[0].text
    assert "| --- | --- |" in tbl[0].text and "| Nam | 9 |" in tbl[0].text
    after = next(c for c in chunks if "Sau bảng" in c.text)                       # chữ sau bảng giữ đúng mục
    assert after.heading_path == ["Phần A", "Mục A.1"]
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    row = chunk_row(chunks[0])
    assert row["block_range"][0] <= row["block_range"][1] and isinstance(row["heading_path"], list)
    assert all(not c.text.endswith("# ") for c in chunks)


def test_structured_off_keeps_markdown_chunks():
    doc, md, p = _doc()
    assert get_settings().chunking.structured is False
    chunks = doc_chunks(doc, md, semantic=False, blocks=p.blocks)
    assert all(c.block_range is None for c in chunks) and "heading_path" not in chunk_row(chunks[0])


def test_hash_struct_detects_media_changes_but_not_markdown_hash():
    d1, _, _ = _doc()
    d2, _, _ = _doc(HTML.replace("/media/2/b1.jpg", "/media/2/b9.jpg"))           # đổi ảnh: markdown không đổi
    d3, _, _ = _doc(HTML.replace("quyche.pdf", "quyche2.pdf"))                    # đổi file đính kèm
    assert d1["content_hash"] == d2["content_hash"] == d3["content_hash"]        # hash_v2 không thấy → trước đây "unchanged"
    assert len({d1["hash_struct"], d2["hash_struct"], d3["hash_struct"]}) == 3
    at = ix.block_index(d1["structure"])                                           # vị trí + mục chứa media
    assert at["https://sict.haui.edu.vn/media/1/a1.jpg"][1] == ["Phần A"]
    assert at["https://sict.haui.edu.vn/media/2/b1.jpg"][1] == ["Phần B"]
    assert "Nam" not in str(d1["structure"])                                      # outline không lặp toàn văn


def test_structured_splits_long_section_by_sentence():
    long = "Câu số một khá dài để tốn ngân sách. " * 120
    html = f'<div class="irs-blog-single-col"><h2>Dài</h2><p>{long}</p></div>'
    doc, md, p = _doc(html, BASE)
    meta = ix.DocMeta(**{k: doc[k] for k in ix.DocMeta.__dataclass_fields__})
    chunks = ix.build_chunks_structured(meta, p.blocks, 50, 200)
    assert len(chunks) > 1 and all(c.heading_path == ["Dài"] for c in chunks)
    assert all(ix._tokens(c.text) <= 260 for c in chunks)


def test_br_inside_table_cell_keeps_item_boundary():
    html = ('<div class="irs-blog-single-col"><p>Danh sách đề tài sinh viên nghiên cứu khoa học năm 2022.</p>'
            '<table><tr><td>1</td><td>Cao Bá Lộc (CN)<br/> Lê Minh Hiếu<br/>Lương Thị Sao Mai</td></tr></table></div>')
    p = parse_html(BASE, html)
    assert "Cao Bá Lộc (CN); Lê Minh Hiếu; Lương Thị Sao Mai" in p.markdown
    assert re.sub(r"\s+;", ";", next(b for b in p.blocks if b["type"] == "table")["text"]).count(";") == 2
    names = [m.name for m in ix_graph().extract_persons(p.markdown)]
    assert "Lê Minh Hiếu" in names and "Lương Thị Sao Mai" in names and not any(len(n.split()) > 4 for n in names)


def ix_graph():
    from sict_crawler import graph
    return graph


def test_pdf_layout_columns_kept_apart():
    text = "1   Nguyễn Văn An      Trần Thị Bình     CNTT01\n2   Lê Văn Cường       Phạm Thị Dung     CNTT02"
    clean = ix._clean_page(text)
    assert "Nguyễn Văn An | Trần Thị Bình | CNTT01" in clean
    names = {m.name for m in ix_graph().extract_persons(clean)}
    assert {"Nguyễn Văn An", "Trần Thị Bình", "Lê Văn Cường", "Phạm Thị Dung"} <= names
