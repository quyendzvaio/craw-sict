"""Query rewriting (LLM) + hybrid retrieval + light rerank.

Callers: api.py.
Pieces:
  rewrite(question) → dict(rewritten, terms) — LLM rewrite via vlm.rewrite_query,
    fallback rule-based expansion on failure; terms = accent-stripped keywords.
  hybrid_search_text(rewritten_vec, terms, limit, text=) → hits — E5 dense + keyword
    (BM25 sparse bỏ dấu ở collection v2) fusion (RRF); scores never merged with CLIP's.
  rerank_text(hits, question, top_n, reorder) → hits — E5 pointwise cosine → raw_score
    (router); reorder chỉ dùng cho collection v1 (nhánh keyword v1 không xếp hạng).
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
    """LLM rewrite + ý định router trong CÙNG 1 lượt gọi (vlm.rewrite_and_route), fallback
    rule-based. Trả {rewritten, terms, route_llm} — route_llm None khi không có LLM/JSON lỗi
    (router.decide dùng luật từ khoá)."""
    from sict_crawler.service.retrieval import vlm
    llm = vlm.rewrite_and_route(question)
    rewritten = (llm or {}).get("rewritten") or _rule_rewrite(question)
    route_llm = {k: llm[k] for k in ("media", "wants_file")} if llm and llm.get("media") else None
    return {"rewritten": rewritten, "terms": _terms(rewritten), "route_llm": route_llm}


def _key(r: dict) -> tuple:
    return (r.get("url", ""), r.get("chunk_id", r.get("media_id", "")))


def dedupe_hits(hits: list[dict]) -> list[dict]:
    """Bỏ hit trùng (cùng url + chunk/media), giữ thứ tự và hit đầu tiên."""
    seen: dict[tuple, dict] = {}
    for h in hits:
        seen.setdefault(_key(h), h)
    return list(seen.values())


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


def hybrid_search_text(tvec: list[float] | None, terms: list[str], limit: int = 8,
                       filter_document_id: int | None = None, query_filter=None,
                       text: str | None = None, graph_filter=None,
                       extra_vecs: list[tuple[str, list[float]]] | None = None,
                       alt_queries: list[tuple[list[float], str]] | None = None) -> list[dict]:
    """Dense E5 + keyword legs fused by RRF.

    Keyword leg (vector_store.keyword_search): collection v2 → BM25 sparse trên token bỏ dấu của
    `text` (1 lượt gọi, có xếp hạng — khớp cấu hình eval); v1 → MatchText từng term (cũ).
    graph_filter: filter payload `entities` (thực thể trong câu hỏi) → danh sách thứ 3 cho RRF.
    tvec None = truy vấn không có chữ (chỉ ảnh): bỏ nhánh dense chữ, keyword và graph.
    extra_vecs: [(via, vector)] — thêm danh sách dense cho vector khác cùng không gian (D21: vector ảnh
    truy vấn tìm trong chunk/caption); mỗi hit gắn `via`.
    alt_queries: [(vector, chữ)] — câu hỏi khác cùng ý (D27: câu LLM viết lại khi `tvec`/`text` là câu gốc), mỗi câu
    thêm 1 danh sách dense + 1 danh sách BM25 vào RRF: câu viết lại chỉ bổ sung, không thay câu gốc.
    """
    from sict_crawler.repository import vector_store
    dense = (vector_store.search_text(tvec, limit=limit, filter_document_id=filter_document_id,
                                      query_filter=query_filter) if tvec is not None else [])
    for h in dense:   # cosine câu hỏi↔điểm, giữ qua RRF → rerank_text không phải nhúng lại (raw_score)
        h["dense_score"] = h["score"]
    try:
        kw_hits = (vector_store.keyword_search(text or " ".join(terms), terms, limit=limit,
                                               query_filter=query_filter)
                   if (text or terms) else [])
    except Exception:
        kw_hits = []  # keyword leg is best-effort; dense already returned
    x_lists: list[list[dict]] = []
    for via, v in extra_vecs or []:
        x_lists.append([{**h, "via": via} for h in vector_store.search_text(
            v, limit=limit, filter_document_id=filter_document_id, query_filter=query_filter)])
    # graph (D20): câu hỏi nêu người/sự kiện/CLB → thêm 1 danh sách dense chỉ trong chunk/caption
    # gắn thực thể đó, gộp RRF (không lọc cứng: tên nhận sai không làm mất kết quả thường)
    # graph_filter: 1 filter hoặc danh sách (VD [cả 2 thực thể (AND), bất kỳ (OR)]) → mỗi filter 1 danh sách
    g_lists: list[list[dict]] = []
    for gf in (graph_filter if isinstance(graph_filter, list) else [graph_filter]):
        if gf is None or tvec is None:
            continue
        try:
            g_lists.append(vector_store.search_text(tvec, limit=limit, query_filter=vector_store._and(
                query_filter, gf)))
        except Exception:
            pass
    a_lists: list[list[dict]] = []
    for avec, atext in alt_queries or []:
        a_dense = vector_store.search_text(avec, limit=limit, filter_document_id=filter_document_id,
                                           query_filter=query_filter)
        for h in a_dense:
            h["dense_score"] = h["score"]
        a_lists.append(a_dense)
        try:
            a_lists.append(vector_store.keyword_search(atext, _terms(atext), limit=limit, query_filter=query_filter))
        except Exception:
            pass
    lists = [x for x in (dense, kw_hits, *x_lists, *g_lists, *a_lists) if x]
    if len(lists) <= 1:
        return lists[0] if lists else []
    return _rrf(*lists)


def rerank_text(hits: list[dict], question: str, top_n: int = 5, reorder: bool = True,
                qvec: list[float] | None = None) -> list[dict]:
    """Cosine câu hỏi ↔ `title. content` → h["raw_score"] (router chặn caption theo ngưỡng cosine).
    reorder=True: xếp lại theo cosine (score = cosine), cắt top_n.

    Collection v2/v3 dùng reorder=False: đo trên bộ vàng (Qdrant thật, 2026-10-14) xếp lại bằng cosine
    E5 làm mất tín hiệu BM25 của RRF — R@5 0.94 → 0.97, MRR 0.81 → 0.87 khi giữ thứ tự RRF. Khi đó chỉ
    hit `media_caption` cần raw_score (router.passes), nên chỉ nhúng các hit đó — thường không tốn lượt nào.
    qvec: vector câu hỏi đã có (backend api, tránh nhúng lại).

    reorder=False + qvec: raw_score = cosine với vector ĐÃ LƯU của điểm — `dense_score` của nhánh dense (Qdrant đã
    tính), điểm chỉ đến từ BM25 thì lấy vector trong Qdrant (point_vectors) nhân với qvec. Không gọi embedding: trước
    đây nhúng lại chữ của mọi caption mỗi câu hỏi — index v3 qua dịch vụ int8 tốn ~16 s / câu (đo 2026-10-20)."""
    from sict_crawler.service.embedding import embedder
    if not hits:
        return []
    scored = hits if reorder else [h for h in hits if h.get("kind") == "media_caption"]
    if not reorder and qvec is not None:
        from sict_crawler.repository import vector_store
        need = [(h["_col"], h["_pid"]) for h in scored if h.get("dense_score") is None and h.get("_pid")]
        vecs = vector_store.point_vectors(need) if need else {}
        for h in scored:
            v = vecs.get(h.get("_pid")) if h.get("dense_score") is None else None
            if h.get("dense_score") is not None:
                h["raw_score"] = h["dense_score"]
            elif v is not None:
                h["raw_score"] = sum(a * b for a, b in zip(qvec, v))
        scored = [h for h in scored if "raw_score" not in h]   # không có vector đã lưu (VD hit dựng tay) → nhúng như cũ
        if not scored:
            return hits[:top_n]
    if scored:
        texts = [f"{h.get('title', '')}. {h.get('content') or h.get('caption') or ''}"[:1000] for h in scored]
        if embedder.backend() == "api":   # D22: tiền tố query do embedder áp (kind="query"), không gắn chữ vào văn bản
            qv, tvecs = qvec or embedder.embed_query(question), embedder.embed_texts(texts)
        else:
            vecs = embedder.embed_texts([f"query: {question}"] + [f"passage: {t}" for t in texts])
            qv, tvecs = vecs[0], vecs[1:]
        for h, tv in zip(scored, tvecs):
            h["raw_score"] = sum(a * b for a, b in zip(qv, tv))  # cosine in [-1,1]
            if reorder:
                h["score"] = h["raw_score"]
    if reorder:
        hits.sort(key=lambda h: -h["score"])
    return hits[:top_n]
