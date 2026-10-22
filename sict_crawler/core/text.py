"""Chuẩn hoá chữ tiếng Việt dùng chung (BM25, router, graph, bộ lọc)."""
import unicodedata


def fold(text: str) -> str:
    """lowercase + bỏ dấu tiếng Việt (đ→d)."""
    s = unicodedata.normalize("NFD", (text or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d")
