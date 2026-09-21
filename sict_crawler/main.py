"""Pipeline entry: crawl SICT tree → parse → chunk → embed → PG + Qdrant + S3.

Usage:
  python -m sict_crawler.main --crawl-limit 20
  python -m sict_crawler.main --full
  python -m sict_crawler.main --annotate-ocr 100

Callers: CLI.
Schemas: orchestrates frontier/crawler/parser/chunker/embedder/db/vector_store/storage/video.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os

import httpx
from sqlalchemy import delete, func, select

from . import crawler, db, embedder, parser, storage, vector_store, video
from .chunker import chunk_text
from .config import get_settings
from .frontier import Frontier
from .models import Chunk, Document, MediaAsset


def _content_hash(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()


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
                        frontier: Frontier | None, html: str) -> None:
    cfg = get_settings()
    parsed = parser.parse_html(url, html)

    # skip unchanged content
    chash = _content_hash(html)
    existing = await session.scalar(select(Document).where(Document.url == url))
    if existing and existing.content_hash == chash:
        return

    if existing:  # content changed → drop stale chunks/media/vectors first
        vector_store.delete_by_document(existing.id)
        await session.execute(delete(Chunk).where(Chunk.document_id == existing.id))
        await session.execute(delete(MediaAsset).where(MediaAsset.document_id == existing.id))
        await session.flush()
        print(f"[changed] {url}")

    raw_key = st.put_raw_html(url, html.encode())

    doc = existing or Document(url=url)
    doc.title = parsed.title[:512]
    doc.raw_s3_key = raw_key
    doc.content_hash = chash
    doc.meta = {"tables": len(parsed.tables), "chunking": "sem"}
    session.add(doc)
    await session.flush()

    # tables → markdown, append to text corpus
    table_md = "\n\n".join(
        "| " + " | ".join(row) + " |" for t in parsed.tables for row in t)
    full_text = parsed.markdown + ("\n\n" + table_md if table_md else "")

    # chunks → embed → qdrant
    chunks = chunk_text(full_text, parsed.title,
                        cfg.chunking.min_tokens, cfg.chunking.max_tokens,
                        cfg.chunking.overlap_ratio, cfg.chunking.semantic)
    if chunks:
        vectors = embedder.embed_texts([c.text for c in chunks])
        rows = [{"vector": v, "document_id": doc.id, "chunk_id": i,
                 "url": url, "title": parsed.title, "heading": c.heading,
                 "content": c.text} for i, (c, v) in enumerate(zip(chunks, vectors))]
        ids = vector_store.upsert_text_chunks(rows)
        for c, pid in zip(chunks, ids):
            session.add(Chunk(document_id=doc.id, content=c.text, heading=c.heading,
                              chunk_index=c.index, token_count=len(c.text) // 4,
                              vector_id=pid))

    # images → S3-only (không lưu base64 vào PG) + base64 giữ trong RAM để embed
    # alt text HTML thành caption gốc để ảnh luôn đính kèm chú thích
    new_imgs: list[MediaAsset] = []
    new_bin: list[bytes] = []
    for img_url, alt in zip(parsed.image_urls[:20], parsed.image_alts[:20]):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as hc:
                r = await hc.get(img_url)
                if r.status_code != 200 or len(r.content) < 1000:
                    continue
                ext = img_url.rsplit(".", 1)[-1].lower()[:4] or "jpg"
                key, _b64 = st.put_media(r.content, ext)
                a = MediaAsset(document_id=doc.id, kind="image", s3_key=key,
                               base64_data="", source_url=img_url,
                               caption=alt if len(alt) >= 3 else "")
                session.add(a)
                new_imgs.append(a)
                new_bin.append(r.content)
        except Exception:
            continue
    await session.flush()
    new_media: list[MediaAsset] = new_imgs
    if new_media:
        vecs, ok_idx = embedder.embed_image_bytes(new_bin)
        rows = [{"vector": v, "media_id": new_media[j].id, "document_id": doc.id,
                 "url": new_media[j].source_url, "kind": "image",
                 "s3_key": new_media[j].s3_key,
                 "caption": new_media[j].caption or ""}
                for j, v in zip(ok_idx, vecs)]
        if rows:
            ids = vector_store.upsert_images(rows)
            for j, pid in zip(ok_idx, ids):
                new_media[j].vector_id = pid

    # videos → keyframes (deduped, timestamped) → same image path, kind=video_frame
    for vid_url in parsed.video_urls[:5]:
        try:
            frames = video.extract_keyframes(vid_url)  # [(bytes, pts_sec)]
            if not frames:
                continue
            vecs, ok_idx = embedder.embed_image_bytes([f for f, _ in frames])
            frames = [frames[i] for i in ok_idx]
            kept = video.dedup_indices(vecs, cfg.video.dedup_threshold)
            assets: list[MediaAsset] = []
            for i in kept:
                key, _b64 = st.put_media(frames[i][0], "png")
                a = MediaAsset(document_id=doc.id, kind="video_frame", s3_key=key,
                               base64_data="", source_url=vid_url,
                               timestamp=frames[i][1])
                session.add(a)
                assets.append(a)
            await session.flush()
            rows = [{"vector": vecs[i], "media_id": a.id, "document_id": doc.id,
                     "url": vid_url, "kind": "video_frame", "s3_key": a.s3_key,
                     "timestamp": a.timestamp, "caption": ""}
                    for i, a in zip(kept, assets)]
            if rows:
                ids = vector_store.upsert_images(rows)
                for a, pid in zip(assets, ids):
                    a.vector_id = pid
            new_media.extend(assets)
        except Exception as e:
            print(f"[video] skip {vid_url}: {e}")
            continue

    # caption OCR-only (title + OCR text, không face/VLM) — best effort
    # bytes đọc từ S3 (S3-only, base64 PG luôn "")
    caption_rows: list[dict] = []
    try:
        if new_media:
            from . import annotate_ocr as _annotate_ocr
            items = [(a, (storage.from_base64(a.base64_data) if a.base64_data
                          else st.get_media(a.s3_key))) for a in new_media]
            caption_rows = await _annotate_ocr.annotate_ocr_batch(
                session, st, items,
                doc_title=parsed.title, doc_text=full_text[:1000])
    except Exception as e:
        print(f"annotate skipped: {e}")

    await session.commit()
    # sync caption PG → payload sict_image để /query media leg hiện chú thích
    try:
        pairs = [(a.vector_id, a.caption) for a in new_media
                 if a.vector_id and a.caption]
        if pairs:
            vector_store.set_image_captions(pairs)
    except Exception as e:
        print(f"caption payload sync skipped: {e}")
    if existing:
        _notify("crawl.edit", {"url": url})

    # caption points → sict_text (E5) so "thầy A" text queries hit media
    if caption_rows:
        try:
            vectors = embedder.embed_texts([r["title"] + ". " + r["caption"]
                                            for r in caption_rows])
            vector_store.upsert_media_captions(
                [{**r, "vector": v} for r, v in zip(caption_rows, vectors)])
        except Exception as e:
            print(f"caption upsert skipped: {e}")

    # discover child links
    if frontier is not None:
        for link in parsed.links:
            frontier.push(link, depth + 1)


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
                    await _process_page(session, st, url, depth, frontier, html)
                    processed += 1
                    print(f"[{processed}/{limit}] depth={depth} {url}")
                elif status in (404, 410):
                    existing = await session.scalar(
                        select(Document).where(Document.url == url))
                    if existing:
                        vector_store.delete_by_document(existing.id)
                        await session.delete(existing)  # chunks/media cascade theo FK
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
                        for j in kept:
                            key, _b64 = st.put_media(frames[j][0], "png")
                            a = MediaAsset(document_id=doc.id, kind="video_frame",
                                           s3_key=key, base64_data="",
                                           source_url=vid_url, timestamp=frames[j][1])
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
    ap.add_argument("--check-edits", type=int, default=100,
                    help="docs re-fetched per incremental run (oldest first)")
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
        from .annotate_ocr import run_annotate_ocr
        asyncio.run(run_annotate_ocr(args.annotate_ocr))
        return
    if args.incremental:
        from .incremental import run_incremental
        limit = args.crawl_limit if args.crawl_limit is not None else 200
        asyncio.run(run_incremental(
            check_edits=args.check_edits, crawl_limit=limit,
            annotate_ocr=args.annotate_ocr_n))
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
