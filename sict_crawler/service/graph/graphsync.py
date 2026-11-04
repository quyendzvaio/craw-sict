"""Graph khái niệm D33 — MỘT đường code dựng graph, Postgres là nguồn sự thật duy nhất.

Đầu vào đọc từ PG + Qdrant (không từ đối tượng trong bộ nhớ của loader/indexer):
  rebuild         đoạn của các collection chữ (chữ, bảng, caption) → cụm danh từ đoạn chữ (cache chunk_phrases)
                  → chọn khái niệm theo số bài (graph/concepts.py) → khớp danh bạ trên mọi đoạn → entities,
                  entity_links (mentions, theo đoạn) → payload Qdrant.
  sync_payloads   payload `entities` của đoạn chữ / caption SINH LẠI từ entity_links (ghi đè, chỉ ghi point khác);
                  gỡ `entities` / `entities_strong` khỏi point ảnh (graph luật cũ gắn tên người vào ảnh).
Lúc chạy: loader (bulk) và incremental (sau mỗi lượt ingest có bài mới / sửa / xoá). Không cập nhật từng bài:
lượt dựng cuối lượt ingest thay toàn bộ (khái niệm phụ thuộc số bài của cả corpus).

Usage:
  python -m sict_crawler.service.graph.graphsync rebuild
  python -m sict_crawler.service.graph.graphsync sync-payloads
  python -m sict_crawler.service.graph.graphsync stats
Callers: loader, incremental, api (load_gazetteer), CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import defaultdict

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sict_crawler.core.config import get_settings
from sict_crawler.model.orm import ChunkPhrase, Document, EntityLink, GraphEntity
from sict_crawler.repository import vector_store
from sict_crawler.service.graph import concepts as C
from sict_crawler.service.graph import graph as G


# ------------------------------------------------------------------ đọc đầu vào từ PG + Qdrant
def _scroll_chunks(known_docs: set[int]) -> list[C.ChunkText]:
    """Mọi đoạn của các collection chữ (chữ, bảng, caption). Caption khớp trên "tiêu đề. caption".
    Chỉ giữ đoạn của tài liệu có trong PG (entity_links trỏ khoá ngoài documents)."""
    client = vector_store.get_client()
    out: list[C.ChunkText] = []
    for coll in dict.fromkeys(vector_store.text_collections()):
        offset = None
        while True:
            pts, offset = client.scroll(coll, limit=1000, offset=offset,
                                        with_payload=["document_id", "chunk_id", "kind", "content", "caption",
                                                      "title"], with_vectors=False)
            for p in pts:
                pl = p.payload
                d = pl.get("document_id")
                if not isinstance(d, int) or d not in known_docs:
                    continue
                kind = pl.get("kind") or "text"
                text = pl.get("content") or pl.get("caption") or ""
                if kind == "media_caption":
                    text = f"{pl.get('title', '')}. {text}"
                out.append(C.ChunkText(pl.get("chunk_id") or str(p.id), d, kind, text))
            if offset is None:
                break
    return out


async def _known_docs(session) -> set[int]:
    return {i for (i,) in (await session.execute(select(Document.id).where(Document.hash_v2.is_not(None)))).all()}


async def _chunk_phrases(session, chunks: list[C.ChunkText]) -> dict[str, list[str]]:
    """Cụm danh từ từng đoạn, lấy từ cache chunk_phrases; đoạn mới / đổi nội dung mới tách (underthesea).
    Cache của đoạn không còn bị xoá."""
    have: dict[str, tuple[str, list[str]]] = {
        cid: (h, ph or []) for cid, h, ph in (await session.execute(
            select(ChunkPhrase.chunk_id, ChunkPhrase.text_hash, ChunkPhrase.phrases))).all()}
    out: dict[str, list[str]] = {}
    todo = []
    for c in chunks:
        h = C.text_hash(c.text)
        if have.get(c.chunk_id, ("",))[0] == h:
            out[c.chunk_id] = have[c.chunk_id][1]
        else:
            todo.append((c, h))
    if todo:
        try:
            import underthesea  # noqa: F401
        except ImportError as e:
            raise RuntimeError(f"graph khái niệm: {len(todo)} đoạn chưa tách cụm mà image này thiếu underthesea "
                               "(uv sync --group graph)") from e
        t0, batch = time.time(), []
        for n, (c, h) in enumerate(todo, 1):
            out[c.chunk_id] = C.noun_phrases(c.text)
            batch.append({"chunk_id": c.chunk_id, "text_hash": h, "phrases": out[c.chunk_id]})
            if len(batch) >= 500 or n == len(todo):
                stmt = pg_insert(ChunkPhrase).values(batch)
                await session.execute(stmt.on_conflict_do_update(
                    index_elements=["chunk_id"], set_={"text_hash": stmt.excluded.text_hash,
                                                       "phrases": stmt.excluded.phrases}))
                await session.commit()      # cache độc lập với graph: giữ tiến độ nếu lượt dựng hỏng giữa chừng
                batch = []
                print(f"[graphsync] tách cụm {n}/{len(todo)} đoạn, {time.time() - t0:.0f}s", flush=True)
    keep = {c.chunk_id for c in chunks}
    gone = [cid for cid in have if cid not in keep]
    for i in range(0, len(gone), 1000):
        await session.execute(delete(ChunkPhrase).where(ChunkPhrase.chunk_id.in_(gone[i:i + 1000])))
    return out


async def load_gazetteer(session) -> G.Gazetteer:
    rows = (await session.execute(select(GraphEntity.id, GraphEntity.kind, GraphEntity.key,
                                         GraphEntity.name, GraphEntity.aliases,
                                         GraphEntity.doc_count, GraphEntity.attrs))).all()
    return G.Gazetteer([G.Entity(kind=k, key=key, name=n, aliases=list(a or []), doc_count=c,
                                 id=i, attrs=dict(at or {}))
                        for i, k, key, n, a, c, at in rows])


# ------------------------------------------------------------------ ghi PG
async def upsert_entities(session, ents) -> dict[tuple[str, str], int]:
    rows = [{"kind": e.kind, "key": e.key[:300], "name": e.name[:300], "aliases": list(e.aliases),
             "doc_count": e.doc_count, "attrs": dict(e.attrs)} for e in ents]
    out: dict[tuple[str, str], int] = {}
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(GraphEntity).values(rows[i:i + 1000])
        stmt = stmt.on_conflict_do_update(
            index_elements=["kind", "key"],
            set_={c: stmt.excluded[c] for c in ("name", "aliases", "doc_count", "attrs")})
        res = await session.execute(stmt.returning(GraphEntity.id, GraphEntity.kind, GraphEntity.key))
        out.update({(k, key): i_ for i_, k, key in res.all()})
    return out


async def write_links(session, rows: list[dict]) -> None:
    """Thay TOÀN BỘ cạnh."""
    await session.execute(delete(EntityLink))
    for i in range(0, len(rows), 2000):
        await session.execute(pg_insert(EntityLink).values(rows[i:i + 2000]))


# ------------------------------------------------------------------ dựng
async def rebuild(session, sync: bool = True) -> dict:
    """Dựng lại toàn bộ graph khái niệm từ PG + Qdrant: thay mọi thực thể và cạnh, rồi payload."""
    t0 = time.time()
    tm: dict[str, float] = {}

    def lap(name: str) -> None:
        nonlocal t0
        tm[name] = round(time.time() - t0, 1)
        print(f"[graphsync] {name}: {tm[name]}s", flush=True)
        t0 = time.time()

    chunks = _scroll_chunks(await _known_docs(session))
    lap("load")
    src = [c for c in chunks if c.phrase_source]
    phrases = await _chunk_phrases(session, src)
    lap("phrases")
    pairs = [(c.document_id, phrases.get(c.chunk_id, [])) for c in src]
    n_docs = len({c.document_id for c in src})
    ents = C.select_concepts(pairs, n_docs, get_settings().graph.concept_df_max_ratio)
    eids = await upsert_entities(session, ents)
    keep = set(eids.values())
    stale = [i for (i,) in (await session.execute(select(GraphEntity.id))).all() if i not in keep]
    for i in range(0, len(stale), 1000):
        await session.execute(delete(GraphEntity).where(GraphEntity.id.in_(stale[i:i + 1000])))
    for e in ents:
        e.id = eids[(e.kind, e.key)]
    rows = C.link_rows(G.Gazetteer(ents), chunks, eids)
    lap("link")
    await write_links(session, rows)
    lap("write")
    await session.commit()
    stats = {"documents": n_docs, "chunks": len(chunks), "entities": len(ents), "removed_entities": len(stale),
             "entity_links": len(rows)}
    if sync:
        stats.update(await sync_payloads(session))
        lap("payload")
    stats["seconds"] = tm
    return stats


# ------------------------------------------------------------------ payload Qdrant từ PG
async def sync_payloads(session) -> dict:
    """Ghi đè payload `entities` của mọi đoạn chữ / caption theo entity_links (so như tập hợp, chỉ ghi point
    khác; gom point cùng payload vào 1 lệnh set_payload: 1 lệnh / point tốn ~70 ms, 1 lệnh / 500 point ~0,2 s).
    Point ảnh: gỡ `entities` / `entities_strong` (graph khái niệm không gắn vào ảnh)."""
    q = get_settings().qdrant
    client = vector_store.get_client()
    ek = {i: f"{k}:{key}" for i, k, key in (await session.execute(
        select(GraphEntity.id, GraphEntity.kind, GraphEntity.key))).all()}
    chunk_m: dict[str, set[str]] = defaultdict(set)
    for cid, e in (await session.execute(select(EntityLink.chunk_id, EntityLink.entity_id).where(
            EntityLink.rel == "mentions", EntityLink.chunk_id.is_not(None)))).all():
        if e in ek:
            chunk_m[cid].add(ek[e])
    out = {}
    for coll in dict.fromkeys(vector_store.text_collections()):
        groups: dict[tuple, list] = defaultdict(list)
        n = 0
        offset = None
        while True:
            pts, offset = client.scroll(coll, limit=1000, offset=offset, with_payload=["chunk_id", "entities"],
                                        with_vectors=False)
            for p in pts:
                n += 1
                want = chunk_m.get(p.payload.get("chunk_id") or str(p.id), set())
                if set(p.payload.get("entities") or ()) != want:
                    groups[tuple(sorted(want))].append(p.id)
            if offset is None:
                break
        changed = 0
        for keys, pids in groups.items():
            for i in range(0, len(pids), 1000):
                part = pids[i:i + 1000]
                vector_store._with_retry(lambda part=part, keys=keys, coll=coll: client.set_payload(
                    coll, {"entities": list(keys)}, points=part, wait=True))
                changed += len(part)
        out[coll] = {"points": n, "changed": changed, "writes": len(groups)}
    # ảnh / khung hình: bỏ tên do graph luật cũ gắn (chạy 1 lần có tác dụng, sau đó không còn point nào)
    stale, offset = [], None
    while True:
        pts, offset = client.scroll(q.collection_image, limit=1000, offset=offset,
                                    with_payload=["entities", "entities_strong"], with_vectors=False)
        stale += [p.id for p in pts if "entities" in p.payload or "entities_strong" in p.payload]
        if offset is None:
            break
    for i in range(0, len(stale), 1000):
        part = stale[i:i + 1000]
        vector_store._with_retry(lambda part=part: client.delete_payload(
            q.collection_image, keys=["entities", "entities_strong"], points=part, wait=True))
    res = {"payload_text": out[q.collection_text], "payload_media_cleared": len(stale)}
    if vector_store.ocr_collection() != q.collection_text:
        res["payload_ocr"] = out[vector_store.ocr_collection()]
    return res


async def graph_stats(session) -> dict:
    kinds = dict((await session.execute(select(GraphEntity.kind, func.count()).group_by(GraphEntity.kind))).all())
    return {"entities": kinds,
            "links": await session.scalar(select(func.count()).select_from(EntityLink)),
            "phrase_cache": await session.scalar(select(func.count()).select_from(ChunkPhrase))}


def main() -> None:
    from sict_crawler.repository import db
    ap = argparse.ArgumentParser(description="Graph khái niệm D33: dựng / đồng bộ payload / thống kê")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild")
    sub.add_parser("sync-payloads")
    sub.add_parser("stats")
    args = ap.parse_args()

    async def run():
        vector_store.require_v2()
        await db.init_db()
        async with db.get_session() as s:
            if args.cmd == "rebuild":
                return await rebuild(s)
            if args.cmd == "sync-payloads":
                return await sync_payloads(s)
            return await graph_stats(s)

    print(json.dumps(asyncio.run(run()), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
