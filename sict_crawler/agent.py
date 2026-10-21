"""Agentic RAG loop: retrieve → judge context → adapt retrieval → answer.

Thay multi-retrieval mù (luôn trả những gì tìm được) bằng vòng lặp có kiểm tra:
  attempt 1: retrieve chuẩn (E5+CLIP, top_k)
  judge (LLM nhỏ, nhanh): context đủ trả lời? nếu chưa →
  attempt 2: top_k x2 + nới media
  attempt 3: fallback từ vựng (keyword scroll, không cần vector)
  cuối: trả context tốt nhất + cờ sufficient (caller tự thêm ghi chú
  "tài liệu hạn chế" khi insufficient).

Không có LLM key → bỏ judge, 1 attempt duy nhất (hành vi cũ).
Callers: api.py (/chat SSE + /query).
"""
from __future__ import annotations

import json

MAX_ATTEMPTS = 3
JUDGE_MAX_TOKENS = 120


def judge_context(question: str, context: str) -> dict:
    """LLM chấm context: {sufficient: bool, reason: str}.

    Không key / call fail → {"sufficient": True} để giữ hành vi cũ
    (không chặn response khi thiếu judge).
    """
    from . import vlm
    from .config import get_settings

    if not get_settings().llm.api_key:
        return {"sufficient": True, "reason": "no-judge (no key)"}
    if not (context or "").strip():
        return {"sufficient": False, "reason": "empty context"}
    prompt = (
        "Bạn kiểm tra chất lượng ngữ cảnh cho hệ hỏi-đáp. "
        "Câu hỏi: " + question[:300] + "\n"
        "Ngữ cảnh:\n" + context[:2500] + "\n"
        "Ngữ cảnh có chứa thông tin TRỰC TIẾP trả lời câu hỏi không "
        "(không tính nội dung chung chung)? "
        'Chỉ trả JSON: {"sufficient": true/false, "reason": "ngắn gọn"}'
    )
    try:
        raw = vlm._text_call(get_settings().llm.model, prompt)
        obj = json.JSONDecoder().raw_decode(raw[raw.find("{"):] if "{" in raw else raw)[0]
        suf = obj.get("sufficient")
        if isinstance(suf, str):
            suf = suf.lower() in ("true", "1", "yes", "có", "du")
        return {"sufficient": bool(suf), "reason": str(obj.get("reason", ""))[:200]}
    except Exception:
        return {"sufficient": True, "reason": "judge-parse-fail"}


def _unpack(res) -> tuple[list, list, object]:
    """retrieve → (text, media[, route]). Hỗ trợ cả bản cũ trả 2 phần tử (test/mock)."""
    return res[0], res[1], (res[2] if len(res) > 2 else None)


def run(question: str, top_k: int = 5,
        query_image_b64: str | None = None, filters: dict | None = None) -> dict:
    """Chạy loop agentic. Trả {text, media, context, attempts, sufficient, log, media_mode,
    wants_file, media_note}."""
    from . import api as _api
    from . import router as _router

    log: list[str] = []
    tsrcs, msrcs, route = [], [], None
    judge_q = question.strip() or "(người dùng chỉ gửi một ảnh, không kèm câu hỏi)"   # D21 truy vấn chỉ có ảnh
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt == 1:
            tsrcs, msrcs, route = _unpack(_api.retrieve(question, top_k, query_image_b64, filters))
            log.append(f"attempt1: top_k={top_k}")
        elif attempt == 2:
            tsrcs, msrcs, r2 = _unpack(_api.retrieve(question, top_k * 2, query_image_b64, filters))
            route = r2 or route
            log.append(f"attempt2: top_k={top_k * 2} (mở rộng)")
        else:
            if not question.strip():
                break   # chỉ có ảnh: không có từ khoá để tìm bằng nhánh lexical
            route = route or _router.classify_rules(question)
            tsrcs, msrcs = _lexical_fallback(question, top_k * 2, filters, route)
            log.append("attempt3: lexical fallback")
        ctx = _api.build_context(tsrcs, msrcs, getattr(route, "facts", None))
        verdict = judge_context(judge_q, ctx)
        log.append(f"judge{attempt}: {verdict}")
        if verdict.get("sufficient"):
            return _result(tsrcs, msrcs, ctx, attempt, True, log, route)
    ctx = _api.build_context(tsrcs, msrcs, getattr(route, "facts", None))
    return _result(tsrcs, msrcs, ctx, MAX_ATTEMPTS, False, log, route)


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
            "graph_facts": list(getattr(route, "facts", []) or [])}


def _lexical_fallback(question: str, limit: int, filters: dict | None = None, route=None):
    """Không dense: nhánh keyword (BM25 sparse ở v2, MatchText ở v1) cho chữ + caption media.
    Luôn trả lists (có thể rỗng)."""
    from . import api as _api
    from . import query_rewriter

    from . import filters as F
    terms = query_rewriter._terms(question)[:5]
    qf = F.to_qdrant(F.from_request(filters))
    try:
        from . import vector_store
        hits = vector_store.keyword_search(question, terms, limit=limit * 2, query_filter=qf)
    except Exception:
        hits = []
    # tách text thuần vs media_caption (giữ quy ước per-space của api)
    uniq = query_rewriter.dedupe_hits(hits)
    texts = [h for h in uniq if h.get("kind") != "media_caption"][:limit]
    caps = [h for h in uniq if h.get("kind") == "media_caption"][:limit]
    # router: none → không media; optional → chỉ caption thuộc bài chữ đứng đầu, tối đa 3
    if route is not None:
        from . import router as _router
        from .config import get_settings
        rc = get_settings().router
        caps = _router.gate(route, texts[:rc.optional_top_docs],
                            [{**c, "modality": "caption_kw", "raw_score": None} for c in caps],
                            limit, rc.clip_min, rc.caption_min, rc.optional_max)
    return _api.text_sources(texts), _api.media_sources(
        [dict(c, kind=c.get("media_kind", "image")) for c in caps])
