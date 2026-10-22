"""Phân loại URL / trang của site (dùng chung snapshot, compute, indexer, incremental)."""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Trang lỗi ứng dụng trả 200 (AC-H4) — dò trên markdown vùng nội dung
_ERROR_SIGNS = ("Server Error in", "The parameters dictionary contains a null entry",
                "Runtime Error")
_PAGINATION = re.compile(r"^/vn/[a-z0-9-]+/\d+/?$")
_LISTING = re.compile(r"^/vn/?$|^/vn/(?!html/)[a-z0-9-]+(/\d+)?/?$")
_YOUTUBE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|v/)"
                      r"|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    m = _YOUTUBE.search(url)
    return m.group(1) if m else None


def page_type(url: str) -> str:
    """listing (trang chủ, mục, phân trang) | content (bài, trang html tĩnh)."""
    return "listing" if _LISTING.match(urlparse(url).path) else "content"


def is_error_page(markdown: str) -> bool:
    return any(s in markdown for s in _ERROR_SIGNS) or len(markdown.strip()) < 50


def link_depth(parent_depth: int, link: str) -> int:
    """Phân trang giữ depth trang cha; link khác +1."""
    return parent_depth if _PAGINATION.match(urlparse(link).path) else parent_depth + 1
