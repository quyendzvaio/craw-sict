"""Định tuyến hybrid có cache; chỉ dùng khi được bật riêng sau gate shadow."""
from __future__ import annotations

import hashlib
import json
import logging
import time

from sict_crawler.core.config import get_settings
from sict_crawler.repository import cache
from sict_crawler.service.retrieval import query_rewriter, router, vlm
from sict_crawler.service.retrieval.route_schema import RouteDecision

ROUTE_TTL = 24 * 60 * 60
ROUTE_POLICY_VERSION = 'intent-v3'
log = logging.getLogger('sict.router.middleware')


def _record(question, has_image, rules, proposal, final, origin, started):
    def fields(route):
        return {key: getattr(route, key) for key in ('media', 'wants_file', 'oos')}
    log.warning('middleware_route %s', json.dumps({
        'key': hashlib.sha256((question + '\0' + str(has_image)).encode()).hexdigest(),
        'rules': fields(rules), 'llm': proposal, 'final': fields(final),
        'origin': origin, 'policy': ROUTE_POLICY_VERSION,
        'seconds': round(time.monotonic() - started, 3),
    }, ensure_ascii=False))
    return final


def route(question: str, has_image: bool) -> router.Route:
    started = time.monotonic()
    rules = router.classify_rules(question, has_image=has_image)
    # optional theo chủ đề và câu liệt kê đều có thể là câu hỏi thuần thông tin.
    if not question.strip() or rules.media == 'required':
        return _record(question, has_image, rules, None, rules, 'rules', started)
    cfg = get_settings()
    normalized = question
    if cfg.retrieval.restore_accents:
        from sict_crawler.service.retrieval import accent
        normalized = accent.maybe_restore(question) or question
    normalized = query_rewriter._rule_rewrite(normalized)
    # Giữ dấu trong khóa vì router và LLM phân biệt "anh" với "ảnh".
    model = cfg.llm.router_model or cfg.llm.model
    identity = (cfg.llm.api_base, model, normalized, has_image, ROUTE_POLICY_VERSION,
                cfg.llm.router_max_new_tokens or cfg.llm.max_new_tokens, cfg.llm.router_timeout)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    key = 'rr:v3:' + digest
    try:
        raw = cache.get(key)
        data = json.loads(raw) if raw else None
        decision = RouteDecision.model_validate(data).model_dump() if data else None
    except (TypeError, ValueError):
        decision = None
    origin = 'cache' if decision is not None else 'llm'
    if decision is None:
        decision = vlm.route_intent(normalized, has_image)
        if decision is not None:
            try:
                decision = RouteDecision.model_validate(decision).model_dump()
                cache.put(key, json.dumps(decision).encode(), ROUTE_TTL)
            except (TypeError, ValueError):
                decision = None
    final = router.policy_merge(rules, decision, question=question)
    return _record(question, has_image, rules, decision, final,
                   origin if decision is not None else 'fallback', started)
