"""Prompt caching hai tầng (T1: route decision, T2: câu trả lời sinh).

spec: docs/prompt_caching_multi_tier.md
Default tắt (PROMPT_CACHE_ENABLED=false); bật bằng biến môi trường riêng — không ảnh hưởng
latency production khi tắt.

T1: cache RouteDecision cho hybrid_router.route() — hit thì bỏ qua lượt LLM router
    (luật vẫn chạy và policy_merge vẫn bảo vệ, không bao giờ hạ quyết định luật).
T2: cache câu trả lời LLM (stream_answer_cached) — hit thì stream lại từ cache, bỏ qua
    lượt gọi LLM; caller (routes.py, graph.py) vẫn chạy citations.enforce như thường.

Key chứa đầy đủ ngữ cảnh phân biệt để không phục vụ kết quả sai khi đổi model / policy /
ngữ cảnh. Hai lệch có chủ đích so với công thức trong spec (đều theo nguyên tắc bao trùm
"key chứa đủ ngữ cảnh phân biệt" của chính spec):
  - câu hỏi GIỮ NGUYÊN dấu, không fold: "tiếng Anh" và "ảnh" cùng fold thành "anh" nhưng
    route phân biệt hai trường hợp này (xem comment khóa rr:v3 trong hybrid_router.py);
  - key T2 dùng hash NGỮ CẢNH (sha256(context)[:16]) thay vì len(số nguồn): cùng câu hỏi
    với filters khác → ngữ cảnh khác → không được trả answer cũ.
Chỉ dùng stdlib + hashlib/json + repository.cache (Redis db 3 best-effort đã có).
"""
from __future__ import annotations

import hashlib
import json
import logging

from sict_crawler.core.config import get_settings
from sict_crawler.repository import cache

log = logging.getLogger("sict.prompt_cache")

PROMPT_CACHE_VERSION = "pc:v1"
# Giữ đồng bộ với ROUTE_POLICY_VERSION trong hybrid_router.py (import vòng nên giữ hằng ở đây)
_ROUTE_POLICY_VERSION = "intent-v5"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ── T1: Route cache ────────────────────────────────────────────────────────────

def route_cache_key(question: str, image_hash: str, query_key: str) -> str:
    """Key T1 route. Thay đổi khi: câu hỏi, ảnh (flag), model router, policy version, query_key."""
    cfg = get_settings().llm
    router_model = cfg.router_model or cfg.model
    parts = [
        question,               # giữ dấu — "tiếng Anh" ≠ "ảnh" (xem docstring)
        image_hash,
        router_model,
        _ROUTE_POLICY_VERSION,
        query_key,
    ]
    return f"{PROMPT_CACHE_VERSION}:route:{_sha256('|'.join(parts))}"


def get_route(question: str, image_hash: str, query_key: str) -> dict | None:
    """Lấy RouteDecision từ T1 cache. None nếu tắt, miss, hoặc lỗi Redis."""
    cfg = get_settings().llm
    if not cfg.prompt_cache_enabled:
        return None
    key = route_cache_key(question, image_hash, query_key)
    raw = cache.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        log.debug("route cache deserialize failed: key=%s", key)
        return None


def put_route(question: str, image_hash: str, query_key: str, route_dict: dict) -> None:
    """Lưu RouteDecision vào T1 cache. No-op nếu tắt hoặc lỗi Redis."""
    cfg = get_settings().llm
    if not cfg.prompt_cache_enabled:
        return
    key = route_cache_key(question, image_hash, query_key)
    try:
        cache.put(key, json.dumps(route_dict, ensure_ascii=False).encode(), cfg.prompt_cache_route_ttl)
    except Exception:
        log.debug("route cache put failed: key=%s", key)


# ── T2: Prompt / câu trả lời sinh ─────────────────────────────────────────────

def prompt_cache_key(question: str, image_hash: str, context: str, index_fingerprint: str) -> str:
    """Key T2 prompt. Thay đổi khi: câu hỏi, ảnh, model, max_tokens, ngữ cảnh, index, template version."""
    cfg = get_settings().llm
    parts = [
        question,               # giữ dấu (xem docstring)
        image_hash,
        cfg.model,
        str(cfg.max_new_tokens),
        _sha256(context or "")[:16],   # hash ngữ cảnh: filters khác → ngữ cảnh khác → key khác
        index_fingerprint,
        cfg.prompt_cache_template_version,
    ]
    return f"{PROMPT_CACHE_VERSION}:prompt:{_sha256('|'.join(parts))}"


def get_answer(question: str, image_hash: str, context: str, index_fingerprint: str) -> str | None:
    """Lấy câu trả lời từ T2 cache. None nếu tắt, miss, hoặc lỗi Redis."""
    cfg = get_settings().llm
    if not cfg.prompt_cache_enabled:
        return None
    key = prompt_cache_key(question, image_hash, context, index_fingerprint)
    raw = cache.get(key)
    if raw is None:
        return None
    try:
        return raw.decode()
    except Exception:
        log.debug("prompt cache decode failed: key=%s", key)
        return None


def put_answer(question: str, image_hash: str, context: str, index_fingerprint: str, answer: str) -> None:
    """Lưu câu trả lời vào T2 cache. No-op nếu tắt, câu trả lời rỗng, hoặc lỗi Redis."""
    cfg = get_settings().llm
    if not cfg.prompt_cache_enabled:
        return
    if not answer or not answer.strip():
        return  # không cache câu trả lời rỗng (LLM hỏng → caller tự fallback echo context)
    key = prompt_cache_key(question, image_hash, context, index_fingerprint)
    try:
        cache.put(key, answer.encode(), cfg.prompt_cache_prompt_ttl)
    except Exception:
        log.debug("prompt cache put failed: key=%s", key)


def stream_answer_cached(question: str, context: str, image_b64: str | None = None):
    """Generator bọc vlm.stream_answer với T2 cache — cùng hợp đồng delta cho caller.

    Hit: yield câu trả lời cache chia khúc 80 ký tự (giống fallback echo, UI vẽ mượt).
    Miss: stream LLM thật; khi stream xong không lỗi thì lưu tổng thể vào cache.
    Lỗi giữa stream → không cache (tránh lưu câu trả lời cụt); caller tự fallback như cũ.
    """
    from sict_crawler.service.retrieval import retriever, vlm   # import tại chỗ tránh vòng

    image_hash = _sha256(image_b64 or "")[:16]
    index_fp = retriever.index_tag()
    cached = get_answer(question, image_hash, context, index_fp)
    if cached is not None:
        log.debug("T2 prompt cache HIT: %s", question[:60])
        for offset in range(0, len(cached), 80):
            yield cached[offset:offset + 80]
        return
    pieces: list[str] = []
    ok = False
    try:
        for chunk in vlm.stream_answer(question, context, image_b64):
            pieces.append(chunk)
            yield chunk
        ok = True
    finally:
        if ok:
            put_answer(question, image_hash, context, index_fp, "".join(pieces))
