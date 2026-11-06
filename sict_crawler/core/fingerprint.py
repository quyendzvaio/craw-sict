"""Fingerprint hợp nhất của index đang phục vụ (đối chiếu kiến trúc 2026-11-06).

Một chuỗi duy nhất trả lời "đổi gì thì phải build lại / cache cũ phải bỏ": spec chunk,
model nhúng + dim, collection đang phục vụ, quy tắc graph. Không thay thế hash_v2 /
hash_struct (hash nội dung) — chỉ hợp nhất các *con số phiên bản* đã có.

Callers: api.metrics (gauge), api.routes + retrieval.agent (provenance), scripts/cutover_v3.sh (manifest).
"""
from __future__ import annotations

import hashlib
import json

from sict_crawler.core.config import get_settings


def _components() -> dict:
    """Các thành phần quyết định index; import lazy để API không kéo nhóm graph/ml khi chỉ đọc config."""
    from sict_crawler.service.graph.concepts import RULES_VERSION
    from sict_crawler.service.index.indexing import SPEC_VERSION

    cfg = get_settings()
    return {
        "spec": SPEC_VERSION,
        "graph_rules": RULES_VERSION,
        "embedding": {
            "backend": cfg.embedding.backend,
            "model": cfg.embedding.model,
            "dim": cfg.embedding.dim,
        },
        "collections": {
            "text": cfg.qdrant.collection_text,
            "image": cfg.qdrant.collection_image,
            "ocr": cfg.qdrant.collection_ocr,
        },
    }


def _fingerprint(components: dict) -> str:
    """Băm đầu vào có tên key + sort_keys → không phụ thuộc thứ tự ghép."""
    digest = hashlib.sha256(
        json.dumps(components, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    emb = components["embedding"]
    return f"{components['spec']}-{emb['model']}-{emb['dim']}-{digest}"


def index_fingerprint() -> str:
    """Chuỗi ổn định, đọc được: spec-model-dim-<16 hex> (cùng config → cùng chuỗi)."""
    return _fingerprint(_components())


def index_version() -> dict:
    """Dict đầy đủ cho response / manifest: fingerprint + spec + graph_rules + embedding + collections."""
    components = _components()
    return {"fingerprint": _fingerprint(components), **components}
