"""Snapshot crawl offline: hàm thuần + 1 lượt crawl thu nhỏ qua httpx.MockTransport (không mạng).

Run: python -m tests.test_snapshot  |  pytest tests/test_snapshot.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.pipeline import snapshot as snap
from sict_crawler.service.crawl import urls

FIX = Path(__file__).parent / "fixtures"
ARTICLE = ("https://sict.haui.edu.vn/vn/tin-tuc/"
           "sict-ghi-dau-an-cong-nghe-ai-tai-hanoi-mip-fair-2026/71898")
REGULATION = "https://sict.haui.edu.vn/vn/html/quy-che"


def test_url_helpers():
    assert urls.youtube_id("https://www.youtube.com/embed/mpIyV4xFf2o") == "mpIyV4xFf2o"
    assert urls.youtube_id("https://youtu.be/mpIyV4xFf2o?t=3") == "mpIyV4xFf2o"
    assert urls.youtube_id("https://example.com/a.mp4") is None
    assert urls.page_type("https://sict.haui.edu.vn/vn") == "listing"
    assert urls.page_type("https://sict.haui.edu.vn/vn/tin-tuc/7") == "listing"
    assert urls.page_type(ARTICLE) == "content"
    assert urls.page_type("https://sict.haui.edu.vn/vn/html/gioi-thieu") == "content"
    # phân trang giữ depth; bài viết +1
    assert urls.link_depth(3, "https://sict.haui.edu.vn/vn/tin-tuc/9") == 3
    assert urls.link_depth(3, ARTICLE) == 4
    assert urls.is_error_page("x " * 10 + "The parameters dictionary contains a null entry")
    assert urls.is_error_page("ngắn")
    assert not urls.is_error_page("Nội dung bài viết " * 10)


def test_scope_sict_only_and_tags_skipped():
    s = snap.Scope("https://sict.haui.edu.vn", ["/vn/tags"], max_depth=5)
    assert s.accept(ARTICLE, 1)
    assert not s.accept("https://www.haui.edu.vn/vn/tin-tuc", 1)
    assert not s.accept("https://sict.haui.edu.vn/vn/tags?p=1&q=AI", 1)
    assert not s.accept(ARTICLE, 6)


def _handler(calls: list[str]):
    article = (FIX / "sict_article.html").read_bytes()
    listing = (FIX / "sict_listing.html").read_bytes()
    jpeg = b"\xff\xd8\xff" + b"0" * 4000
    regulation = (FIX / "sict_attachments.html").read_bytes()

    def h(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        calls.append(u)
        if u == ARTICLE:
            return httpx.Response(200, content=article, headers={"content-type": "text/html"})
        if u == REGULATION:
            return httpx.Response(200, content=regulation, headers={"content-type": "text/html"})
        if u.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF-1.4 " + u.encode(),
                                  headers={"content-type": "application/pdf"})
        if "/media/" in u:
            return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})
        if u.rstrip("/").endswith("/vn/tin-tuc"):
            return httpx.Response(200, content=listing, headers={"content-type": "text/html"})
        return httpx.Response(404)
    return h


def test_mini_crawl_export_and_resume():
    from sict_crawler.core.config import get_settings
    seeds = get_settings().site.seed_urls
    old = list(seeds)
    seeds[:] = ["https://sict.haui.edu.vn/vn/tin-tuc"]
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            calls: list[str] = []
            s = snap.Snapshot(out, delay=0.0, max_pages=None,
                              transport=httpx.MockTransport(_handler(calls)))
            s.db.execute("INSERT INTO pages(url, depth) VALUES(?, 1)", (ARTICLE,))
            s.db.execute("INSERT OR IGNORE INTO pages(url, depth) VALUES(?, 1)", (REGULATION,))
            s.db.commit()
            summary = asyncio.run(s.run())

            pages = [json.loads(x) for x in (out / "pages.jsonl").read_text().splitlines()]
            art = next(p for p in pages if p["url"] == ARTICLE)
            assert art["page_type"] == "content" and not art["content_fallback"]
            assert art["images"], "ảnh trong bài phải được ghi nhận"
            assert not any("mpIyV4xFf2o" in v for v in art["videos"])  # video sidebar
            lst = next(p for p in pages if p["url"].endswith("/vn/tin-tuc"))
            assert lst["page_type"] == "listing" and not lst["images"]
            assert (out / art["html_path"]).exists()
            imgs = [json.loads(x) for x in (out / "images.jsonl").read_text().splitlines()]
            assert imgs and all((out / i["path"]).exists() for i in imgs)
            assert summary["unique_images"] == 1  # mọi ảnh mock cùng bytes → 1 file
            assert summary["pages"].get("gone", 0) > 0  # link 404 không làm hỏng lượt chạy
            files = [json.loads(x) for x in (out / "files.jsonl").read_text().splitlines()]
            assert len(files) >= 10 and all((out / f["path"]).exists() for f in files)
            assert any("Quy chế đào tạo" in pg["desc"] for f in files for pg in f["pages"])

            # resume: chạy lại không tải lại gì
            before = len(calls)
            s2 = snap.Snapshot(out, delay=0.0, max_pages=None,
                               transport=httpx.MockTransport(_handler(calls)))
            asyncio.run(s2.run())
            assert len(calls) == before
    finally:
        seeds[:] = old


def test_normalize_state_merges_scheme_and_slash_duplicates():
    with tempfile.TemporaryDirectory() as d:
        s = snap.Snapshot(Path(d), delay=0.0, max_pages=None)
        rows = [("http://sict.haui.edu.vn/vn/html/quy-che", "done"),
                ("https://sict.haui.edu.vn/vn/html/quy-che", "pending"),
                ("http://sict.haui.edu.vn/vn/thong-bao/", "pending"),
                ("https://sict.haui.edu.vn/vn/tin-tuc", "pending")]
        for u, st in rows:
            s.db.execute("INSERT INTO pages(url, depth, status) VALUES(?, 0, ?)", (u, st))
        s.db.commit()
        assert s.normalize_state() >= 2
        got = dict(s.db.execute("SELECT url, status FROM pages"))
        assert got["http://sict.haui.edu.vn/vn/html/quy-che"] == "done"       # giữ bản đã tải
        assert got["https://sict.haui.edu.vn/vn/html/quy-che"] == "duplicate"
        assert got["https://sict.haui.edu.vn/vn/thong-bao"] == "pending"      # đổi về dạng chuẩn
        assert "http://sict.haui.edu.vn/vn/thong-bao/" not in got
        assert s.normalize_state() == 0                                        # idempotent


def test_dead_host_short_circuit():
    with tempfile.TemporaryDirectory() as d:
        s = snap.Snapshot(Path(d), delay=0.0, max_pages=None)
        urls = [f"http://fit-haui.edu.vn/pic/{i}.jpg" for i in range(20)] + ["https://sict.haui.edu.vn/media/1.jpg"]
        for u in urls:
            s.db.execute("INSERT INTO media(url) VALUES(?)", (u,))
        s.db.commit()
        for u in urls[:snap.HOST_DEAD_AFTER]:
            s._retry_or_fail("media", u, 0, "ConnectTimeout: ")
        got = dict(s.db.execute("SELECT url, status FROM media"))
        assert all(got[u] == "failed" for u in urls[snap.HOST_DEAD_AFTER:20])  # bỏ phần còn lại của host chết
        assert got["https://sict.haui.edu.vn/media/1.jpg"] == "pending"        # host khác không bị ảnh hưởng


if __name__ == "__main__":
    test_url_helpers()
    test_scope_sict_only_and_tags_skipped()
    test_mini_crawl_export_and_resume()
    test_normalize_state_merges_scheme_and_slash_duplicates()
    print("OK — snapshot")
