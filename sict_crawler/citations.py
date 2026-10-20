"""Trích dẫn bắt buộc (D24, AC-S8). Hàm thuần — không gọi mạng, dễ kiểm thử.

Ngữ cảnh gửi LLM đánh số nguồn [1]…[n] (api.build_context: nguồn chữ trước, media sau). LLM được yêu cầu kết
mỗi ý bằng [n]; `enforce` kiểm lại phía server: bỏ [n] không tồn tại, và nếu câu trả lời không có [n] hợp lệ
nào thì ghép dòng "Nguồn:" lấy từ các nguồn đứng đầu. Mỗi nguồn có `cite` (nhãn dựng sẵn: "Tên file — trang N"
cho PDF/PPTX, "Tiêu đề › Mục" cho trang web) và `url`.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .indexing import path_str

_MARK = re.compile(r"\[(\d{1,3})\]")
FOOTER_SOURCES = 3      # số nguồn ghép vào dòng "Nguồn:" khi LLM không trích dẫn


def _file_name(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", 1)[-1])


def make_cite(title: str, url: str, kind: str, page: int | None = None,
              heading_path: list[str] | None = None) -> str:
    """Nhãn trích dẫn người đọc kiểm chứng được: file → tên + số trang; trang web → tiêu đề + mục."""
    name = (title or _file_name(url) or url).strip()
    if kind == "file":
        return f"{name} — trang {page}" if page else name
    return f"{name} › {path_str(heading_path)}" if heading_path else name


def used(answer: str, n_sources: int) -> list[int]:
    """[n] hợp lệ (1 ≤ n ≤ số nguồn) theo thứ tự xuất hiện, không lặp."""
    seen: list[int] = []
    for m in _MARK.finditer(answer or ""):
        n = int(m.group(1))
        if 1 <= n <= n_sources and n not in seen:
            seen.append(n)
    return seen


def entry(n: int, s) -> dict:
    """Mục trích dẫn gửi cho client (s: api.Source)."""
    return {"n": n, "url": s.url, "title": s.title, "kind": s.kind, "page": s.page,
            "heading_path": s.heading_path, "cite": s.cite}


def index(sources: list) -> list[dict]:
    """Toàn bộ nguồn đã đánh số (đúng thứ tự trong ngữ cảnh) — cho /query."""
    return [entry(i, s) for i, s in enumerate(sources, 1)]


def enforce(answer: str, sources: list) -> tuple[str, list[dict]]:
    """→ (câu trả lời đã chuẩn hoá, danh sách trích dẫn được dùng). Không có nguồn (ngữ cảnh rỗng) thì không
    ép trích dẫn. [n] ngoài khoảng bị bỏ; không còn [n] hợp lệ → ghép "Nguồn: [1] … — url" cho các nguồn đầu."""
    if not sources:
        return answer, []
    n_src = len(sources)
    answer = _MARK.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_src else "", answer or "")
    ns = used(answer, n_src)
    if not ns:
        ns = list(range(1, min(FOOTER_SOURCES, n_src) + 1))
        lines = [f"[{n}] {sources[n - 1].cite} — {sources[n - 1].url}" for n in ns]
        answer = answer.rstrip() + "\n\nNguồn:\n" + "\n".join(lines)
    return answer, [entry(n, sources[n - 1]) for n in ns]
