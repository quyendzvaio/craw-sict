"""Đối soát Postgres ↔ Qdrant (AC-E6, D8) — chạy hằng đêm từ incremental, hoặc tay.

Ghi PG và Qdrant không cùng giao dịch, nên lỗi giữa chừng để lại:
  1. point chữ có document_id không còn trong PG (bài đã xoá / giao dịch PG rollback) → xoá point;
  2. point media có document_id không còn trong PG → gỡ id, point không còn bài nào → xoá;
  3. bài có hash_v2 (đã index xong) nhưng 0 point chữ dù n_chunks > 0 → đặt hash_v2 = NULL để lần
     recheck sau index lại (không tự embed ở đây).
Cầu chì: tỉ lệ point mồ côi > MAX_ORPHAN_RATIO (VD trỏ nhầm DB rỗng) → dừng, không xoá gì.

Usage: python -m sict_crawler.reconcile [--dry-run]
Callers: incremental.run_incremental (mỗi 24h, khoá Redis reconcile:last), CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select, update

from . import db, vector_store
from .config import get_settings
from .models import Document

MAX_ORPHAN_RATIO = 0.2


class ReconcileAbort(RuntimeError):
    pass


def _scroll(collection: str):
    client = vector_store.get_client()
    offset = None
    while True:
        pts, offset = client.scroll(collection, limit=2000, offset=offset,
                                    with_payload=["document_id"], with_vectors=False)
        yield from pts
        if offset is None:
            break


async def reconcile(dry_run: bool = False) -> dict:
    vector_store.require_v2()
    q = get_settings().qdrant
    client = vector_store.get_client()
    async with db.get_session() as s:
        rows = (await s.execute(select(Document.id, Document.hash_v2, Document.meta))).all()
    pg_ids = {r[0] for r in rows}

    text_total, orphan_text = 0, []          # orphan_text: (collection, point id) — chunk (text) + caption (ocr)
    text_docs: set[int] = set()
    for tc in vector_store.text_collections():
        for p in _scroll(tc):
            text_total += 1
            d = p.payload.get("document_id")
            if d in pg_ids:
                if tc == q.collection_text:
                    text_docs.add(d)
            else:
                orphan_text.append((tc, p.id))
    img_total, img_fix, img_drop = 0, {}, []
    for p in _scroll(q.collection_image):
        img_total += 1
        ids = vector_store._doc_ids(p.payload.get("document_id"))
        keep = [d for d in ids if d in pg_ids]
        if len(keep) != len(ids):
            if keep:
                img_fix[p.id] = keep
            else:
                img_drop.append(p.id)
    stale_docs = [i for i, h, meta in rows
                  if h and (meta or {}).get("n_chunks") and i not in text_docs]
    stats = {"pg_docs": len(pg_ids), "text_points": text_total, "orphan_text": len(orphan_text),
             "image_points": img_total, "image_detach": len(img_fix), "image_orphan": len(img_drop),
             "docs_missing_points": len(stale_docs), "dry_run": dry_run}
    ratio = (len(orphan_text) + len(img_drop)) / max(1, text_total + img_total)
    if ratio > MAX_ORPHAN_RATIO:
        stats["aborted"] = f"orphan ratio {ratio:.0%} > {MAX_ORPHAN_RATIO:.0%}"
        raise ReconcileAbort(json.dumps(stats, ensure_ascii=False))
    if not dry_run:
        for tc in vector_store.text_collections():
            ids = [pid for c, pid in orphan_text if c == tc]
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                vector_store._with_retry(lambda part=part, tc=tc: client.delete(tc, points_selector=part))
        for pid, keep in img_fix.items():
            vector_store._with_retry(lambda pid=pid, keep=keep: client.set_payload(
                q.collection_image, {"document_id": keep}, [pid]))
        if img_drop:
            vector_store._with_retry(lambda: client.delete(q.collection_image, points_selector=img_drop))
        if stale_docs:
            async with db.get_session() as s:
                await s.execute(update(Document).where(Document.id.in_(stale_docs)).values(hash_v2=None))
                await s.commit()
    print(f"[reconcile] {json.dumps(stats, ensure_ascii=False)}")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Đối soát PG ↔ Qdrant (*_v2)")
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(reconcile(ap.parse_args().dry_run))


if __name__ == "__main__":
    main()
