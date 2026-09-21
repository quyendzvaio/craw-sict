"""Query rewriting (LLM) + hybrid retrieval + light rerank.

Callers: api.py.
Pieces:
  rewrite(question) → dict(rewritten, terms) — LLM rewrite via vlm.rewrite_query,
    fallback rule-based expansion on failure; terms = accent-stripped keywords.
  hybrid_search_text(rewritten_vec, terms, limit) → hits — E5 dense + keyword
    fusion (RRF); scores never merged with CLIP's.
  rerank_text(hits, question, top_n) → hits — E5 pointwise rescore
    (query/passage pair), the cheap rerank (no cross-encoder dep).
"""
from __future__ import annotations

import re
import unicodedata

# Rule-based fallback when LLM rewrite fails
_ABBREV = [
    (re.compile(r"\battt\b", re.I), "an toàn thông tin"),
    (re.compile(r"\bcntt\b", re.I), "công nghệ thông tin"),
    (re.compile(r"\bhttt\b", re.I), "hệ thống thông tin"),
    (re.compile(r"\bkhmt\b", re.I), "khoa học máy tính"),
    (re.compile(r"\bkhdl\b", re.I), "khoa học dữ liệu"),
    (re.compile(r"\bctdt\b", re.I), "chương trình đào tạo"),
    (re.compile(r"\bthac\s*si\b", re.I), "thạc sĩ"),
    (re.compile(r"\bdtb\b", re.I), "điểm trung bình"),
]


def _no_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d").replace("Đ", "D")


def _rule_rewrite(question: str) -> str:
    for pat, full in _ABBREV:
        question = pat.sub(full, question)
    return question


def _terms(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]+",
                       text.lower())
    seen: set[str] = set()
    out = []
    for w in (_no_accents(w) for w in words):
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def rewrite(question: str) -> dict:
    """LLM rewrite (vlm.rewrite_query), fallback rule-based. Extract terms
    from the rewritten text either way."""
    from . import vlm
    rewritten = vlm.rewrite_query(question) or _rule_rewrite(question)
    return {"rewritten": rewritten, "terms": _terms(rewritten)}


def _key(r: dict) -> tuple:
    return (r.get("url", ""), r.get("chunk_id", r.get("media_id", "")))


def _rrf(*hit_lists: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion of N ranked hit lists."""
    scores: dict[tuple, float] = {}
    best: dict[tuple, dict] = {}
    for hits in hit_lists:
        for rank, r in enumerate(hits):
            scores[_key(r)] = scores.get(_key(r), 0.0) + 1.0 / (k + rank + 1)
            best.setdefault(_key(r), r)
    out = []
    for kk, s in sorted(scores.items(), key=lambda x: -x[1]):
        h = dict(best[kk])
        h["score"] = s  # RRF score, comparable only within text group
        out.append(h)
    return out


def hybrid_search_text(tvec: list[float], terms: list[str], limit: int = 8,
                       filter_document_id: int | None = None) -> list[dict]:
    """Dense E5 + keyword legs fused by RRF.

    Keyword leg: Qdrant MatchText scroll per term (≤6 terms). Works without a
    payload index on small corpora. ponytail: add Qdrant FTS index if corpus
    outgrows ~10k chunks.
    """
    from . import vector_store
    dense = vector_store.search_text(tvec, limit=limit, filter_document_id=filter_document_id)

    kw_hits: list[dict] = []
    seen: set[tuple] = set()
    for term in terms[:6]:
        try:
            hits = vector_store.scroll_text_by_content(term, limit=limit)
        except Exception:
            continue  # keyword leg is best-effort; dense already returned
        for h in hits:
            kk = _key(h)
            if kk not in seen:
                seen.add(kk)
                kw_hits.append(h)
    if not kw_hits:
        return dense
    return _rrf(dense, kw_hits)


def rerank_text(hits: list[dict], question: str, top_n: int = 5) -> list[dict]:
    """Light rerank: E5 pointwise rescore (query vs hit text), cap top_n."""
    from . import embedder
    if not hits:
        return []
    texts = [f"{h.get('title', '')}. {h.get('content') or h.get('caption') or ''}"[:1000]
             for h in hits]
    vecs = embedder.embed_texts([f"query: {question}"] + [f"passage: {t}" for t in texts])
    qv = vecs[0]
    for h, tv in zip(hits, vecs[1:]):
        h["score"] = sum(a * b for a, b in zip(qv, tv))  # cosine in [-1,1]
    hits.sort(key=lambda h: -h["score"])
    return hits[:top_n]
