"""Hit Qdrant → Source (chữ / media) và ngữ cảnh đánh số [1]…[n] cho LLM (D24)."""
from __future__ import annotations

from sict_crawler.core.config import get_settings
from sict_crawler.model.schemas import Source
from sict_crawler.service.retrieval.citations import make_cite


def _cosine(r: dict) -> float | None:
    """Cosine câu hỏi↔điểm: raw_score (rerank_text) hoặc dense_score (nhánh dense; ở v2/v3 rerank chỉ chấm caption).
    Hit chỉ từ BM25 → None."""
    return r["raw_score"] if r.get("raw_score") is not None else r.get("dense_score")


def text_sources(text_hits: list[dict]) -> list[Source]:
    """E5 hits → Source. media_caption hits carry media refs."""
    sources: list[Source] = []
    for r in text_hits:
        if r.get("kind") == "media_caption":
            sources.append(Source(
                url=r.get("url", ""), title=r.get("title", ""),
                kind=r.get("media_kind", "image"), score=r["score"],
                excerpt=r.get("caption", ""), content=r.get("caption", ""), media_id=r.get("media_id"),
                media_key=r.get("media_key"),
                timestamp=r.get("timestamp"), published_at=r.get("published_at"),
                section=r.get("section"), parent_url=r.get("page_url"), via=r.get("via", "caption"),
                cosine=_cosine(r),
                heading_path=r.get("heading_path") or None,
                cite=make_cite(r.get("title", ""), r.get("page_url") or r.get("url", ""),
                               r.get("media_kind", "image"), None, r.get("heading_path"))))
        else:
            kind = "file" if r.get("doc_type") == "attachment" else "text"
            body = r.get("content") or r.get("text") or ""
            sources.append(Source(url=r["url"], title=r.get("title", ""), kind=kind,
                                  score=r["score"], excerpt=body[:300], content=body, cosine=_cosine(r),
                                  published_at=r.get("published_at"), section=r.get("section"),
                                  parent_url=r.get("parent_url"), via=r.get("via", "text"),
                                  heading_path=r.get("heading_path") or None, page=r.get("page"),
                                  cite=make_cite(r.get("title", ""), r["url"], kind, r.get("page"),
                                                 r.get("heading_path"))))
    return sources


def _first(v):
    return v[0] if isinstance(v, list) and v else (None if isinstance(v, list) else v)


def media_sources(image_hits: list[dict]) -> list[Source]:
    """CLIP hits (sict_image) / caption → Source. v2: section/page_urls là danh sách (ảnh dùng
    chung nhiều bài) → lấy bài đầu; video caption → kind video, media_key = khung hình đầu."""
    return [Source(
        url=r.get("url", ""), title=r.get("title", ""), kind=r.get("kind", "image"),
        score=r["score"], excerpt=r.get("caption", r.get("s3_key", "")), content=r.get("caption") or "",
        media_id=r.get("media_id"), media_key=r.get("media_key"), timestamp=r.get("timestamp"),
        published_at=r.get("published_at"), section=_first(r.get("section")),
        parent_url=r.get("page_url") or _first(r.get("page_urls")), via=r.get("modality"),
        heading_path=r.get("heading_path") or None,
        cite=make_cite(r.get("title", ""), r.get("page_url") or _first(r.get("page_urls")) or r.get("url", ""),
                       r.get("kind", "image"), None, r.get("heading_path")))
        for r in image_hits]


def _date_tag(s: Source) -> str:
    """' (11/09/2026)' khi có ngày đăng — LLM cần ngày để trả lời câu hỏi thời gian."""
    if not s.published_at:
        return ""
    p = s.published_at[:10].split("-")
    return f" ({p[2]}/{p[1]}/{p[0]})" if len(p) == 3 else ""


MIN_SOURCE_CHARS = 400   # nhiều nguồn: mỗi nguồn vẫn giữ ít nhất chừng này ký tự


def build_context(tsrcs: list[Source], msrcs: list[Source], facts: list[str] | None = None,
                  source_chars: int | None = None, context_chars: int | None = None) -> str:
    """facts: dữ kiện graph D20 (thực thể trong câu hỏi + thực thể liên quan, số bài làm chứng).

    D27: mỗi nguồn đưa nội dung đầy đủ (`content`, rỗng ở bản cache cũ → excerpt), cắt ở min(source_chars,
    context_chars / số nguồn) — chia đều để nguồn cuối không bị cắt mất (câu hỏi liệt kê có tới 10 nguồn)."""
    cfg = get_settings().llm
    srcs = tsrcs + msrcs
    per = source_chars or cfg.source_chars
    if srcs:
        per = max(MIN_SOURCE_CHARS, min(per, (context_chars or cfg.context_chars) // len(srcs)))
    parts = [f"[graph] {f}" for f in (facts or [])]
    # D24: nguồn đánh số [1]…[n] (chữ trước, media sau) — LLM trích dẫn bằng [n], citations.enforce kiểm lại
    parts += [f"[{i}] ({s.kind}) {s.cite or s.title or s.url}{_date_tag(s)}\n{(s.content or s.excerpt)[:per]}"
              for i, s in enumerate(srcs, 1)]
    return "\n\n---\n\n".join(parts)
