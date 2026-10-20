"""Parser trên HTML thật của theme irs-* (fixtures/): chỉ lấy vùng nội dung chính (AC-H1..H3).

Run: python -m sict_crawler.tests.test_parser_theme  |  pytest sict_crawler/tests/test_parser_theme.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler.parser import parse_html

FIX = Path(__file__).parent / "fixtures"
ARTICLE_URL = ("https://sict.haui.edu.vn/vn/tin-tuc/"
               "sict-ghi-dau-an-cong-nghe-ai-tai-hanoi-mip-fair-2026/71898")


def _article():
    return parse_html(ARTICLE_URL, (FIX / "sict_article.html").read_text(encoding="utf-8"))


def test_article_uses_main_content():
    p = _article()
    assert not p.content_fallback
    assert "Hanoi MIP Fair 2026" in p.markdown
    assert "Từ ngày 09 – 11/9/2026" in p.markdown


def test_sidebar_video_not_attached():
    # video YouTube ở irs-side-bar xuất hiện trên mọi trang → không thuộc bài
    assert not any("mpIyV4xFf2o" in v for v in _article().video_urls)


def test_site_chrome_images_excluded():
    imgs = _article().image_urls
    assert imgs, "ảnh trong bài vẫn phải có"
    assert not any("/dnn/web/haui/" in u for u in imgs)  # logo, cờ vn/en


def test_links_still_cover_whole_page():
    # frontier cần link toàn trang (menu, sidebar) để discover, không chỉ vùng nội dung
    links = _article().links
    assert any(u.rstrip("/").endswith("/vn/tin-tuc") for u in links)


def test_listing_page_main_column():
    p = parse_html("https://sict.haui.edu.vn/vn/tin-tuc",
                   (FIX / "sict_listing.html").read_text(encoding="utf-8"))
    assert not p.content_fallback
    assert not any("mpIyV4xFf2o" in v for v in p.video_urls)


def test_unknown_layout_flags_fallback():
    p = parse_html("https://x/y", "<html><body><div>" + "chữ " * 100 + "</div></body></html>")
    assert p.content_fallback


if __name__ == "__main__":
    test_article_uses_main_content()
    test_sidebar_video_not_attached()
    test_site_chrome_images_excluded()
    test_links_still_cover_whole_page()
    test_listing_page_main_column()
    test_unknown_layout_flags_fallback()
    print("OK — parser theme irs-*")
