"""Schema vào/ra của API hỏi đáp (pydantic): câu hỏi và nguồn trả về."""
from __future__ import annotations

from pydantic import BaseModel


class QueryIn(BaseModel):
    question: str
    top_k: int = 8
    image: str | None = None  # base64 (không có tiền tố data:) — truy vấn bằng ảnh (D21); question có thể rỗng
    # lọc cứng metadata: {"sections": [...], "doc_types": [...], "year_from": int, "year_to": int}
    filters: dict | None = None


class Source(BaseModel):
    url: str
    title: str
    kind: str  # text | image | video_frame
    score: float
    excerpt: str = ""
    media_id: int | None = None      # index v1 → GET /media/{media_id}
    media_key: str | None = None     # index v2 (sha256) → GET /media/s/{media_key}
    timestamp: float | None = None
    published_at: str | None = None  # index spec v2 (thiếu ở index cũ)
    section: str | None = None
    parent_url: str | None = None    # file đính kèm → trang chứa nó (url = link tải file)
    via: str | None = None           # D21: nhánh tìm ra nguồn này: text | i2t | i2i-doc | graph | clip | i2i | i2t | caption …
    heading_path: list[str] | None = None   # D23: mục chứa đoạn trích trong bài
    page: int | None = None                 # D24: trang (từ 1) trong file PDF/PPTX; None = không có trang
    cite: str = ""                          # D24: nhãn trích dẫn dựng sẵn (citations.make_cite)
