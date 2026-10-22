"""FastAPI: hybrid query endpoint + chat UI + SSE streaming + media serving.

Callers: uvicorn sict_crawler.api.routes:app.
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
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from sict_crawler.model.orm import MediaAsset
from sict_crawler.model.schemas import QueryIn
from sict_crawler.repository import cache, db, storage
from sict_crawler.service.retrieval import citations

app = FastAPI(title="SICT RAG API")
from sict_crawler.api import metrics as _metrics  # noqa: E402 — D13: GET /metrics (HTTP + pipeline) cho Alloy / Grafana Cloud

_metrics.instrument(app)
_GRAPH_REFRESH_SEC = 600


async def _refresh_graph() -> None:
    """Nạp danh bạ thực thể (graph D20) từ Postgres. Lỗi/không có PG → giữ bản cũ (hoặc tắt graph)."""
    import asyncio

    from sict_crawler.service.graph import graph as G
    from sict_crawler.service.graph import graphsync
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

_MEDIA_TTL = 86400    # bytes ảnh theo media_id (≤1MB mới cache)
_MEDIA_MAX = 1_000_000


IMAGE_ONLY_QUESTION = "Người dùng gửi một ảnh, không kèm câu hỏi. Hãy cho biết thông tin liên quan đến ảnh này."


@app.post("/query")
async def query(q: QueryIn):
    from sict_crawler.service.retrieval import agent as _agent
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
    from sict_crawler.service.retrieval import agent as _agent
    from sict_crawler.service.retrieval import vlm
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
    from sict_crawler.model.orm import Media
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise HTTPException(404, "media not found")
    etag = f'"{key[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    data = cache.get(f"ms:{key}")
    async with db.get_session() as session:
        m = await session.scalar(select(Media).where(Media.sha256 == key))
    if m is None or not m.s3_key:
        raise HTTPException(404, "media not found")
    if data is None:
        data = await run_in_threadpool(storage.Storage().get_media, m.s3_key)   # S3 đồng bộ: không chặn event loop
        if len(data) <= _MEDIA_MAX:
            cache.put(f"ms:{key}", data, _MEDIA_TTL)
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
        hit = cache.get(f"m:{media_id}")
        if hit:
            data = hit
    if data is None:
        data = await run_in_threadpool(storage.Storage().get_media, a.s3_key)
        if len(data) <= _MEDIA_MAX:
            cache.put(f"m:{media_id}", data, _MEDIA_TTL)
    mt = "image/png" if a.s3_key.lower().endswith(".png") else "image/jpeg"
    return Response(content=data, media_type=mt,
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "ETag": etag})
