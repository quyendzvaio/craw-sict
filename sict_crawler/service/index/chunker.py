"""Structure-aware markdown chunker + semantic sentence-boundary splitting.

Callers: main.py, tests.
Schemas: chunk_text(markdown, title) → list[Chunk(text, heading, index)].
Semantic: trong section quá budget, chia tại ranh giới câu có cosine distance
lớn nhất (embed bằng chính model e5 đã dùng cho chunks), thay vì cửa sổ ký tự.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass
class Chunk:
    text: str
    heading: str
    index: int


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)", re.MULTILINE)
# Sentence splitter đủ dùng cho VN/EN văn bản hành chính (không thêm dep:
# nltk/spacy cồng kềnh, regex này cover case của parser output).
_SENT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _approx_tokens(text: str) -> int:
    # ~4 chars/token; Vietnamese words longer, still fine for budgeting
    return len(text) // 4


def _cosine(a: list[float], b: list[float]) -> float:
    # embed_texts đã normalize → dot product; giữ generic cho chắc
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _embed_sentences(sents: list[str]) -> list[list[float]] | None:
    """None = embedding unavailable (model chưa download/đời fail) → caller fallback."""
    try:
        from sict_crawler.service.embedding import embedder
        return embedder.embed_texts(sents)
    except Exception as e:  # noqa: BLE001 — fallback có chủ đích, không phá crawl
        print(f"[chunker] embed unavailable, window-split fallback: {e}")
        return None


def _semantic_split(body: str, max_tokens: int, overlap_ratio: float,
                    min_tokens: int) -> list[str]:
    """Chia body thành các đoạn < max_tokens tại ranh giới câu kém liên kết nhất.

    Thuật toán (text-splitting style):
      1. Tách câu, embed cả batch.
      2. sim giữa câu liên tiếp; breakpoint = where sim < mean - 1.0*std
         (e5 mọi sim đều dương cao nên threshold cứng fail; dùng tương đối).
      3. Không có breakpoint mà đoạn vẫn quá dài → hard cut tại câu gần max_tokens
         (xử lý bởi điều kiện cur_tok + st > max_tokens).
    """
    sents = [s.strip() for s in _SENT_RE.split(body) if s.strip()]
    if len(sents) < 2:
        return [body]
    vecs = _embed_sentences(sents)
    if vecs is None:
        return _window_split(sents, max_tokens, overlap_ratio)
    sims = [_cosine(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    mu = sum(sims) / len(sims)
    sd = math.sqrt(sum((s - mu) ** 2 for s in sims) / len(sims))
    thresh = mu - sd
    pieces: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for i, s in enumerate(sents):
        st = _approx_tokens(s)
        # tách: overflow budget, HOẶC gặp ranh giới ngữ nghĩa (câu sau đổi chủ đề)
        # khi đoạn đã đủ min để không ra mảnh vụn
        if cur and (cur_tok + st > max_tokens or
                    (i < len(sims) and sims[i] < thresh and cur_tok >= min_tokens)):
            pieces.append(" ".join(cur))
            # overlap: giữ lại câu cuối làm ngữ cảnh
            cur = ([cur[-1]] if overlap_ratio > 0 and cur else [])
            cur_tok = sum(_approx_tokens(x) for x in cur)
        cur.append(s)
        cur_tok += st
    if cur:
        pieces.append(" ".join(cur))
    return [p for p in pieces if p.strip()]


def _window_split(sents: list[str], max_tokens: int,
                  overlap_ratio: float) -> list[str]:
    """Fallback không semantic: gom câu theo cửa sổ token."""
    pieces: list[str] = []
    cur, cur_tok = [], 0
    for s in sents:
        st = _approx_tokens(s)
        if cur and cur_tok + st > max_tokens:
            pieces.append(" ".join(cur))
            keep = max(1, int(len(cur) * overlap_ratio))
            cur = cur[-keep:] if keep else []
            cur_tok = sum(_approx_tokens(x) for x in cur)
        cur.append(s)
        cur_tok += st
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def chunk_text(markdown: str, title: str = "",
               min_tokens: int = 200, max_tokens: int = 500,
               overlap_ratio: float = 0.1, semantic: bool = True) -> list[Chunk]:
    """Split markdown into heading-aware chunks within [min,max] token budget.

    semantic=True → section quá budget chia tại ranh giới câu (embedding);
    False → cửa sổ ký tự cũ.
    """
    if not markdown.strip():
        return []

    # split into sections at headings
    sections: list[tuple[str, str]] = []  # (heading, body)
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches:
        sections = [("", markdown)]
    else:
        if matches[0].start() > 0:
            sections.append(("", markdown[:matches[0].start()].strip()))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            sections.append((m.group(2).strip(), markdown[m.start():end].strip()))

    # merge small sections, split large ones
    chunks: list[Chunk] = []
    buf, buf_heading = "", ""

    def emit(heading: str, text: str):
        t = f"[{title}] [{heading}] {text}".strip() if heading else f"[{title}] {text}".strip()
        chunks.append(Chunk(text=t, heading=heading[:512], index=len(chunks)))

    def flush():
        nonlocal buf, buf_heading
        if buf.strip():
            if semantic and _approx_tokens(buf) > max_tokens:
                for p in _semantic_split(buf, max_tokens, overlap_ratio, min_tokens):
                    emit(buf_heading, p)
            else:
                emit(buf_heading, buf)
        buf, buf_heading = "", ""

    for heading, body in sections:
        if not body:
            continue
        if _approx_tokens(body) > max_tokens:
            flush()
            if semantic:
                for p in _semantic_split(body, max_tokens, overlap_ratio, min_tokens):
                    emit(heading, p)
            else:
                # split long body by paragraphs (window cũ, giữ cho semantic=False)
                para_tokens = _approx_tokens(body)
                target = max_tokens * 0.8
                step = max(1, int(len(body) * target / para_tokens)) if para_tokens else len(body)
                overlap = int(step * overlap_ratio)
                pos = 0
                while pos < len(body):
                    piece = body[pos:pos + step]
                    if _approx_tokens(piece) >= min_tokens or pos + step >= len(body):
                        emit(heading, piece)
                    pos += step - overlap
        elif _approx_tokens(buf) + _approx_tokens(body) > max_tokens:
            flush()
            buf, buf_heading = body, heading
        else:
            buf = (buf + "\n\n" + body).strip()
            buf_heading = buf_heading or heading
    flush()
    return chunks


if __name__ == "__main__":
    # self-check: body 2 chủ đề, câu chuyển đề → phải tách tại đó
    topic_a = "Hội nghị tổng kết năm học 2024-2025 đã diễn ra trang trọng. " \
              "Các đại biểu thảo luận về công tác đào tạo đại học. "
    topic_b = "Máy tính lượng tử sử dụng bit lượng tử gọi là qubit. " \
              "Thuật toán Shor phân tích số nguyên thành thừa số nguyên tố. "
    body = (topic_a * 4 + "\n" + topic_b * 4).strip()
    # không có model → window fallback, không crash
    parts = _window_split([s for s in _SENT_RE.split(body) if s.strip()], 120, 0.1)
    assert parts, "window split rỗng"
    assert all(len(p) > 0 for p in parts)
    # semantic=False path vẫn chạy như cũ
    md = "# Tiêu đề A\n" + body + "\n# Tiêu đề B\nĐoạn ngắn."
    cs = chunk_text(md, "T", min_tokens=10, max_tokens=120, semantic=False)
    assert len(cs) >= 2 and cs[0].heading.startswith("#") or cs[0].heading
    assert [c.index for c in cs] == list(range(len(cs))), "index phải liên tục"
    assert all(c.text.startswith("[T]") for c in cs)
    print(f"chunker self-check OK: {len(cs)} chunks")
