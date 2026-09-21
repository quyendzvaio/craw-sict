"""FastAPI: hybrid query endpoint + chat UI + SSE streaming + media serving.

Callers: uvicorn sict_crawler.api:app.
Schemas: POST /query {question, top_k} → {answer, context, sources[]}; GET / → chat UI;
         GET /media/{media_id} → image bytes from PG base64 (S3 fallback).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import db, embedder, storage, vector_store
from .models import MediaAsset

app = FastAPI(title="SICT RAG API")
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


def _query_cache_key(question: str, top_k: int) -> str:
    return "q:v1:" + hashlib.sha256(
        f"{question}\x00{top_k}".encode()).hexdigest()


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


class QueryIn(BaseModel):
    question: str
    top_k: int = 8
    image: str | None = None  # base64 (no data: prefix) — CLIP image query


class Source(BaseModel):
    url: str
    title: str
    kind: str  # text | image | video_frame
    score: float
    excerpt: str = ""
    media_id: int | None = None
    timestamp: float | None = None


def text_sources(text_hits: list[dict]) -> list[Source]:
    """E5 hits → Source. media_caption hits carry media refs."""
    sources: list[Source] = []
    for r in text_hits:
        if r.get("kind") == "media_caption":
            sources.append(Source(
                url=r.get("url", ""), title=r.get("title", ""),
                kind=r.get("media_kind", "image"), score=r["score"],
                excerpt=r.get("caption", ""), media_id=r.get("media_id"),
                timestamp=r.get("timestamp")))
        else:
            sources.append(Source(url=r["url"], title=r.get("title", ""), kind="text",
                                  score=r["score"], excerpt=r["content"][:300]))
    return sources


def media_sources(image_hits: list[dict]) -> list[Source]:
    """CLIP hits (sict_image) → Source."""
    return [Source(
        url=r.get("url", ""), title="", kind=r.get("kind", "image"),
        score=r["score"], excerpt=r.get("caption", r.get("s3_key", "")),
        media_id=r.get("media_id"), timestamp=r.get("timestamp"))
        for r in image_hits]


def _fuse_media(img_query_vecs: list[list[float]], terms: list[str],
                limit: int) -> list[Source]:
    """Image/video search: CLIP vec(s) ↔ sict_image + E5 keyword ↔ media captions.

    Ranks: each CLIP query vector + up to 3 keyword-matched caption points all
    compete for RRF positions; final top `limit` by fused rank. CLIP scores and
    E5 caption scores never mix numerically — RRF uses rank only.
    """
    from . import query_rewriter
    lists: list[list[dict]] = []
    for v in img_query_vecs:
        lists.append(vector_store.search_images(v, limit=limit))
    cap_hits: list[dict] = []
    for term in terms[:3]:
        try:
            cap_hits += vector_store.scroll_text_by_content(term, limit=limit)
        except Exception:
            pass
    # keep only media_caption hits — plain chunks are ranked in the text leg
    cap_hits = [h for h in cap_hits if h.get("kind") == "media_caption"]
    if cap_hits:
        lists.append(cap_hits[:limit])
    fused = query_rewriter._rrf(*lists) if lists else []
    return media_sources(fused[:limit])


def retrieve(question: str, top_k: int = 5,
             query_image_b64: str | None = None
             ) -> tuple[list[Source], list[Source]]:
    """Hybrid, per-space top_k: (text, media). Text: rewrite → E5 dense +
    keyword RRF → light E5 rerank. Media: CLIP (query text + optional query
    image) + keyword-matched captions, RRF-fused. Scores never cross groups.

    Kết quả cache Redis 10p theo (question, top_k, có/không ảnh query) —
    ảnh query base64 không đưa vào key (quá dài), request kèm ảnh luôn bỏ cache.
    """
    if not query_image_b64:
        ck = _query_cache_key(question, top_k)
        hit = _cache_get(ck)
        if hit:
            try:
                d = json.loads(hit)
                return ([Source(**s) for s in d["text"]],
                        [Source(**s) for s in d["media"]])
            except Exception:
                pass
    from . import query_rewriter
    r = query_rewriter.rewrite(question)

    # text leg: hybrid + light rerank
    tvec = embedder.embed_query(r["rewritten"])
    t_hits = query_rewriter.hybrid_search_text(tvec, r["terms"], limit=max(top_k * 2, 10))
    tsrcs = text_sources(query_rewriter.rerank_text(t_hits, r["rewritten"], top_n=top_k))

    # media leg: CLIP text + optional CLIP image + caption keywords
    img_query_vecs = [embedder.embed_text_clip([r["rewritten"]])[0]]
    if query_image_b64:
        qvec = embedder.embed_image_base64([query_image_b64])
        if qvec:
            img_query_vecs.append(qvec[0])
    msrcs = _fuse_media(img_query_vecs, r["terms"], top_k)
    if not query_image_b64:
        _cache_set(ck, json.dumps(
            {"text": [s.model_dump() for s in tsrcs],
             "media": [s.model_dump() for s in msrcs]}).encode(), _QUERY_TTL)
    return tsrcs, msrcs


def build_context(tsrcs: list[Source], msrcs: list[Source]) -> str:
    return "\n\n---\n\n".join(
        f"[{s.kind}] {s.title or s.url}\n{s.excerpt}" for s in tsrcs + msrcs)


@app.post("/query")
async def query(q: QueryIn):
    from . import agent as _agent
    res = _agent.run(q.question, q.top_k, q.image)
    return {"answer": None, "context": res["context"],
            "text": [s.model_dump() for s in res["text"]],
            "media": [s.model_dump() for s in res["media"]],
            "attempts": res["attempts"], "sufficient": res["sufficient"]}


@app.post("/chat")
async def chat(q: QueryIn):
    """SSE streaming qua agent loop: status (attempts) → sources event →
    streamed LLM answer (context echo fallback if no LLM key)."""
    from . import agent as _agent
    from . import vlm
    res = _agent.run(q.question, q.top_k, q.image)
    tsrcs, msrcs, context = res["text"], res["media"], res["context"]
    if not res["sufficient"]:
        context = ("(Lưu ý: tài liệu tìm được chỉ liên quan một phần, "
                   "câu trả lời có thể chưa đầy đủ.)\n\n" + context)

    async def gen():
        yield f"data: {json.dumps({'status': {'attempts': res['attempts'], 'sufficient': res['sufficient']}})}\n\n"
        yield f"data: {json.dumps({'text': [s.model_dump() for s in tsrcs], 'media': [s.model_dump() for s in msrcs]})}\n\n"
        import asyncio
        import queue
        import threading
        loop = asyncio.get_event_loop()
        q_out: queue.Queue = queue.Queue()
        SENTINEL = object()

        def worker():
            """vlm.stream_answer is a sync generator doing blocking HTTP."""
            try:
                for chunk in vlm.stream_answer(q.question, context):
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
            yield f"data: {json.dumps({'delta': item})}\n\n"
        if not got:  # no key / LLM fail → echo context
            answer = f"(LLM chưa trả lời được — đây là context truy xuất được)\n\n{context[:1500]}"
            for chunk in [answer[i:i+80] for i in range(0, len(answer), 80)]:
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
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
        st = storage.Storage()
        data = st.get_media(a.s3_key)
        if len(data) <= _MEDIA_MAX:
            _cache_set(f"m:{media_id}", data, _MEDIA_TTL)
    mt = "image/png" if a.s3_key.lower().endswith(".png") else "image/jpeg"
    return Response(content=data, media_type=mt,
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "ETag": etag})
