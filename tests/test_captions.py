"""Self-check cho caption ảnh (alt → OCR → payload/UI).

Run: python -m tests.test_captions  |  pytest tests/test_captions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.service.crawl.parser import parse_html
from sict_crawler.service.index.annotate_ocr import build_caption
from sict_crawler.service.retrieval.sources import media_sources

HTML = """
<html><head><title>Tin tức</title></head><body><main>
<h1>Bản tin</h1><p>Nội dung.</p>
<img src="/media/a.jpg" alt="Lễ trao giải sinh viên NCKH">
<img src="/media/b.jpg" alt="">
<img src="/media/c.jpg">
<img src="/no-image.png" alt="placeholder">
</main></body></html>
"""


def test_parser_keeps_alt_aligned():
    p = parse_html("https://sict.haui.edu.vn/vn/tin-tuc", HTML)
    assert len(p.image_urls) == len(p.image_alts) == 3, (p.image_urls, p.image_alts)
    assert p.image_alts[0] == "Lễ trao giải sinh viên NCKH", p.image_alts
    assert p.image_alts[1] == "" and p.image_alts[2] == ""
    assert not any("no-image" in u for u in p.image_urls)


def test_parser_alt_falls_back_to_title_attr():
    p = parse_html("https://sict.haui.edu.vn/vn/x",
                   "<html><body><main><img src='/m/d.jpg' title='Ảnh toàn cảnh'></main></body></html>")
    assert p.image_alts == ["Ảnh toàn cảnh"], p.image_alts


def test_build_caption_prefers_ocr():
    assert build_caption("Bản tin", "TRƯỜNG ĐẠI HỌC CÔNG NGHIỆP HÀ NỘI") == \
        "Bản tin. TRƯỜNG ĐẠI HỌC CÔNG NGHIỆP HÀ NỘI"
    assert build_caption("Bản tin", "  ngắn  ") == "Bản tin"  # OCR <10 ký tự → title
    assert build_caption("", "văn bản ocr dài đủ dùng") == "văn bản ocr dài đủ dùng"
    assert build_caption("Bản tin", "") == "Bản tin"


def test_media_hit_prefers_caption_over_s3key():
    with_cap = media_sources([{"score": 0.9, "url": "u", "kind": "image",
                               "media_id": 1, "s3_key": "media/abc.jpg",
                               "caption": "Lễ trao giải"}])
    assert with_cap[0].excerpt == "Lễ trao giải", with_cap[0].excerpt
    without_cap = media_sources([{"score": 0.9, "url": "u", "kind": "image",
                                  "media_id": 2, "s3_key": "media/abc.jpg"}])
    assert without_cap[0].excerpt == "media/abc.jpg"


if __name__ == "__main__":
    test_parser_keeps_alt_aligned()
    print("OK — parser giữ alt thẳng hàng urls, bỏ no-image")
    test_parser_alt_falls_back_to_title_attr()
    print("OK — alt fallback title attr")
    test_build_caption_prefers_ocr()
    print("OK — build_caption title+OCR")
    test_media_hit_prefers_caption_over_s3key()
    print("OK — media excerpt ưu tiên caption")
