"""Đối soát Postgres ↔ Qdrant (AC-E6, D8) — chạy hằng đêm từ incremental, hoặc tay.

Ghi PG và Qdrant không cùng giao dịch, nên lỗi giữa chừng để lại:
  1. point chữ có document_id không còn trong PG (bài đã xoá / giao dịch PG rollback) → xoá point;
  2. point media có document_id không còn trong PG → gỡ id, point không còn bài nào → xoá;
  3. bài có hash_v2 (đã index xong) nhưng 0 point chữ dù n_chunks > 0 → đặt hash_v2 = NULL để lần
     recheck sau index lại (không tự embed ở đây).
Cầu chì: tỉ lệ point mồ côi > MAX_ORPHAN_RATIO (VD trỏ nhầm DB rỗng) → dừng, không xoá gì.

Dọn bộ khung video cũ (models.Media.frame_sets; chỉ chạy tay, sau khi bỏ hẳn collection ảnh cũ — VD đã xoá
sict_image_v2, không còn rollback về v2): xoá dòng media khung mà KHÔNG tag nào là collection ảnh còn tồn tại.
Mặc định chỉ in kế hoạch; --apply mới xoá. Khung chưa gắn tag chỉ bị xoá khi có --include-untagged; không bao giờ
xoá hết khung của một video (giữ lại để lần index sau còn byte mà dựng). File khung trên store không bị xoá.

Usage: python -m sict_crawler.pipeline.reconcile [--dry-run]
       python -m sict_crawler.pipeline.reconcile --prune-frames [--include-untagged] [--apply]
Callers: incremental.run_incremental (mỗi 24h, khoá Redis reconcile:last), CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import delete, select, update

from sict_crawler.core.config import get_settings
from sict_crawler.model.orm import Document, Media
from sict_crawler.repository import db, vector_store

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


def plan_frame_prune(rows: list[tuple[int, int | None, list]], live: set[str],
                     include_untagged: bool = False) -> list[int]:
    """rows (media id, video_id, frame_sets) → id cần xoá: không tag nào thuộc `live` (collection ảnh còn tồn
    tại); chưa có tag thì chỉ khi include_untagged; video nào sẽ mất hết khung thì giữ nguyên cả video."""
    def dead(sets: list) -> bool:
        return not (set(sets or []) & live) and (bool(sets) or include_untagged)
    by_video: dict[int | None, list[tuple[int, bool]]] = {}
    for mid, vid, sets in rows:
        by_video.setdefault(vid, []).append((mid, dead(sets)))
    return sorted(mid for items in by_video.values() if not all(d for _, d in items)
                  for mid, d in items if d)


async def prune_frames(apply: bool = False, include_untagged: bool = False) -> dict:
    live = {c.name for c in vector_store.get_client().get_collections().collections}
    async with db.get_session() as s:
        rows = (await s.execute(select(Media.id, Media.video_id, Media.frame_sets)
                                .where(Media.kind == "video_frame"))).all()
        ids = plan_frame_prune([tuple(r) for r in rows], live, include_untagged)
        tags: dict[str, int] = {}
        for _, _, sets in rows:
            for t in (sets or ["(chưa gắn)"]):
                tags[t] = tags.get(t, 0) + 1
        stats = {"frames": len(rows), "by_set": tags, "live_collections": sorted(live),
                 "prune": len(ids), "applied": apply}
        if apply and ids:
            for i in range(0, len(ids), 1000):
                await s.execute(delete(Media).where(Media.id.in_(ids[i:i + 1000])))
            await s.commit()
    print(f"[reconcile] prune-frames {json.dumps(stats, ensure_ascii=False)}")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Đối soát PG ↔ Qdrant (*_v2)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune-frames", action="store_true", help="dọn bộ khung video của collection đã bỏ")
    ap.add_argument("--include-untagged", action="store_true", help="--prune-frames: xoá cả khung chưa gắn bộ")
    ap.add_argument("--apply", action="store_true", help="--prune-frames: xoá thật (mặc định chỉ in kế hoạch)")
    a = ap.parse_args()
    if a.prune_frames:
        asyncio.run(prune_frames(a.apply, a.include_untagged))
    else:
        asyncio.run(reconcile(a.dry_run))


if __name__ == "__main__":
    main()
