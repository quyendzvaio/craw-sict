"""FastAPI: hybrid query endpoint + chat UI + SSE streaming + media serving.

Callers: uvicorn sict_crawler.api:app.
Schemas: POST /query {question, top_k} → {answer, context, sources[]}; GET / → chat UI;
         GET /media/{media_id} → image bytes from PG base64 (S3 fallback) — index v1.
         GET /media/s/{sha256} → media index v2 (bảng media).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from . import citations, db, embedder, storage, vector_store
from . import filters as F
from .citations import make_cite
from .config import get_settings
from .models import MediaAsset

app = FastAPI(title="SICT RAG API")
from . import metrics as _metrics  # noqa: E402 — D13: GET /metrics (HTTP + pipeline) cho Alloy / Grafana Cloud
_metrics.instrument(app)
_GRAPH_REFRESH_SEC = 600


async def _refresh_graph() -> None:
    """Nạp danh bạ thực thể (graph D20) từ Postgres. Lỗi/không có PG → giữ bản cũ (hoặc tắt graph)."""
    import asyncio

    from . import graph as G
    from . import graphsync
    try:
        async with db.get_session() as s:
            gaz = await asyncio.wait_for(graphsync.load_gazetteer(s), timeout=20)
            rels = await asyncio.wait_for(graphsync.load_relations(s), timeout=20)
        G.set_current(gaz if len(gaz) else None)
        G.set_relations(rels, gaz)
        print(f"[graph] danh bạ {len(gaz)} thực thể, {len(rels)} cạnh thực thể–thực thể")
    except Exception as e:  # noqa: BLE001
        print(f"[graph] không nạp được danh bạ: {e}")


@app.on_event("startup")
async def _graph_startup() -> None:
    import asyncio

    async def loop():
        while True:
            await _refresh_graph()
            await asyncio.sleep(_GRAPH_REFRESH_SEC)
    app.state.graph_task = asyncio.create_task(loop())
UI_DIR = Path(__file__).parent / "ui"

# BE cache (Redis db 3, best-effort): mọi key đều có TTL nên an toàn với
# chính sách volatile-ttl — frontier/celery (không TTL) không bao giờ bị đuổi.
_QUERY_TTL = 600      # kết quả retrieve theo câu hỏi
_MEDIA_TTL = 86400    # bytes ảnh theo media_id (≤1MB mới cache)
_MEDIA_MAX = 1_000_000
_cache_client = None


def _cache():
    """Redis db 3 lazy. Lỗi kết nối → None (chạy tiếp không cache)."""
    global _cache_client
    if _cache_client is None:
        try:
            import redis
            from urllib.parse import urlsplit

            from .config import get_settings
            # from_url bỏ qua db= kwarg (path /0 thắng) → parse tay để chắc db 3
            u = urlsplit(get_settings().redis_url)
            _cache_client = redis.Redis(
                host=u.hostname or "localhost", port=u.port or 6379,
                username=u.username or None,
                password=u.password or None, db=3,
                socket_connect_timeout=2, socket_timeout=2)
            _cache_client.ping()
        except Exception:
            _cache_client = False
    return _cache_client or None


def _index_tag() -> str:
    """Collection đang phục vụ: đổi v2 ↔ v3 (cutover / rollback) thì không lấy lại kết quả cache của index kia."""
    q = get_settings().qdrant
    return f"{q.collection_text}|{q.collection_image}|{q.collection_ocr}"


def _query_cache_key(question: str, top_k: int, filters_key: str = "", index: str | None = None) -> str:
    index = _index_tag() if index is None else index
    return "q:v3:" + hashlib.sha256(
        f"{question}\x00{top_k}\x00{filters_key}\x00{index}".encode()).hexdigest()


def _cache_get(key: str) -> bytes | None:
    try:
        c = _cache()
        return c.get(key) if c else None
    except Exception:
        return None


def _cache_set(key: str, val: bytes, ttl: int) -> None:
    try:
        c = _cache()
        if c:
            c.setex(key, ttl, val)
    except Exception:
        pass


IMAGE_ONLY_QUESTION = "Người dùng gửi một ảnh, không kèm câu hỏi. Hãy cho biết thông tin liên quan đến ảnh này."


class QueryIn(BaseModel):
    question: str
    top_k: int = 8
    image: str | None = None  # base64 (không có tiền tố data:) — truy vấn bằng ảnh (D21); question có thể rỗng
    # lọc cứng metadata: {"sections": [...], "doc_types": [...], "year_from": int, "year_to": int}
    filters: dict | None = None


class Source(BaseModel):
    url: str
    title: str
    kind: str  # text | image | video_frame
    score: float
    excerpt: str = ""
    media_id: int | None = None      # index v1 → GET /media/{media_id}
    media_key: str | None = None     # index v2 (sha256) → GET /media/s/{media_key}
    timestamp: float | None = None
    published_at: str | None = None  # index spec v2 (thiếu ở index cũ)
    section: str | None = None
    parent_url: str | None = None    # file đính kèm → trang chứa nó (url = link tải file)
    via: str | None = None           # D21: nhánh tìm ra nguồn này: text | i2t | i2i-doc | graph | clip | i2i | i2t | caption …
    heading_path: list[str] | None = None   # D23: mục chứa đoạn trích trong bài
    page: int | None = None                 # D24: trang (từ 1) trong file PDF/PPTX; None = không có trang
    cite: str = ""                          # D24: nhãn trích dẫn dựng sẵn (citations.make_cite)


def text_sources(text_hits: list[dict]) -> list[Source]:
    """E5 hits → Source. media_caption hits carry media refs."""
    sources: list[Source] = []
    for r in text_hits:
        if r.get("kind") == "media_caption":
            sources.append(Source(
                url=r.get("url", ""), title=r.get("title", ""),
                kind=r.get("media_kind", "image"), score=r["score"],
                excerpt=r.get("caption", ""), media_id=r.get("media_id"),
                media_key=r.get("media_key"),
                timestamp=r.get("timestamp"), published_at=r.get("published_at"),
                section=r.get("section"), parent_url=r.get("page_url"), via=r.get("via", "caption"),
                heading_path=r.get("heading_path") or None,
                cite=make_cite(r.get("title", ""), r.get("page_url") or r.get("url", ""),
                               r.get("media_kind", "image"), None, r.get("heading_path"))))
        else:
            kind = "file" if r.get("doc_type") == "attachment" else "text"
            sources.append(Source(url=r["url"], title=r.get("title", ""), kind=kind,
                                  score=r["score"], excerpt=(r.get("content") or r.get("text") or "")[:300],
                                  published_at=r.get("published_at"), section=r.get("section"),
                                  parent_url=r.get("parent_url"), via=r.get("via", "text"),
                                  heading_path=r.get("heading_path") or None, page=r.get("page"),
                                  cite=make_cite(r.get("title", ""), r["url"], kind, r.get("page"),
                                                 r.get("heading_path"))))
    return sources


def _first(v):
    return v[0] if isinstance(v, list) and v else (None if isinstance(v, list) else v)


def media_sources(image_hits: list[dict]) -> list[Source]:
    """CLIP hits (sict_image) / caption → Source. v2: section/page_urls là danh sách (ảnh dùng
    chung nhiều bài) → lấy bài đầu; video caption → kind video, media_key = khung hình đầu."""
    return [Source(
        url=r.get("url", ""), title=r.get("title", ""), kind=r.get("kind", "image"),
        score=r["score"], excerpt=r.get("caption", r.get("s3_key", "")),
        media_id=r.get("media_id"), media_key=r.get("media_key"), timestamp=r.get("timestamp"),
        published_at=r.get("published_at"), section=_first(r.get("section")),
        parent_url=r.get("page_url") or _first(r.get("page_urls")), via=r.get("modality"),
        heading_path=r.get("heading_path") or None,
        cite=make_cite(r.get("title", ""), r.get("page_url") or _first(r.get("page_urls")) or r.get("url", ""),
                       r.get("kind", "image"), None, r.get("heading_path")))
        for r in image_hits]


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

    from . import graph as G
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
    from . import router
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
    from . import query_rewriter, router
    from .config import get_settings
    rc = get_settings().router
    unified = embedder.backend() == "api"
    has_text = bool(question.strip()) or not unified   # unified: question rỗng = truy vấn chỉ có ảnh
    # metadata: lọc cứng chỉ từ `filters` caller truyền; câu hỏi chỉ cho ưu tiên mềm (filters.py)
    f = F.merge(F.from_request(filters), F.infer(question))
    qf = F.to_qdrant(f)
    ck = _query_cache_key(question, top_k, f.cache_key() + _image_tag(query_image_b64))
    hit = _cache_get(ck)
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
    from . import graph as G
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
    _cache_set(ck, json.dumps(
        {"text": [s.model_dump() for s in tsrcs],
         "media": [s.model_dump() for s in msrcs],
         "route": route.to_dict()}).encode(), _QUERY_TTL)
    return tsrcs, msrcs, route


def _date_tag(s: Source) -> str:
    """' (11/09/2026)' khi có ngày đăng — LLM cần ngày để trả lời câu hỏi thời gian."""
    if not s.published_at:
        return ""
    p = s.published_at[:10].split("-")
    return f" ({p[2]}/{p[1]}/{p[0]})" if len(p) == 3 else ""


def build_context(tsrcs: list[Source], msrcs: list[Source], facts: list[str] | None = None) -> str:
    """facts: dữ kiện graph D20 (thực thể trong câu hỏi + thực thể liên quan, số bài làm chứng)."""
    parts = [f"[graph] {f}" for f in (facts or [])]
    # D24: nguồn đánh số [1]…[n] (chữ trước, media sau) — LLM trích dẫn bằng [n], citations.enforce kiểm lại
    parts += [f"[{i}] ({s.kind}) {s.cite or s.title or s.url}{_date_tag(s)}\n{s.excerpt}"
              for i, s in enumerate(tsrcs + msrcs, 1)]
    return "\n\n---\n\n".join(parts)


@app.post("/query")
async def query(q: QueryIn):
    from . import agent as _agent
    # agent.run đồng bộ (Qdrant, embed, LLM): gọi thẳng trong handler async thì chặn event loop → mọi request
    # (cả /metrics của Alloy) xếp hàng tuần tự; tải 4 luồng 2026-10-21: p50 0,4 → 1,6 s, scrape /metrics 30 s, up=0
    res = await run_in_threadpool(_agent.run, q.question, q.top_k, q.image, q.filters)
    return {"answer": None, "context": res["context"],
            "media_mode": res.get("media_mode"), "wants_file": res.get("wants_file"),
            "media_note": res.get("media_note"), "entities": res.get("entities"),
            "graph_facts": res.get("graph_facts"),
            "inputs": res.get("inputs"), "outputs": res.get("outputs"),   # D21: đầu vào / đầu ra thực tế
            "citations": citations.index(res["text"] + res["media"]),     # D24: nguồn đánh số như trong context
            "text": [s.model_dump() for s in res["text"]],
            "media": [s.model_dump() for s in res["media"]],
            "attempts": res["attempts"], "sufficient": res["sufficient"]}


@app.post("/chat")
async def chat(q: QueryIn):
    """SSE streaming qua agent loop: status (attempts) → sources event →
    streamed LLM answer (context echo fallback if no LLM key)."""
    from . import agent as _agent
    from . import vlm
    res = await run_in_threadpool(_agent.run, q.question, q.top_k, q.image, q.filters)   # xem /query
    tsrcs, msrcs, context = res["text"], res["media"], res["context"]
    if not res["sufficient"]:
        context = ("(Lưu ý: tài liệu tìm được chỉ liên quan một phần, "
                   "câu trả lời có thể chưa đầy đủ.)\n\n" + context)

    async def gen():
        yield f"data: {json.dumps({'status': {'attempts': res['attempts'], 'sufficient': res['sufficient'], 'media_mode': res.get('media_mode'), 'wants_file': res.get('wants_file'), 'media_note': res.get('media_note'), 'entities': res.get('entities'), 'inputs': res.get('inputs'), 'outputs': res.get('outputs')}})}\n\n"
        yield f"data: {json.dumps({'text': [s.model_dump() for s in tsrcs], 'media': [s.model_dump() for s in msrcs]})}\n\n"
        import asyncio
        import queue
        import threading
        loop = asyncio.get_event_loop()
        streamed: list[str] = []
        q_out: queue.Queue = queue.Queue()
        SENTINEL = object()

        def worker():
            """vlm.stream_answer is a sync generator doing blocking HTTP."""
            try:
                for chunk in vlm.stream_answer(q.question.strip() or IMAGE_ONLY_QUESTION, context, q.image):
                    q_out.put(chunk)
            finally:
                q_out.put(SENTINEL)

        threading.Thread(target=worker, daemon=True).start()
        got = False
        while True:
            item = await loop.run_in_executor(None, q_out.get)
            if item is SENTINEL:
                break
            got = True
            streamed.append(item)
            yield f"data: {json.dumps({'delta': item})}\n\n"
        if not got:  # no key / LLM fail → echo context
            answer = f"(LLM chưa trả lời được — đây là context truy xuất được)\n\n{context[:1500]}"
            for chunk in [answer[i:i+80] for i in range(0, len(answer), 80)]:
                streamed.append(chunk)
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
        # D24: mọi câu trả lời kèm trích dẫn hợp lệ — thiếu thì ghép dòng "Nguồn:", [n] ảo bị bỏ
        said = "".join(streamed)
        final, cites = citations.enforce(said, tsrcs + msrcs)
        if final != said:
            yield f"data: {json.dumps({'delta': final[len(said):]} if final.startswith(said) else {'answer': final})}\n\n"
        yield f"data: {json.dumps({'citations': cites})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((UI_DIR / "index.html").read_text(),
                        headers={"Cache-Control": "no-store"})


@app.get("/app.js")
async def app_js():
    return Response((UI_DIR / "app.js").read_text(),
                    media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/media/s/{key}")
async def media_by_key(key: str, request: Request) -> Response:
    """Media index v2 theo sha256 (bảng media). Nội dung theo hash → ETag = key, immutable."""
    from .models import Media
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise HTTPException(404, "media not found")
    etag = f'"{key[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    data = _cache_get(f"ms:{key}")
    async with db.get_session() as session:
        m = await session.scalar(select(Media).where(Media.sha256 == key))
    if m is None or not m.s3_key:
        raise HTTPException(404, "media not found")
    if data is None:
        data = await run_in_threadpool(storage.Storage().get_media, m.s3_key)   # S3 đồng bộ: không chặn event loop
        if len(data) <= _MEDIA_MAX:
            _cache_set(f"ms:{key}", data, _MEDIA_TTL)
    ext = m.s3_key.rsplit(".", 1)[-1].lower()
    mt = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    return Response(content=data, media_type=mt,
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "ETag": etag})


@app.get("/media/{media_id}")
async def media(media_id: int, request: Request) -> Response:
    """Ảnh S3-only (PG không còn base64). FE cache: ETag theo s3_key
    (content-hash, immutable) + Redis bytes cache 1 ngày (file ≤1MB)."""
    async with db.get_session() as session:
        a = await session.get(MediaAsset, media_id)
    if not a:
        raise HTTPException(404, "media not found")
    if not a.s3_key:
        if a.base64_data:  # hàng legacy trước thời S3-only
            data = storage.from_base64(a.base64_data)
            mt = "image/jpeg"
            return Response(content=data, media_type=mt,
                            headers={"Cache-Control": "public, max-age=86400"})
        raise HTTPException(404, "media has no s3 key")
    etag = f'"{hashlib.sha256(a.s3_key.encode()).hexdigest()[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    data = None
    if a.s3_key:
        hit = _cache_get(f"m:{media_id}")
        if hit:
            data = hit
    if data is None:
        data = await run_in_threadpool(storage.Storage().get_media, a.s3_key)
        if len(data) <= _MEDIA_MAX:
            _cache_set(f"m:{media_id}", data, _MEDIA_TTL)
    mt = "image/png" if a.s3_key.lower().endswith(".png") else "image/jpeg"
    return Response(content=data, media_type=mt,
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "ETag": etag})
