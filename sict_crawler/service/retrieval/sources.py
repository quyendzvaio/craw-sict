"""Hit Qdrant → Source (chữ / media) và ngữ cảnh đánh số [1]…[n] cho LLM (D24)."""
from __future__ import annotations

import re

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


def build_context(tsrcs: list[Source], msrcs: list[Source],
                  source_chars: int | None = None, context_chars: int | None = None,
                  media_first: bool = False) -> str:
    """D27: mỗi nguồn đưa nội dung đầy đủ (`content`, rỗng ở bản cache cũ → excerpt), cắt ở min(source_chars,
    context_chars / số nguồn) — chia đều để nguồn cuối không bị cắt mất (câu hỏi liệt kê có tới 10 nguồn).
    media_first (2026-11-12): khối ảnh/video xếp TRƯỚC nguồn chữ (câu đòi ảnh/video) — RAGAS 11-11: context_precision
    nhóm 8 câu đòi media 0,37 → 0,80 vì giám khảo coi unit đứng đầu quan trọng hơn; số [n] KHÔNG đổi (text vẫn
    [1..n], media [n+1..]) nên trích dẫn LLM và citations phía server giữ nguyên."""
    cfg = get_settings().llm
    srcs = tsrcs + msrcs
    per = source_chars or cfg.source_chars
    if srcs:
        per = max(MIN_SOURCE_CHARS, min(per, (context_chars or cfg.context_chars) // len(srcs)))
    # D24: nguồn đánh số [1]…[n] (chữ trước, media sau) — LLM trích dẫn bằng [n], citations.enforce kiểm lại
    text_parts = [f"[{i}] ({s.kind}) {s.cite or s.title or s.url}{_date_tag(s)}\n{(s.content or s.excerpt)[:per]}"
                  for i, s in enumerate(tsrcs, 1)]
    media_parts = _media_parts(msrcs, len(tsrcs) + 1, per)
    parts = media_parts + text_parts if media_first else text_parts + media_parts
    return "\n\n---\n\n".join(parts)


# Phần đầu caption lặp lại header bài (doc_rules.image_caption_text: "Tiêu đề | Mục | dd/mm/yyyy. alt. OCR")
_CAPTION_HEAD = re.compile(r"(?:\s*\|\s*[^|.]+){0,2}\.\s*")


def media_text(s: Source) -> str:
    """Chữ riêng của một ảnh/video: caption bỏ header bài và alt trùng tiêu đề (còn alt khác, OCR, phụ đề)."""
    t = (s.content or s.excerpt or "").strip()
    title = (s.title or "").strip()
    if not title or not t.startswith(title):
        return t
    t = t[len(title):]
    m = _CAPTION_HEAD.match(t)
    if m:
        t = t[m.end():]
    while title and t.startswith(title):            # alt = tiêu đề bài (site đặt alt như vậy)
        t = t[len(title):].lstrip(" .")
    return t.strip(" .|")


def _media_parts(msrcs: list[Source], start: int, per: int) -> list[str]:
    """Ảnh/video gom theo bài (2026-11-04): mỗi bài một khối "tên bài + ngày", mỗi ảnh một dòng [n] chỉ với chữ
    riêng của nó, gắn nhãn là chữ máy đọc (OCR / phụ đề, có thể sai). Trước đây mỗi ảnh lặp tiêu đề bài 3 lần,
    chữ OCR trông như chữ của bài: 6 ảnh của một bài → LLM mô tả 6 ý gần giống nhau và chép nguyên link OCR
    đọc sai ("tracuu.euni.hqui.edu.vn"). Số [n] giữ nguyên thứ tự msrcs; khối theo thứ tự ảnh đầu tiên của bài."""
    groups: dict[str, list[tuple[int, Source]]] = {}
    for i, s in enumerate(msrcs, start):
        groups.setdefault(s.parent_url or s.cite or s.title or s.url, []).append((i, s))
    parts = []
    for items in groups.values():
        first = items[0][1]
        # Tên bài (không kèm mục: cite "Bài › Mục:" + "(ngày)" làm model tưởng không có ngày đăng, g064) + "đăng ngày"
        head = [f"Bài: {first.title or first.cite or first.url}"]
        if _date_tag(first):
            head.append("đăng ngày " + _date_tag(first).strip(" ()"))
        counts: dict[str, int] = {}
        for _, s in items:
            counts[_kind_vi(s.kind)] = counts.get(_kind_vi(s.kind), 0) + 1
        head.append(", ".join(f"{c} {k}" for k, c in counts.items()))
        lines = [" — ".join(head)]
        for i, s in items:
            text = media_text(s)[:per]
            label = "phụ đề / chữ trong video (máy đọc, có thể sai)" if _kind_vi(s.kind) == "video" \
                else "chữ đọc từ ảnh (OCR, có thể sai)"
            lines.append(f"[{i}] ({s.kind}) {label}: {text}" if text else f"[{i}] ({s.kind}) không có chữ đọc được")
        parts.append("\n".join(lines))
    return parts


def _kind_vi(kind: str) -> str:
    return "video" if (kind or "").startswith("video") else "ảnh"
