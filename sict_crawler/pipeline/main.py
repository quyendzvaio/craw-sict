"""Pipeline entry: crawl SICT tree → parse → chunk → embed → PG + Qdrant + S3.

Usage:
  python -m sict_crawler.pipeline.main --crawl-limit 20
  python -m sict_crawler.pipeline.main --full
  python -m sict_crawler.pipeline.main --annotate-ocr 100

Callers: CLI.
Schemas: orchestrates frontier/crawler → indexer (index spec v2). --rechunk/--videos-backfill
chỉ cho index v1 (từ chối khi collection là v2).
"""
from __future__ import annotations

import argparse
import asyncio
import os

import httpx
from sqlalchemy import delete, func, select

from sict_crawler.core.config import get_settings
from sict_crawler.model.orm import Chunk, Document, MediaAsset
from sict_crawler.repository import db, storage, vector_store
from sict_crawler.repository.frontier import Frontier
from sict_crawler.service.crawl import crawler, parser
from sict_crawler.service.embedding import embedder
from sict_crawler.service.index import video
from sict_crawler.service.index.chunker import chunk_text


def _notify(event: str, data: dict) -> None:
    """POST change event to SICT_WEBHOOK_URL when set. Best effort."""
    hook = os.environ.get("SICT_WEBHOOK_URL")
    if not hook:
        return
    try:
        httpx.post(hook, json={"event": event, **data}, timeout=10)
    except Exception as e:
        print(f"[webhook] {event} failed: {e}")


async def _process_page(session, st: storage.Storage, url: str, depth: int,
                        frontier: Frontier | None, html: str) -> str:
    """1 trang → index spec v2 (indexer.index_page: cùng quy tắc + id với bulk compute/loader).
    Idempotent: markdown sạch không đổi → bỏ qua; đổi → gỡ dữ liệu cũ của bài rồi ghi lại.
    Trả trạng thái (new | changed | unchanged | duplicate | listing | error_page | ...)."""
    from sict_crawler.service.index import indexer
    status = await indexer.index_page(session, st, url, html, depth, frontier)
    if status == "changed":
        _notify("crawl.edit", {"url": url})
    return status


def _legacy_v1_only(cmd: str) -> bool:
    """Lệnh bảo trì index v1 (chunk/media_assets cũ). Collection đang trỏ là v2 → từ chối để không
    ghi payload v1 vào collection v2. Re-index v2 = snapshot → compute → loader."""
    if vector_store.has_sparse():
        print(f"[{cmd}] bỏ qua: lệnh chỉ dành cho index v1, collection hiện tại là schema v2 "
              "(dùng snapshot → compute → loader)")
        return True
    return False


async def run_refresh() -> int:
    """Light incremental pass: re-fetch seed listing pages, queue newly-discovered
    URLs only (frontier:seen dedups). Returns count of new URLs. Outbound webhook
    via SICT_WEBHOOK_URL when set."""
    cfg = get_settings()
    frontier = Frontier()
    before = frontier.seen_count()
    for seed in cfg.site.seed_urls:
        try:
            html, status = await crawler.fetch(seed)
            if status == 200 and html:
                for link in parser.parse_html(seed, html).links:
                    frontier.push(link, 1)
        except Exception as e:
            print(f"[refresh] skip {seed}: {e}")
    new = frontier.seen_count() - before
    print(f"[refresh] {new} new URL(s) queued")
    if new:
        _notify("crawl.post", {"count": new})
    return new


async def run(crawl_limit: int | None = None) -> None:
    cfg = get_settings()
    frontier = Frontier()
    st = storage.Storage()
    vector_store.ensure_collections()
    await db.init_db()

    for seed in cfg.site.seed_urls:
        frontier.push(seed, 0)

    processed = 0
    limit = crawl_limit or cfg.crawl.max_pages
    async with db.get_session() as session:
        while processed < limit:
            item = frontier.pop()
            if not item:
                await asyncio.sleep(1)
                if frontier.size() == 0:
                    break
                continue
            url, depth = item
            try:
                html, status = await crawler.fetch(url)
                if status == 200 and html:
                    res = await _process_page(session, st, url, depth, frontier, html)
                    processed += 1
                    print(f"[{processed}/{limit}] {res} depth={depth} {url}")
                elif status in (404, 410):
                    existing = await session.scalar(
                        select(Document).where(Document.url == url))
                    if existing:
                        from sict_crawler.service.index import indexer
                        await indexer.remove_document(session, existing)
                        await session.commit()
                        _notify("crawl.delete", {"url": url})
                        print(f"[deleted] {url}")
            except Exception as e:
                print(f"ERROR {url}: {e}")


async def run_rechunk(limit: int) -> None:
    """Re-chunk docs đã có bằng semantic chunking (chunk cũ không có marker
    meta.chunking='sem'). Idempotent: doc đã gắn marker thì skip; raw html mất
    (disk cleanup) thì skip luôn — không re-fetch 1000 trang.

    Session mới mỗi chunk: 1 session cho 2.8K docs → identity map giữ hết
    Document+Chunk objects (chunk.content ~5KB mỗi cái) → OOM.
    """
    if _legacy_v1_only("rechunk"):
        return
    cfg = get_settings()
    st = storage.Storage()
    vector_store.ensure_collections()
    await db.init_db()
    done, chunk = 0, 50
    while done < limit:
        async with db.get_session() as session:
            ids = (await session.scalars(
                select(Document.id).where(
                    func.coalesce(Document.meta["chunking"].as_string(), "") != "sem")
                .order_by(Document.id).limit(chunk))).all()
            if not ids:
                break
            for doc_id in ids:
                doc = await session.get(Document, doc_id)
                if not doc or (doc.meta or {}).get("chunking") == "sem":
                    continue
                try:
                    raw = st.read_raw_html(doc.raw_s3_key) if doc.raw_s3_key else None
                except FileNotFoundError:
                    raw = None
                if not raw:
                    # doc không còn nguồn → đánh dấu để SQL filter không pick lại mãi
                    doc.meta = {**(doc.meta or {}), "chunking": "skip"}
                    await session.commit()
                    continue
                parsed = parser.parse_html(doc.url, raw.decode(errors="replace"))
                table_md = "\n\n".join(
                    "| " + " | ".join(row) + " |" for t in parsed.tables for row in t)
                full_text = parsed.markdown + ("\n\n" + table_md if table_md else "")
                chunks = chunk_text(full_text, parsed.title,
                                    cfg.chunking.min_tokens, cfg.chunking.max_tokens,
                                    cfg.chunking.overlap_ratio, cfg.chunking.semantic)
                await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
                # chunk points không có key 'kind' → delete_text_chunks giữ
                # point media_caption của cùng doc trong collection sict_text
                vector_store.delete_text_chunks(doc.id)
                if chunks:
                    vectors = await asyncio.to_thread(
                        embedder.embed_texts, [c.text for c in chunks])
                    rows = [{"vector": v, "document_id": doc.id, "chunk_id": i,
                             "url": doc.url, "title": parsed.title, "heading": c.heading,
                             "content": c.text}
                            for i, (c, v) in enumerate(zip(chunks, vectors))]
                    pids = vector_store.upsert_text_chunks(rows)
                    for c, pid in zip(chunks, pids):
                        session.add(Chunk(document_id=doc.id, content=c.text,
                                          heading=c.heading, chunk_index=c.index,
                                          token_count=len(c.text) // 4, vector_id=pid))
                doc.meta = {**(doc.meta or {}), "chunking": "sem"}
                await session.commit()  # per-doc: chết giữa đường không mất gì
                done += 1
                print(f"[rechunk] {done}/{limit} doc={doc.id} chunks={len(chunks)}", flush=True)
    print(f"rechunked {done} documents")

async def run_video_backfill(limit: int) -> None:
    """Re-fetch documents, extract video keyframes missed by earlier runs
    (embed URLs failed silently before yt-dlp support). Idempotent: docs that
    already have video_frame assets are skipped."""
    if _legacy_v1_only("videos-backfill"):
        return
    cfg = get_settings()
    st = storage.Storage()
    vector_store.ensure_collections()
    await db.init_db()
    async with db.get_session() as session:
        doc_ids = (await session.scalars(
            select(Document.id).order_by(Document.id))).all()
        done = 0
        for i, doc_id in enumerate(doc_ids):
            if done >= limit:
                break
            doc = await session.get(Document, doc_id)
            if not doc or not doc.raw_s3_key:
                continue
            existing = await session.scalar(select(func.count(MediaAsset.id)).where(
                MediaAsset.document_id == doc.id,
                MediaAsset.kind == "video_frame"))
            if existing:
                continue
            try:
                try:
                    raw = await asyncio.to_thread(st.read_raw_html, doc.raw_s3_key)
                except FileNotFoundError:
                    # raw html lost (disk cleanup) — re-fetch the page
                    raw, status = await crawler.fetch(doc.url)
                    if status != 200 or not raw:
                        continue
                video_urls = parser.parse_html(doc.url, raw).video_urls[:5]
                if not video_urls:
                    continue
                print(f"[vbackfill] {doc.url} → {len(video_urls)} video(s)")
                new_media: list[MediaAsset] = []
                for vid_url in video_urls:
                    try:
                        frames = await asyncio.to_thread(video.extract_keyframes, vid_url)
                        if not frames:
                            continue
                        vecs, ok_idx = embedder.embed_image_bytes([f for f, _ in frames])
                        frames = [frames[j] for j in ok_idx]
                        kept = video.dedup_indices(vecs, cfg.video.dedup_threshold)
                        assets = []
                        # subtitle-only: có thì gắn sẵn vào caption, OCR pass sau sẽ append thêm
                        # title video luôn gắn (| vid:) dù không có sub
                        try:
                            info = await asyncio.to_thread(
                                video.fetch_video_info, vid_url)
                        except Exception:
                            info = {}
                        subs = (info.get("subs") or "") if info else ""
                        vtitle = (info.get("title") or "") if info else ""
                        extra = " ".join(p for p in (
                            ("| vid: " + vtitle) if vtitle else "",
                            ("| sub: " + subs) if subs else "") if p)
                        sub_cap = (f"{doc.title}. {extra}"[:900]
                                   if extra and doc.title else extra[:900])
                        for j in kept:
                            key, _b64 = st.put_media(frames[j][0], "png")
                            a = MediaAsset(document_id=doc.id, kind="video_frame",
                                           s3_key=key, base64_data="",
                                           source_url=vid_url, timestamp=frames[j][1],
                                           caption=sub_cap)
                            session.add(a)
                            assets.append(a)
                        await session.flush()
                        rows = [{"vector": vecs[j], "media_id": a.id,
                                 "document_id": doc.id, "url": vid_url,
                                 "kind": "video_frame", "s3_key": a.s3_key,
                                 "timestamp": a.timestamp}
                                for j, a in zip(kept, assets)]
                        if rows:
                            ids = vector_store.upsert_images(rows)
                            for a, pid in zip(assets, ids):
                                a.vector_id = pid
                        new_media.extend(assets)
                    except Exception as e:
                        print(f"[video] skip {vid_url}: {e}")
                await session.commit()
                done += 1
            except Exception as e:
                print(f"[vbackfill] error {doc.url}: {e}")
        print(f"[vbackfill] processed {done} doc(s) with video")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawl-limit", type=int, default=None)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--reset", action="store_true", help="clear frontier and exit")
    ap.add_argument("--refresh", action="store_true",
                    help="incremental: re-fetch seeds, queue new URLs, fire webhook")

    ap.add_argument("--annotate-ocr", type=int, default=None, metavar="N",
                    help="OCR-only caption N pending media (title+OCR, no face/VLM) and exit")
    ap.add_argument("--incremental", action="store_true",
                    help="cron job: discover new + recheck edits/deletes + drain + OCR")
    ap.add_argument("--check-edits", type=int, default=50,
                    help="docs re-fetched per incremental run (oldest first)")
    ap.add_argument("--budget-sec", type=int, default=None,
                    help="incremental: ngân sách thời gian drain + recheck (giây, mặc định crawl.cycle_budget_sec, 0 = không giới hạn)")
    ap.add_argument("--annotate-ocr-n", type=int, default=500,
                    help="OCR assets per incremental run")
    ap.add_argument("--videos-backfill", type=int, default=None, metavar="N",
                    help="extract keyframes for N docs with videos missed earlier")
    ap.add_argument("--rechunk", type=int, default=None, metavar="N",
                    help="re-chunk N text docs with semantic chunking and exit")
    args = ap.parse_args()
    if args.refresh:
        asyncio.run(run_refresh())
        return
    if args.reset:
        Frontier().reset()
        print("Frontier cleared.")
        return
    if args.annotate_ocr is not None:
        from sict_crawler.service.index.annotate_ocr import run_annotate_ocr
        asyncio.run(run_annotate_ocr(args.annotate_ocr))
        return
    if args.incremental:
        from sict_crawler.pipeline.incremental import run_incremental
        limit = args.crawl_limit if args.crawl_limit is not None else 200
        asyncio.run(run_incremental(
            check_edits=args.check_edits, crawl_limit=limit,
            annotate_ocr=args.annotate_ocr_n, budget_sec=args.budget_sec))
        return
    if args.videos_backfill is not None:
        asyncio.run(run_video_backfill(args.videos_backfill))
        return
    if args.rechunk is not None:
        asyncio.run(run_rechunk(args.rechunk))
        return
    asyncio.run(run(None if args.full else args.crawl_limit))


if __name__ == "__main__":
    main()
