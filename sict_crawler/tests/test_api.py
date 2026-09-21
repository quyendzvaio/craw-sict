"""Self-check for source mapping (no services needed).

Run: python -m sict_crawler.tests.test_api
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler.api import media_sources, text_sources
from sict_crawler.query_rewriter import _rrf, rewrite


def test_media_caption_hit():
    hits = [{"score": 0.9, "kind": "media_caption", "media_kind": "video_frame",
             "media_id": 42, "url": "http://x/v.mp4", "title": "Thầy A",
             "caption": "Thầy A đang giảng bài", "timestamp": 12.3}]
    srcs = text_sources(hits)
    assert len(srcs) == 1
    s = srcs[0]
    assert s.kind == "video_frame", s.kind
    assert s.media_id == 42
    assert s.timestamp == 12.3
    assert s.excerpt == "Thầy A đang giảng bài"


def test_text_hit():
    hits = [{"score": 0.8, "url": "http://x/p", "title": "Trang",
             "content": "nội dung" * 100}]
    srcs = text_sources(hits)
    s = srcs[0]
    assert s.kind == "text"
    assert s.media_id is None and s.timestamp is None
    assert len(s.excerpt) == 300


def test_image_hit():
    img = [{"score": 0.7, "url": "http://x/v.mp4", "kind": "video_frame",
            "media_id": 7, "s3_key": "media/abc.png", "timestamp": 3.5}]
    srcs = media_sources(img)
    s = srcs[0]
    assert s.kind == "video_frame" and s.media_id == 7 and s.timestamp == 3.5


def test_no_cross_group_mixing():
    """Per-space mapping keeps groups separate — no merged sort."""
    t = text_sources([{"score": 0.5, "kind": "media_caption", "media_kind": "image",
                       "media_id": 1, "url": "u", "title": "t", "caption": "c"}])
    m = media_sources([{"score": 0.9, "url": "u", "kind": "image",
                        "media_id": 2, "s3_key": "k"}])
    assert len(t) == 1 and len(m) == 1
    assert t[0].score == 0.5 and m[0].score == 0.9


def test_rewrite_expands_abbrev():
    r = rewrite("Ngành CNTT học phí thế nào")
    assert "công nghệ thông tin" in r["rewritten"].lower()
    assert "hoc" in r["terms"]  # accent-stripped
    r2 = rewrite("Ngành KHMT tuyển sinh bao nhiêu?")
    assert "khoa học máy tính" in r2["rewritten"].lower()


def test_rrf_fuses_lists():
    a = [{"url": "u1", "chunk_id": "1"}, {"url": "u2", "chunk_id": "2"}]
    b = [{"url": "u2", "chunk_id": "2"}, {"url": "u3", "chunk_id": "3"}]
    fused = _rrf(a, b)
    # u2 appears in both lists → ranked first
    assert fused[0]["url"] == "u2"
    assert len(fused) == 3


if __name__ == "__main__":
    test_media_caption_hit()
    print("OK — media_caption → Source")
    test_text_hit()
    print("OK — text hit")
    test_image_hit()
    print("OK — image hit carries media_id/timestamp")
    test_no_cross_group_mixing()
    print("OK — groups stay separate, no merged sort")
    test_rewrite_expands_abbrev()
    print("OK — rewriter expands abbreviations, strips accents")
    test_rrf_fuses_lists()
    print("OK — RRF fuses dense + keyword legs")
