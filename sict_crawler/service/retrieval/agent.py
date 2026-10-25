"""RAG loop: retrieve → ngữ cảnh → cờ "đủ bằng chứng" (D27: không phụ thuộc LLM).

  1 lượt truy xuất (retriever.retrieve: câu gốc + câu LLM viết lại, đa dạng bài, câu liệt kê lấy rộng);
  không ra hit nào → fallback từ vựng (BM25, không cần vector);
  `sufficient` = luật xác định (`evidence`): có nguồn, cosine câu hỏi↔đoạn tốt nhất ≥ retrieval.min_cosine, tỷ lệ từ
  nội dung của câu hỏi có trong ngữ cảnh ≥ retrieval.min_term_cover. Caller thêm ghi chú "tài liệu hạn chế" khi false.

Trước D27 một LLM chấm ngữ cảnh rồi thử lại tối đa 3 lượt: nó không thấy phần không tìm được (chấm "đủ" khi danh sách
thiếu), chấm "đủ" cả câu ngoài phạm vi, đổi theo model và tốn 1–3 lượt gọi LLM / câu (eval/list_v1, 2026-10-25).
Callers: api/routes.py (/chat SSE + /query).
"""
from __future__ import annotations

from sict_crawler.core import sparse
from sict_crawler.core.config import get_settings
from sict_crawler.service.retrieval import retriever, sources

# hư từ / từ hỏi (đã bỏ dấu) — không tính vào độ phủ từ của câu hỏi
_STOP = frozenset("""
a ai bao bi biet cac cach cai cho chu co cua da dang de den deu do duoc gi gom hay hoac hoi khi khong la lam luc
ma minh mot muon nao nay neu nhe nhieu nhu nhung noi o oi ra roi sao se tai the thi thoi toi trong tu va ve vi voi xin
xem em anh chi ban hien duoc nay ay kia
""".split())


def question_terms(question: str) -> list[str]:
    """Từ nội dung của câu hỏi (token BM25 bỏ dấu, bỏ hư từ), giữ thứ tự, không trùng."""
    return list(dict.fromkeys(t for t in sparse.tokens(question) if t not in _STOP))


def evidence(question: str, tsrcs: list, msrcs: list, context: str) -> dict:
    """Cờ "đủ bằng chứng" bằng luật (thay LLM chấm, D27) → {sufficient, cosine, cover, reason}.

    Chỉ là tín hiệu thô (không biết danh sách còn thiếu mục nào — không bộ chấm nào biết khi chỉ thấy ngữ cảnh), nhưng
    ổn định, đo được và không đổi theo LLM. Truy vấn chỉ có ảnh: đủ khi có nguồn."""
    if not (tsrcs or msrcs):
        return {"sufficient": False, "cosine": None, "cover": 0.0, "reason": "không có nguồn"}
    if not question.strip():
        return {"sufficient": True, "cosine": None, "cover": None, "reason": "chỉ ảnh, có nguồn"}
    cfg = get_settings().retrieval
    cos = max((s.cosine for s in tsrcs + msrcs if s.cosine is not None), default=None)
    terms = question_terms(question)
    have = set(sparse.tokens(context))
    cover = round(sum(t in have for t in terms) / len(terms), 3) if terms else 1.0
    low = [x for x, bad in (("cosine", cos is not None and cos < cfg.min_cosine),
                            ("từ khoá", cover < cfg.min_term_cover)) if bad]
    return {"sufficient": not low, "cosine": None if cos is None else round(cos, 3), "cover": cover,
            "reason": ("thấp: " + ", ".join(low)) if low else "ok"}


def _unpack(res) -> tuple[list, list, object]:
    """retrieve → (text, media[, route]). Hỗ trợ cả bản cũ trả 2 phần tử (test/mock)."""
    return res[0], res[1], (res[2] if len(res) > 2 else None)


def run(question: str, top_k: int = 5,
        query_image_b64: str | None = None, filters: dict | None = None) -> dict:
    """1 lượt truy xuất (+ fallback từ vựng khi rỗng). Trả {text, media, context, attempts, sufficient, log,
    media_mode, wants_file, media_note, list_question, …}."""
    from sict_crawler.service.retrieval import router as _router

    tsrcs, msrcs, route = _unpack(retriever.retrieve(question, top_k, query_image_b64, filters))
    log = [f"attempt1: top_k={top_k}" + (" (câu liệt kê)" if getattr(route, "list_question", False) else "")]
    attempts = 1
    if not (tsrcs or msrcs) and question.strip():
        route = route or _router.classify_rules(question)
        tsrcs, msrcs = _lexical_fallback(question, top_k * 2, filters, route)
        attempts = 2
        log.append("attempt2: lexical fallback (không có hit)")
    ctx = sources.build_context(tsrcs, msrcs, getattr(route, "facts", None))
    ev = evidence(question, tsrcs, msrcs, ctx)
    log.append(f"evidence: {ev}")
    return _result(tsrcs, msrcs, ctx, attempts, ev["sufficient"], log, route)


def _result(tsrcs, msrcs, ctx, attempts, sufficient, log, route) -> dict:
    mode = route.media if route else None
    note = ("Không tìm thấy ảnh/video phù hợp với câu hỏi."
            if mode == "required" and not msrcs else None)  # AC-R9: rỗng + ghi chú, không ảnh sai
    return {"text": tsrcs, "media": msrcs, "context": ctx, "attempts": attempts,
            "sufficient": sufficient, "log": log, "media_mode": mode,
            "wants_file": route.wants_file if route else None, "media_note": note,
            "inputs": list(getattr(route, "inputs", []) or []),
            "outputs": list(getattr(route, "outputs", []) or []),
            "entities": list(getattr(route, "entities", []) or []),
            "list_question": bool(getattr(route, "list_question", False)),
            "graph_facts": list(getattr(route, "facts", []) or [])}


def _lexical_fallback(question: str, limit: int, filters: dict | None = None, route=None):
    """Không dense: nhánh keyword (BM25 sparse ở v2, MatchText ở v1) cho chữ + caption media.
    Luôn trả lists (có thể rỗng)."""
    from sict_crawler.service.retrieval import filters as F
    from sict_crawler.service.retrieval import query_rewriter
    terms = query_rewriter._terms(question)[:5]
    qf = F.to_qdrant(F.from_request(filters))
    try:
        from sict_crawler.repository import vector_store
        hits = vector_store.keyword_search(question, terms, limit=limit * 2, query_filter=qf)
    except Exception:
        hits = []
    # tách text thuần vs media_caption (giữ quy ước per-space của api)
    uniq = query_rewriter.dedupe_hits(hits)
    texts = [h for h in uniq if h.get("kind") != "media_caption"][:limit]
    caps = [h for h in uniq if h.get("kind") == "media_caption"][:limit]
    # router: none → không media; optional → chỉ caption thuộc bài chữ đứng đầu, tối đa 3
    if route is not None:
        from sict_crawler.core.config import get_settings
        from sict_crawler.service.retrieval import router as _router
        rc = get_settings().router
        caps = _router.gate(route, texts[:rc.optional_top_docs],
                            [{**c, "modality": "caption_kw", "raw_score": None} for c in caps],
                            limit, rc.clip_min, rc.caption_min, rc.optional_max)
    return sources.text_sources(texts), sources.media_sources(
        [dict(c, kind=c.get("media_kind", "image")) for c in caps])
