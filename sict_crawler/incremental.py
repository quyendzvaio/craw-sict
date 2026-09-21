"""Incremental crawl: chỉ xử lý thay đổi (thêm/sửa/xóa), không crawl lại đồ cũ.

Pipeline mỗi lần cron chạy:
  1. discover: re-fetch seed listing pages → frontier.push (seen trong Redis
     nên URL cũ tự skip, chỉ URL mới được queue).
  2. recheck: lấy N Document cũ nhất (crawled_at ASC) → re-fetch từng URL:
     - 404/410 → xóa doc + chunks/media (cascade FK) + Qdrant points.
     - hash đổi → _process_page (hàm này đã tự xóa stale chunks/media/vectors
       rồi insert lại, idempotent).
     - hash giữ nguyên → skip, chỉ bump crawled_at để lần sau không pick lại.
  3. drain: chạy frontier queue tới crawl_limit bằng _process_page.
  4. (caller cron gọi tiếp --annotate-ocr để caption ảnh mới.)

Callers: main.py --incremental, cron/incremental.sh, k8s CronJob.
S3/MinIO: dùng storage.Storage (raw html + media qua S3_ENDPOINT_URL khi set).
"""
from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import delete, select

from . import crawler, db, parser, storage, vector_store
from .frontier import Frontier
from .main import _notify, _process_page
from .models import Chunk, Document, MediaAsset


def _content_hash(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()


def should_delete(status: int | None) -> bool:
    """Chỉ xóa doc khi site trả 404/410 rõ ràng. Body rỗng với status khác
    (chặn bot, rate-limit, lỗi fetch) KHÔNG được xóa — caller đếm errors."""
    return status in (404, 410)


def mass_delete_abort(deleted: int, checked: int) -> bool:
    """Cầu chì: dừng pass khi tỉ lệ xóa bất thường (site outage/restructure)."""
    return deleted >= 20 and checked > 0 and deleted > checked * 0.2


async def discover_new() -> int:
    """Re-fetch seeds, queue link mới. Returns số URL mới."""
    from .config import get_settings

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
            print(f"[incremental/discover] skip {seed}: {e}")
    new = frontier.seen_count() - before
    print(f"[incremental/discover] {new} new URL(s) queued")
    return new


async def recheck_edits(limit: int = 100) -> dict:
    """Re-fetch N docs cũ nhất. Returns {checked, changed, deleted, unchanged, errors}.

    Mỗi doc cách ly lỗi riêng (rollback + đếm errors) để 1 doc kẹt Qdrant
    không giết cả chu kỳ 6h.
    """
    stats = {"checked": 0, "changed": 0, "deleted": 0, "unchanged": 0, "errors": 0}
    async with db.get_session() as session:
        ids = (await session.scalars(
            select(Document.id).order_by(Document.crawled_at.asc()).limit(limit)
        )).all()
    import datetime as dt

    for doc_id in ids:
        async with db.get_session() as session:
            try:
                doc = await session.get(Document, doc_id)
                if not doc:
                    continue
                stats["checked"] += 1
                await asyncio.sleep(1.0)  # politeness như drain (tránh rate-limit → body rỗng)
                try:
                    html, status = await crawler.fetch(doc.url)
                except Exception as e:
                    print(f"[incremental/recheck] fetch fail {doc.url}: {e}")
                    stats["errors"] += 1
                    continue
                if should_delete(status):
                    vector_store.delete_by_document(doc.id)
                    await session.delete(doc)
                    await session.commit()
                    _notify("crawl.delete", {"url": doc.url})
                    stats["deleted"] += 1
                    print(f"[incremental/deleted] {doc.url}")
                    if mass_delete_abort(stats["deleted"], stats["checked"]):
                        raise RuntimeError(
                            f"mass-delete guard: {stats['deleted']}/{stats['checked']} "
                            "deleted — nghi site outage/restructure, dừng pass để kiểm tra")
                    continue
                if not html:
                    # body rỗng nhưng status không phải 404/410 (chặn bot,
                    # rate-limit, lỗi fetch) → KHÔNG xóa, chỉ đếm errors.
                    print(f"[incremental/recheck] empty body (status={status}): {doc.url}")
                    stats["errors"] += 1
                    continue
                if _content_hash(html) == doc.content_hash:
                    doc.crawled_at = dt.datetime.now(dt.timezone.utc)
                    await session.commit()
                    stats["unchanged"] += 1
                    continue
                # changed → _process_page tự dọn stale rows rồi insert lại
                st = storage.Storage()
                await _process_page(session, st, doc.url, 0, None, html)
                stats["changed"] += 1
                print(f"[incremental/changed] {doc.url}")
            except Exception as e:
                await session.rollback()
                stats["errors"] += 1
                print(f"[incremental/recheck] doc {doc_id} error (isolated): {e}")
    return stats


async def drain_queue(crawl_limit: int = 200) -> int:
    """Crawl hết frontier queue (chủ yếu là URL mới từ discover)."""
    from .config import get_settings

    cfg = get_settings()
    frontier = Frontier()
    st = storage.Storage()
    vector_store.ensure_collections()
    await db.init_db()
    processed = 0
    limit = crawl_limit or cfg.crawl.max_pages
    async with db.get_session() as session:
        while processed < limit:
            item = frontier.pop()
            if not item:
                if frontier.size() == 0:
                    break
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
                    print(f"[incremental/drain {processed}/{limit}] depth={depth} {url}")
                elif status in (404, 410):
                    existing = await session.scalar(
                        select(Document).where(Document.url == url))
                    if existing:
                        vector_store.delete_by_document(existing.id)
                        await session.delete(existing)
                        await session.commit()
                        _notify("crawl.delete", {"url": url})
            except Exception as e:
                print(f"ERROR {url}: {e}")
    return processed


def _heartbeat(ok: bool, summary: dict) -> None:
    """Dead-man switch: redis incremental:last_ok / :last_error (JSON).

    Monitor: key last_ok quá cũ (>7h) hoặc last_error mới hơn last_ok
    nghĩa là chu kỳ fail. Best effort, không block pipeline.
    """
    import datetime as dt
    import json

    import redis

    from .config import get_settings

    try:
        r = redis.Redis.from_url(get_settings().redis_url,
                                 decode_responses=True, socket_timeout=5)
        payload = json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                              **summary})
        r.set("incremental:last_error" if not ok else "incremental:last_ok",
              payload)
    except Exception as e:
        print(f"[incremental] heartbeat skipped: {e}")


async def run_incremental(check_edits: int = 100, crawl_limit: int = 200,
                          annotate_ocr: int = 500) -> dict:
    """Entry cho cron: discover → recheck → drain → annotate-ocr.

    Mỗi stage cách ly lỗi: stage fail ghi errors + notify nhưng các stage
    còn lại vẫn chạy, cuối chu kỳ luôn heartbeat + summary.
    """
    from .annotate_ocr import run_annotate_ocr

    await db.init_db()
    vector_store.ensure_collections()
    summary: dict = {"stage_errors": []}
    try:
        summary["discovered"] = await discover_new()
    except Exception as e:
        summary["stage_errors"].append(f"discover: {e}")
        summary["discovered"] = 0
    try:
        summary.update(await recheck_edits(check_edits) if check_edits > 0 else {})
    except Exception as e:
        summary["stage_errors"].append(f"recheck: {e}")
    try:
        summary["drained"] = await drain_queue(crawl_limit)
    except Exception as e:
        summary["stage_errors"].append(f"drain: {e}")
        summary["drained"] = 0
    try:
        summary["ocr"] = await run_annotate_ocr(annotate_ocr) if annotate_ocr > 0 else 0
    except Exception as e:
        summary["stage_errors"].append(f"ocr: {e}")
        summary["ocr"] = 0
    ok = not summary["stage_errors"]
    print(f"[incremental] done: {summary}")
    _heartbeat(ok, summary)
    _notify("crawl.incremental" if ok else "crawl.incremental_error", summary)
    if not ok:
        raise RuntimeError(f"incremental stage errors: {summary['stage_errors']}")
    return summary
