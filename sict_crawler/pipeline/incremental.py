"""Incremental crawl: chỉ xử lý thay đổi (thêm/sửa/xóa), không crawl lại đồ cũ.

Pipeline mỗi lần cron chạy (12h/lần, ngân sách `crawl.cycle_budget_sec` tính từ đầu lượt cho drain +
recheck; hết giờ → dừng giữa chừng, phần còn lại để lượt sau — frontier nằm ở Redis nên không mất gì).
Đầu lượt chờ Postgres / Redis / Qdrant trả lời (`wait_for_services`, tối đa `crawl.startup_wait_sec`):
  máy vừa khởi động thì worker lên trước các store vài phút; trước đây lượt đó lỗi rồi ngủ 12h.
  0. URL mọi bài đã có trong DB → frontier:seen (`mark_known_seen`): link tới bài cũ không vào hàng đợi.
  1. discover: re-fetch seed pages → frontier.push (seen trong Redis nên URL cũ tự skip, chỉ URL mới
     được queue).
  1b. listings: tải lại trang 1 của MỌI mục danh sách (seed + mục rút ra từ URL bài trong DB + trang
     danh sách đã gặp), KHÔNG qua frontier:seen — seen không hết hạn nên trang danh sách đi qua frontier
     chỉ được tải 1 lần trong đời. Trang còn bài lạ (chưa có trong DB, chưa từng thấy) → đi tiếp trang 2,
     3… tới `crawl.listing_max_pages`: đăng dồn > 1 trang giữa 2 lượt / worker nghỉ vài ngày không mất bài.
  2. drain: chạy frontier queue tới crawl_limit bằng _process_page — chạy TRƯỚC recheck để bài mới
     không phải chờ. URL tải lỗi tạm thời (timeout, 5xx, thân rỗng, lỗi xử lý) được ghi lại và đưa vào
     hàng đợi ở các lượt sau, tối đa `crawl.max_fetch_retries` lần (trước đây: đã pop + vẫn trong seen →
     mất vĩnh viễn).
  3. recheck: lấy N Document cũ nhất (crawled_at ASC, trừ file đính kèm — file đi theo trang
     chứa nó) → re-fetch từng URL:
     - 404/410 → indexer.remove_document (bài + file đính kèm; point media chỉ gỡ id bài).
     - còn lại → _process_page (indexer.index_page, index spec v2): markdown sạch không đổi
       (hash_v2) → chỉ bump crawled_at; đổi → gỡ dữ liệu cũ của bài rồi ghi lại.
  4. graph (graphsync): mỗi 20h dựng lại toàn bộ; các lượt khác tính lại cạnh thực thể ↔ thực thể.
  4b. reconcile (mỗi 20h): đối soát PG ↔ Qdrant, dọn point mồ côi (reconcile.py).
  5. OCR: index v2 OCR ảnh/frame ngay khi index (media.ocr_status) → không còn pass OCR riêng;
     pass --annotate-ocr cũ chỉ chạy khi collection còn là v1.

Callers: main.py --incremental (service `cron` trong docker-compose.worker.yml).
S3/MinIO: dùng storage.Storage (raw html + media qua S3_ENDPOINT_URL khi set).
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

from sqlalchemy import or_, select

from sict_crawler.model.orm import Document
from sict_crawler.pipeline.main import _notify, _process_page
from sict_crawler.repository import db, storage, vector_store
from sict_crawler.repository.frontier import Frontier, canonicalize
from sict_crawler.service.crawl import crawler, parser
from sict_crawler.service.index import indexer


def should_delete(status: int | None) -> bool:
    """Chỉ xóa doc khi site trả 404/410 rõ ràng. Body rỗng với status khác
    (chặn bot, rate-limit, lỗi fetch) KHÔNG được xóa — caller đếm errors."""
    return status in (404, 410)


class MassDeleteAbort(RuntimeError):
    """Cầu chì xoá hàng loạt — phải thoát khỏi vòng recheck (không bị nuốt như lỗi 1 doc)."""


def mass_delete_abort(deleted: int, checked: int) -> bool:
    """Cầu chì: dừng pass khi tỉ lệ xóa bất thường (site outage/restructure)."""
    return deleted >= 20 and checked > 0 and deleted > checked * 0.2


async def mark_known_seen() -> int:
    """URL mọi bài trong DB → frontier:seen (Frontier.mark_seen). Trả số URL vừa thêm (lượt đầu sau
    loader ~1.600 — đo 2026-10-29, các lượt sau ~0)."""
    async with db.get_session() as session:
        urls = (await session.scalars(select(Document.url))).all()
    n = Frontier().mark_seen(urls)
    if n:
        print(f"[incremental/known] {n} URL bài đã có → frontier:seen")
    return n


async def discover_new() -> int:
    """Re-fetch seeds, queue link mới. Returns số URL mới."""
    from sict_crawler.core.config import get_settings

    cfg = get_settings()
    frontier = Frontier()
    before = frontier.seen_count()
    for seed in cfg.site.seed_urls:
        await asyncio.sleep(cfg.crawl.delay_seconds)
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


def listing_root(url: str) -> str | None:
    """URL trang danh sách / bài → gốc mục danh sách (https://host/vn/<mục>), None nếu không phải mục.
    Trang chủ /vn không phân trang → bỏ (seed đã tải mỗi lượt)."""
    from sict_crawler.service.crawl.urls import page_type
    u = urlparse(canonicalize(url))
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "vn" or parts[1] == "html" or parts[1].isdigit():
        return None
    root = f"{u.scheme}://{u.netloc}/vn/{parts[1]}"
    return root if page_type(root) == "listing" else None


async def _listing_roots(frontier: Frontier, doc_urls: list[str]) -> list[str]:
    """Mọi mục danh sách đã biết: seed + mục của các bài trong DB + trang danh sách từng gặp (seen)."""
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.crawl.urls import page_type
    cands = list(get_settings().site.seed_urls) + doc_urls
    cands += [u for u in frontier.seen_urls() if page_type(u) == "listing"]
    roots = {r for u in cands if (r := listing_root(u)) and not frontier.should_skip(r)}
    return sorted(roots)


async def refresh_listings(max_pages: int | None = None) -> dict:
    """Lỗi 1+2 (2026-10-18): tải lại trang danh sách của mọi mục, không phụ thuộc frontier:seen. Mọi link
    trang nội dung lạ (chưa có trong DB, push được vào frontier) đều vào hàng đợi; trang n còn bài lạ CỦA
    MỤC → tải tiếp trang n+1, không còn → dừng mục đó. Trả {roots, pages, new, errors}."""
    from sict_crawler.core.config import get_settings
    from sict_crawler.model.orm import Document
    from sict_crawler.service.crawl.urls import page_type
    cfg = get_settings().crawl
    max_pages = max_pages or cfg.listing_max_pages
    frontier = Frontier()
    async with db.get_session() as session:
        doc_urls = (await session.scalars(select(Document.url))).all()
    known = {canonicalize(u) for u in doc_urls}
    roots = await _listing_roots(frontier, list(doc_urls))
    stats = {"roots": len(roots), "pages": 0, "new": 0, "errors": 0}
    for root in roots:
        for n in range(1, max_pages + 1):
            url = root if n == 1 else f"{root}/{n}"
            await asyncio.sleep(cfg.delay_seconds)  # politeness như drain
            try:
                html, status = await crawler.fetch(url)
            except Exception as e:
                html, status = "", 0
                print(f"[incremental/listings] fetch fail {url}: {e}")
            if status != 200 or not html:
                stats["errors"] += status not in (404, 410)
                break
            stats["pages"] += 1
            new = own = 0
            for link in parser.parse_html(url, html).links:
                c = canonicalize(link)
                if c in known or page_type(c) != "content":
                    continue
                if frontier.push(c, 1):   # False: đã từng thấy (đang chờ / đã xử lý) hoặc bị chặn
                    new += 1
                    own += listing_root(c) == root
            stats["new"] += new
            if new:
                print(f"[incremental/listings] {new} URL mới tại {url} ({own} bài của mục)")
            # chỉ bài CỦA MỤC quyết định đi tiếp trang sau (menu / cột bên có link mục khác lặp ở mọi trang)
            if own == 0:
                break
    print(f"[incremental/listings] {stats}")
    return stats


async def recheck_edits(limit: int = 50, deadline: float | None = None) -> dict:
    """Re-fetch N docs cũ nhất. Returns {checked, changed, deleted, unchanged, errors}.

    Mỗi doc cách ly lỗi riêng (rollback + đếm errors) để 1 doc kẹt Qdrant
    không giết cả chu kỳ. Quá `deadline` (time.monotonic) → dừng, các doc chưa kiểm vẫn là cũ nhất nên
    lượt sau kiểm tiếp.
    """
    stats = {"checked": 0, "changed": 0, "deleted": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    async with db.get_session() as session:
        ids = (await session.scalars(
            select(Document.id)
            .where(or_(Document.doc_type.is_(None), Document.doc_type != "attachment"))
            .order_by(Document.crawled_at.asc()).limit(limit)
        )).all()
    st = storage.Storage()

    for doc_id in ids:
        if deadline is not None and time.monotonic() >= deadline:
            print(f"[incremental/recheck] hết ngân sách thời gian sau {stats['checked']} bài")
            break
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
                    url = doc.url
                    await indexer.remove_document(session, doc)
                    await session.commit()
                    _notify("crawl.delete", {"url": url})
                    stats["deleted"] += 1
                    print(f"[incremental/deleted] {url}")
                    if mass_delete_abort(stats["deleted"], stats["checked"]):
                        raise MassDeleteAbort(
                            f"mass-delete guard: {stats['deleted']}/{stats['checked']} "
                            "deleted — nghi site outage/restructure, dừng pass để kiểm tra")
                    continue
                if not html:
                    # body rỗng nhưng status không phải 404/410 (chặn bot,
                    # rate-limit, lỗi fetch) → KHÔNG xóa, chỉ đếm errors.
                    print(f"[incremental/recheck] empty body (status={status}): {doc.url}")
                    stats["errors"] += 1
                    continue
                res = await _process_page(session, st, doc.url, 0, None, html)
                if res in ("new", "changed"):
                    stats["changed"] += 1
                    print(f"[incremental/changed] {doc.url}")
                elif res == "unchanged":
                    stats["unchanged"] += 1
                else:
                    stats["skipped"] += 1  # listing / trùng / trang lỗi: không index
            except MassDeleteAbort:
                raise  # trước đây bị except bên dưới nuốt → cầu chì không dừng được pass
            except Exception as e:
                await session.rollback()
                stats["errors"] += 1
                print(f"[incremental/recheck] doc {doc_id} error (isolated): {e}")
    return stats


async def drain_queue(crawl_limit: int = 200, deadline: float | None = None) -> dict:
    """Crawl frontier queue (chủ yếu là URL mới từ discover / listings). URL tải lỗi tạm thời → ghi lại
    (frontier.fail) để lượt sau thử lại; 404/410 dứt điểm. Quá `deadline` (time.monotonic) → dừng, URL
    còn lại giữ nguyên trong hàng đợi. Trả {processed, failed, gave_up, requeued, left}."""
    from sict_crawler.core.config import get_settings

    cfg = get_settings()
    frontier = Frontier()
    st = storage.Storage()
    vector_store.ensure_collections()
    await db.init_db()
    stats = {"processed": 0, "failed": 0, "gave_up": 0, "requeued": frontier.requeue_failed()}
    processed = 0
    limit = crawl_limit or cfg.crawl.max_pages

    def failed(url: str, depth: int, why: str) -> None:
        stats["failed"] += 1
        if frontier.fail(url, depth, cfg.crawl.max_fetch_retries):
            print(f"[incremental/drain] lỗi tạm ({why}), thử lại lượt sau: {url}")
        else:
            stats["gave_up"] += 1
            print(f"[incremental/drain] bỏ sau {cfg.crawl.max_fetch_retries} lần lỗi ({why}): {url}")

    async with db.get_session() as session:
        while processed < limit:
            if deadline is not None and time.monotonic() >= deadline:
                print(f"[incremental/drain] hết ngân sách thời gian, còn {frontier.size()} URL cho lượt sau")
                break
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
                    res = await _process_page(session, st, url, depth, frontier, html)
                    frontier.succeeded(url)
                    processed += 1
                    print(f"[incremental/drain {processed}/{limit}] {res} depth={depth} {url}")
                elif status in (404, 410):
                    frontier.succeeded(url)  # dứt điểm, không thử lại
                    existing = await session.scalar(
                        select(Document).where(Document.url == url))
                    if existing:
                        await indexer.remove_document(session, existing)
                        await session.commit()
                        _notify("crawl.delete", {"url": url})
                else:
                    failed(url, depth, f"status={status}" if status else "timeout/không phải HTML/thân rỗng")
            except Exception as e:
                await session.rollback()
                print(f"ERROR {url}: {e}")
                failed(url, depth, type(e).__name__)
    stats["processed"] = processed
    stats["left"] = frontier.size()
    return stats


def _heartbeat(ok: bool, summary: dict) -> None:
    """Dead-man switch: redis incremental:last_ok / :last_error (JSON).

    Monitor: key last_ok quá cũ (> 13h, cron 12h — D31) hoặc last_error mới hơn last_ok
    nghĩa là chu kỳ fail. Best effort, không block pipeline.
    """
    import datetime as dt
    import json

    import redis

    from sict_crawler.core.config import get_settings

    try:
        r = redis.Redis.from_url(get_settings().redis_url,
                                 decode_responses=True, socket_timeout=5)
        payload = json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                              **summary})
        r.set("incremental:last_error" if not ok else "incremental:last_ok",
              payload)
    except Exception as e:
        print(f"[incremental] heartbeat skipped: {e}")


PROBE_TIMEOUT_S = 30


async def _probe_postgres() -> None:
    from sqlalchemy import text
    async with db.get_engine().connect() as conn:
        await conn.execute(text("select 1"))


async def _probe_redis() -> None:
    await asyncio.to_thread(lambda: _redis().ping())


async def _probe_qdrant() -> None:
    await asyncio.to_thread(lambda: vector_store.get_client().get_collections())


SERVICE_PROBES = {"postgres": _probe_postgres, "redis": _probe_redis, "qdrant": _probe_qdrant}


async def wait_for_services(timeout: float, interval: float = 10.0, probes: dict | None = None) -> list[str]:
    """Hỏi từng store tới khi trả lời hoặc hết `timeout` giây. Trả "tên: lỗi cuối" của store vẫn chưa lên
    (rỗng = sẵn sàng). Store đã trả lời thì không hỏi lại. Đo 2026-10-31 trên máy dev sau reboot:
    Postgres nhận kết nối sau ~5 phút, Qdrant còn khôi phục collection tới ~6 phút."""
    pending = dict(probes or SERVICE_PROBES)
    errors: dict[str, str] = {}
    end = time.monotonic() + timeout
    while True:
        for name, probe in list(pending.items()):
            try:
                await asyncio.wait_for(probe(), PROBE_TIMEOUT_S)
                pending.pop(name)
            except Exception as e:
                errors[name] = (str(e) or type(e).__name__)[:300]
        if not pending or time.monotonic() >= end:
            return [f"{name}: {errors[name]}" for name in pending]
        print(f"[incremental/startup] chờ {', '.join(pending)}…")
        await asyncio.sleep(interval)


def _abort_startup(errors: list[str]) -> None:
    summary = {"stage_errors": [f"startup: {e}" for e in errors]}
    print(f"[incremental] bỏ lượt: {summary}")
    _heartbeat(False, summary)
    _notify("crawl.incremental_error", summary)
    raise RuntimeError(f"incremental startup: {errors}")


RECONCILE_EVERY_SEC = 20 * 3600
GRAPH_REBUILD_EVERY_SEC = 20 * 3600


def _redis():
    import redis

    from sict_crawler.core.config import get_settings
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=5)


def _due(key: str, every: int) -> bool:
    import time
    try:
        return time.time() - float(_redis().get(key) or 0) >= every
    except Exception:
        return True


def _mark(key: str) -> None:
    import time
    try:
        _redis().set(key, str(time.time()))
    except Exception:
        pass


async def _graph_stage() -> dict | None:
    """Graph D20 sau khi index bài mới. Mỗi 20h: dựng lại toàn bộ (gộp biến thể toàn cục, nối bài cũ
    với thực thể mới, gỡ thực thể không còn bằng chứng, payload từ PG) — cùng đường code với bulk.
    Các lượt khác: chỉ tính lại cạnh thực thể ↔ thực thể (vài giây)."""
    from sict_crawler.service.graph import graphsync
    if not vector_store.has_sparse():
        return None
    async with db.get_session() as session:
        if _due("graph:last_rebuild", GRAPH_REBUILD_EVERY_SEC):
            stats = await graphsync.rebuild(session)
            _mark("graph:last_rebuild")
            return {"rebuild": stats}
        n = await graphsync.rebuild_relations(session)
        await session.commit()
        return {"entity_relations": n}


async def _maybe_reconcile() -> dict | None:
    """Đối soát PG ↔ Qdrant (reconcile.py) tối đa 1 lần / 20h — khoá Redis `reconcile:last`.
    Collection v1 → bỏ qua."""
    from sict_crawler.pipeline.reconcile import reconcile
    if not vector_store.has_sparse() or not _due("reconcile:last", RECONCILE_EVERY_SEC):
        return None
    stats = await reconcile()
    _mark("reconcile:last")
    return stats


async def run_incremental(check_edits: int = 50, crawl_limit: int = 200,
                          annotate_ocr: int = 500, budget_sec: int | None = None) -> dict:
    """Entry cho cron: discover → listings → drain → recheck → graph → reconcile → annotate-ocr.

    Mỗi stage cách ly lỗi: stage fail ghi errors + notify nhưng các stage
    còn lại vẫn chạy, cuối chu kỳ luôn heartbeat + summary. `budget_sec` (mặc định
    `crawl.cycle_budget_sec`, 0 = không giới hạn) tính từ lúc các store đã sẵn sàng, áp cho drain và
    recheck. Store không lên kịp / khởi tạo lỗi → bỏ lượt, ghi heartbeat lỗi.
    """
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.index.annotate_ocr import run_annotate_ocr

    cfg = get_settings().crawl
    down = await wait_for_services(cfg.startup_wait_sec)
    if down:
        _abort_startup(down)
    budget = cfg.cycle_budget_sec if budget_sec is None else budget_sec
    deadline = time.monotonic() + budget if budget > 0 else None
    try:
        await db.init_db()
        vector_store.ensure_collections()
    except Exception as e:
        _abort_startup([f"init: {e}"])
    summary: dict = {"stage_errors": []}
    try:
        summary["known_marked"] = await mark_known_seen()
    except Exception as e:
        summary["stage_errors"].append(f"mark_known: {e}")
    try:
        summary["discovered"] = await discover_new()
    except Exception as e:
        summary["stage_errors"].append(f"discover: {e}")
        summary["discovered"] = 0
    try:
        summary["listings"] = await refresh_listings()
    except Exception as e:
        summary["stage_errors"].append(f"listings: {e}")
    try:
        d = await drain_queue(crawl_limit, deadline)
        summary["drained"] = d.pop("processed")
        summary["drain"] = d
    except Exception as e:
        summary["stage_errors"].append(f"drain: {e}")
        summary["drained"] = 0
    try:
        summary.update(await recheck_edits(check_edits, deadline) if check_edits > 0 else {})
    except Exception as e:
        summary["stage_errors"].append(f"recheck: {e}")
    try:
        summary["graph"] = await _graph_stage()
    except Exception as e:
        summary["stage_errors"].append(f"graph: {e}")
    try:
        summary["reconcile"] = await _maybe_reconcile()
    except Exception as e:
        summary["stage_errors"].append(f"reconcile: {e}")
    try:
        # v2: OCR chạy ngay lúc index (indexer) → pass OCR backlog chỉ còn cho collection v1
        legacy = annotate_ocr > 0 and not vector_store.has_sparse()
        summary["ocr"] = await run_annotate_ocr(annotate_ocr) if legacy else 0
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
