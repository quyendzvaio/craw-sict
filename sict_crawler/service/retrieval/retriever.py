"""Truy xuất lai (D17/D20/D21): viết lại câu hỏi → dense + BM25 RRF → ưu tiên metadata → media qua router;
ảnh người dùng gửi (U3–U7) → ảnh giống + chữ của bài chứa ảnh. Kết quả cache Redis theo câu hỏi."""
from __future__ import annotations

import hashlib
import json

from sict_crawler.core.config import get_settings
from sict_crawler.model.schemas import Source
from sict_crawler.repository import cache, vector_store
from sict_crawler.service.embedding import embedder
from sict_crawler.service.retrieval import filters as F
from sict_crawler.service.retrieval.sources import media_sources, text_sources

QUERY_TTL = 600      # kết quả retrieve theo câu hỏi (giây)


def index_tag() -> str:
    """Collection đang phục vụ: đổi v2 ↔ v3 (cutover / rollback) thì không lấy lại kết quả cache của index kia."""
    q = get_settings().qdrant
    return f"{q.collection_text}|{q.collection_image}|{q.collection_ocr}"


def query_cache_key(question: str, top_k: int, filters_key: str = "", index: str | None = None) -> str:
    index = index_tag() if index is None else index
    return "q:v3:" + hashlib.sha256(
        f"{question}\x00{top_k}\x00{filters_key}\x00{index}".encode()).hexdigest()


def _graph_filter(keys: list[str], field: str = "entities", all_of: bool = False):
    """Lọc payload theo thực thể: bất kỳ (OR) hoặc tất cả (AND — câu hỏi nêu ≥ 2 thực thể)."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
    if not keys or (all_of and len(keys) < 2):
        return None
    if all_of:
        return Filter(must=[FieldCondition(key=field, match=MatchValue(value=k)) for k in keys])
    return Filter(must=[FieldCondition(key=field, match=MatchAny(any=keys))])


def _media_candidates(img_query_vecs: list[list[float]], terms: list[str], limit: int,
                      f: F.QueryFilters, text: str = "", entity_keys: list[str] | None = None,
                      image_vecs: list[list[float]] | None = None) -> list[dict]:
    """Ứng viên media cho router.gate, giữ ĐIỂM GỐC theo loại (không RRF):
    graph (ảnh/frame gắn thực thể trong câu hỏi: raw_score = trọng số cạnh, thứ tự trong nhóm theo
    CLIP; +0.1 với lượt lọc đúng năm hỏi) + CLIP ảnh (clip, raw_score=cosine) + caption khớp từ khoá
    (caption_kw, không ngưỡng). image_vecs: vector của ẢNH người dùng gửi (D21, không gian jina) →
    modality "i2i", ngưỡng riêng; legacy CLIP thì ảnh gửi lên nằm chung img_query_vecs."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    from sict_crawler.service.graph import graph as G
    qf = F.to_qdrant(f)
    out: list[dict] = []
    if entity_keys and img_query_vecs:
        # chuỗi sự kiện nhiều năm (IT Festival: ~140 ảnh) → hỏi kèm năm thì lấy ảnh đúng năm trước,
        # không thì top-N theo CLIP dễ toàn năm khác
        year_f = (Filter(must=[FieldCondition(key="year", match=MatchAny(any=f.prefer_years))])
                  if f.prefer_years else None)
        # ≥ 2 thực thể ("ảnh thầy X ở IT Festival") → ảnh gắn CẢ HAI đứng đầu (+0.2)
        passes = [(_graph_filter(entity_keys, "entities", all_of=True), G.W_DEPICT_STRONG + 0.2)]
        passes += [(_graph_filter(entity_keys, fld), w) for fld, w in
                   (("entities_strong", G.W_DEPICT_STRONG), ("entities", G.W_DEPICT_WEAK))]
        for gf, w in passes:
            if gf is None:
                continue
            for yf in ((year_f, None) if year_f else (None,)):
                try:
                    hits = vector_store.search_images(img_query_vecs[0], limit=limit, query_filter=
                                                      vector_store._and(qf, gf, yf))
                except Exception:
                    hits = []
                out += [{**h, "modality": "graph", "raw_score": w + (0.1 if yf else 0.0)} for h in hits]
    for modality, vecs in (("clip", img_query_vecs), ("i2i", image_vecs or [])):
        for v in vecs:
            for h in vector_store.search_images(v, limit=limit, query_filter=qf):
                out.append({**h, "modality": modality, "raw_score": h.get("score")})
    cap_only = vector_store._and(qf, Filter(must=[
        FieldCondition(key="kind", match=MatchValue(value="media_caption"))]))
    try:
        hits = vector_store.keyword_search(text or " ".join(terms), terms[:3], limit=limit,
                                           query_filter=cap_only, collection=vector_store.ocr_collection())
    except Exception:
        hits = []
    out += [{**h, "modality": "caption_kw", "raw_score": None}
            for h in hits if h.get("kind") == "media_caption"]
    return out


def _as_media(h: dict) -> dict:
    """Caption point (sict_text) → dạng media payload cho media_sources."""
    if h.get("kind") == "media_caption":
        return {**h, "kind": h.get("media_kind", "image")}
    return h


def _image_tag(b64: str | None) -> str:
    return "|img:" + hashlib.sha256(b64.encode()).hexdigest()[:20] if b64 else ""


def _query_image_vec(b64: str | None) -> list[float] | None:
    """Vector của ảnh người dùng gửi; ảnh hỏng/không giải mã được → None (truy vấn chạy tiếp bằng chữ)."""
    if not b64:
        return None
    try:
        v = embedder.embed_query_image_base64([b64])
    except Exception:
        return None
    return v[0] if v else None


def _image_block(media_key: str | None, doc_id: int) -> int | None:
    """Block chứa ảnh `media_key` trong bài `doc_id` (D23), đọc từ điểm caption của ảnh đó — điểm ảnh chung
    nhiều bài nên không mang vị trí. None = dữ liệu cũ chưa có cấu trúc."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    if not media_key:
        return None
    try:
        caps = vector_store.scroll_text(Filter(must=[
            FieldCondition(key="kind", match=MatchValue(value="media_caption")),
            FieldCondition(key="media_key", match=MatchValue(value=media_key)),
            FieldCondition(key="document_id", match=MatchValue(value=doc_id))]), limit=4,
            collection=vector_store.ocr_collection())
    except Exception:
        return None
    return next((c["block_idx"] for c in caps if c.get("block_idx") is not None), None)


def _chunks_around(doc_id: int, block_idx: int, limit: int = 2) -> list[dict]:
    """Chunk chữ sát ảnh ở block `block_idx` (D24 — ngữ cảnh phụ cận để ánh xạ ảnh về trang): chunk có block
    nằm trong khoảng của nó trước; ảnh ở ranh giới giữa hai chunk thì lấy chunk liền trước + liền sau."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    try:
        chunks = [c for c in vector_store.scroll_text(Filter(must=[
            FieldCondition(key="document_id", match=MatchValue(value=doc_id))]),
            collection=get_settings().qdrant.collection_text)
            if c.get("block_range") and c.get("kind") != "media_caption"]
    except Exception:
        return []
    inside = [c for c in chunks if c["block_range"][0] <= block_idx <= c["block_range"][1]]
    if inside:
        return inside[:limit]
    before = max((c for c in chunks if c["block_range"][1] < block_idx), key=lambda c: c["block_range"][1], default=None)
    after = min((c for c in chunks if c["block_range"][0] > block_idx), key=lambda c: c["block_range"][0], default=None)
    return [c for c in (before, after) if c][:limit]


def _linked_doc_hits(ivec: list[float], kept: list[dict], qf) -> list[dict]:
    """U4/D24: bài chứa ảnh giống ảnh người dùng gửi → đoạn chữ gần ảnh đó (theo block) làm ngữ cảnh; ảnh chưa
    có vị trí block thì lấy đoạn giống vector ảnh nhất của bài. Điểm = độ giống của ảnh."""
    from sict_crawler.service.retrieval import router
    linked: list[dict] = []
    pairs = list(dict.fromkeys((d, h.get("media_key")) for h in kept if h.get("modality") == "i2i"
                               for d in router._docs_of(h)))[:3]
    for d, media_key in pairs:
        hits = []
        blk = _image_block(media_key, int(d))
        if blk is not None:
            hits = _chunks_around(int(d), blk)
        if not hits:
            try:
                hits = [h for h in vector_store.search_text(
                    ivec, limit=2, filter_document_id=int(d), query_filter=qf,
                    collection=get_settings().qdrant.collection_text) if h.get("kind") != "media_caption"]
            except Exception:
                hits = []
        linked += [{**h, "via": "i2i-doc"} for h in hits]
    return linked


def retrieve(question: str, top_k: int = 5,
             query_image_b64: str | None = None, filters: dict | None = None):
    """→ (text_sources, media_sources, route). Text: rewrite → dense + keyword RRF → rerank → ưu tiên
    mềm metadata. Media theo router D17: ý định `none` bỏ hẳn nhánh media; còn lại chặn theo bằng chứng
    (router.gate, điểm gốc theo loại, gắn với bài chữ đứng đầu).

    Đa phương thức (D21, embedding.backend=api, chữ+ảnh chung không gian): ảnh gửi lên vừa tìm ảnh giống
    (i2i), vừa tìm chunk/caption (i2t), và kéo chunk của BÀI chứa ảnh gần trùng (i2i-doc); chỉ có ảnh
    (question rỗng) → ra cả ảnh lẫn chữ. Backend torch giữ hành vi cũ: ảnh chỉ thêm 1 vector vào nhánh CLIP.

    Cache Redis 10p theo (question, top_k, filters, hash ảnh).
    """
    from sict_crawler.service.retrieval import query_rewriter, router
    rc = get_settings().router
    unified = embedder.backend() == "api"
    has_text = bool(question.strip()) or not unified   # unified: question rỗng = truy vấn chỉ có ảnh
    # metadata: lọc cứng chỉ từ `filters` caller truyền; câu hỏi chỉ cho ưu tiên mềm (filters.py)
    f = F.merge(F.from_request(filters), F.infer(question))
    qf = F.to_qdrant(f)
    ck = query_cache_key(question, top_k, f.cache_key() + _image_tag(query_image_b64))
    hit = cache.get(ck)
    if hit:
        try:
            d = json.loads(hit)
            return ([Source(**s) for s in d["text"]], [Source(**s) for s in d["media"]],
                    router.Route(**d["route"]))
        except Exception:
            pass
    ivec = _query_image_vec(query_image_b64) if unified else None
    if query_image_b64 and ivec is None and not has_text:
        return [], [], router.Route(media="none", source="image", inputs=["image"])   # ảnh hỏng, không có chữ
    if has_text:
        r = query_rewriter.rewrite(question)
        route = router.decide(question, r.get("route_llm"))
    else:   # chỉ ảnh (D21 U3+U4)
        r = {"rewritten": "", "terms": [], "route_llm": None}
        route = router.Route(media="required", source="image")
    # graph (D20): người / sự kiện / CLB nêu trong câu hỏi → nhánh chữ + media theo thực thể
    from sict_crawler.service.graph import graph as G
    ents = G.match_question(question, r["rewritten"]) if has_text else []
    ekeys = G.ekeys(ents)
    route.entities = [e.name for e in ents]
    route.facts = G.facts(ents)
    if query_image_b64 and route.media == "none":
        route.media = "optional"  # người dùng gửi ảnh → luôn xét media tương tự
    if route.wants_file:
        f.prefer_doc_types = ["attachment"]

    # text leg: hybrid (dense + BM25 sparse + dense của ảnh truy vấn, RRF) → ưu tiên mềm metadata
    tvec = embedder.embed_query(r["rewritten"]) if has_text else None
    extra = [("i2t", ivec)] if ivec is not None else []
    t_hits = query_rewriter.hybrid_search_text(tvec, r["terms"], limit=max(top_k * 2, 10),
                                               query_filter=qf, text=r["rewritten"],
                                               graph_filter=[_graph_filter(ekeys, all_of=True),
                                                             _graph_filter(ekeys)],
                                               extra_vecs=extra)
    if has_text:
        # raw_score = cosine câu hỏi↔đoạn (ngưỡng caption của router). v2: giữ thứ tự RRF dense+BM25
        # (xem rerank_text); v1: xếp lại theo cosine như cũ.
        reranked = query_rewriter.rerank_text(t_hits, r["rewritten"], top_n=len(t_hits),
                                              reorder=not vector_store.has_sparse(), qvec=tvec)
    else:   # chỉ ảnh: danh sách duy nhất là cosine ảnh↔chữ → lọc theo i2t_min, điểm gốc = cosine
        reranked = [{**h, "raw_score": h["score"]} for h in t_hits if h["score"] >= rc.i2t_min]
    boosted = F.apply_boost(reranked, f)
    text_hits = [h for h in boosted if h.get("kind") != "media_caption"]

    kept: list[dict] = []
    if route.media != "none":
        if unified:
            img_query_vecs = [tvec] if tvec is not None else []
            image_vecs = [ivec] if ivec is not None else []
            clip_min, caption_min = rc.t2i_min, rc.t2c_min
        else:
            img_query_vecs = [embedder.embed_text_clip([r["rewritten"]])[0]]
            image_vecs = []
            if (qvec := _query_image_vec(query_image_b64)) is not None:
                img_query_vecs.append(qvec)
            clip_min, caption_min = rc.clip_min, rc.caption_min
        cands = _media_candidates(img_query_vecs, r["terms"], max(top_k, 10), f, r["rewritten"],
                                  ekeys, image_vecs)
        cands += [{**h, "modality": "caption" if has_text else "i2t"}
                  for h in boosted if h.get("kind") == "media_caption"]
        # hỏi về NGƯỜI và graph đã có ảnh của người đó → bỏ ảnh chỉ khớp CLIP/từ khoá (CLIP không
        # nhận ra người, chỉ chèn ảnh lạc vào cuối danh sách)
        if any(e.kind == "person" for e in ents) and any(c["modality"] == "graph" for c in cands):
            cands = [c for c in cands if c["modality"] in ("graph", "caption")]
        kept = router.gate(route, text_hits[:rc.optional_top_docs], cands, top_k,
                           clip_min, caption_min, rc.optional_max, rc.graph_min, rc.i2i_min, rc.i2t_min)
    msrcs = media_sources([_as_media(h) for h in kept])

    # U4: ảnh giống ảnh người dùng gửi nằm trong bài nào → lấy đoạn gần ảnh nhất của bài đó làm ngữ cảnh
    if ivec is not None:
        linked = _linked_doc_hits(ivec, kept, qf)
        # có chữ kèm ảnh ("ảnh này là sự kiện gì?"): đoạn của bài chứa ảnh khớp NHẤT lên đầu, bài của các ảnh giống
        # kém hơn xếp sau kết quả chữ (ảnh sự kiện khác nhau cũng giống nhau ~0,9, dễ kéo nhầm bài)
        lead = [h for h in linked if linked and h.get("document_id") == linked[0].get("document_id")]
        rest = [h for h in linked if h not in lead]
        text_hits = query_rewriter.dedupe_hits(linked + text_hits if not has_text else lead + text_hits + rest)
    tsrcs = text_sources(text_hits[:top_k])
    route.inputs = [x for x, on in (("text", bool(question.strip())), ("image", bool(query_image_b64))) if on]
    route.outputs = [x for x, on in (("text", bool(tsrcs)), ("image", bool(msrcs))) if on]
    cache.put(ck, json.dumps(
        {"text": [s.model_dump() for s in tsrcs],
         "media": [s.model_dump() for s in msrcs],
         "route": route.to_dict()}).encode(), QUERY_TTL)
    return tsrcs, msrcs, route
