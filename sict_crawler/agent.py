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


def run(question: str, top_k: int = 5,
        query_image_b64: str | None = None) -> dict:
    """Chạy loop agentic. Trả {text, media, context, attempts, sufficient, log}."""
    from . import api as _api

    log: list[str] = []
    tsrcs, msrcs = [], []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt == 1:
            tsrcs, msrcs = _api.retrieve(question, top_k, query_image_b64)
            log.append(f"attempt1: top_k={top_k}")
        elif attempt == 2:
            tsrcs, msrcs = _api.retrieve(question, top_k * 2, query_image_b64)
            log.append(f"attempt2: top_k={top_k * 2} (mở rộng)")
        else:
            tsrcs, msrcs = _lexical_fallback(question, top_k * 2)
            log.append("attempt3: lexical fallback")
        ctx = _api.build_context(tsrcs, msrcs)
        verdict = judge_context(question, ctx)
        log.append(f"judge{attempt}: {verdict}")
        if verdict.get("sufficient"):
            return {"text": tsrcs, "media": msrcs, "context": ctx,
                    "attempts": attempt, "sufficient": True, "log": log}
    ctx = _api.build_context(tsrcs, msrcs)
    return {"text": tsrcs, "media": msrcs, "context": ctx,
            "attempts": MAX_ATTEMPTS, "sufficient": False, "log": log}


def _lexical_fallback(question: str, limit: int):
    """Không vector: keyword scroll text + caption media. Luôn trả lists (có thể rỗng)."""
    from . import api as _api
    from . import query_rewriter

    terms = query_rewriter._terms(question)[:5]
    hits: list[dict] = []
    for term in terms:
        try:
            from . import vector_store
            hits += vector_store.scroll_text_by_content(term, limit=limit)
        except Exception:
            pass
    # tách text thuần vs media_caption (giữ quy ước per-space của api)
    seen: dict = {}
    for h in hits:
        key = (h.get("url"), h.get("chunk_id", h.get("media_id")))
        seen.setdefault(key, h)
    uniq = list(seen.values())
    texts = [h for h in uniq if h.get("kind") != "media_caption"][:limit]
    caps = [h for h in uniq if h.get("kind") == "media_caption"][:limit]
    return _api.text_sources(texts), _api.media_sources(
        [dict(c, kind=c.get("media_kind", "image")) for c in caps])
