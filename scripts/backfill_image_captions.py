"""Backfill caption PG -> payload sict_image cho point cũ (upsert trước khi có caption).

Chạy trong container có config + deps (VD api container trên VM1):
  docker exec craw-sict-api-1 python /app/scripts/backfill_image_captions.py [--limit N]

Không embed lại — chỉ set_payload.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")


def main(limit: int = 0) -> None:
    from sict_crawler import vector_store

    cfg = vector_store.get_settings().qdrant
    client = vector_store.get_client()
    caps = asyncio.run(_pg_captions_all())
    print(f"pg captions loaded: {len(caps)}")

    fixed, skipped, total = 0, 0, 0
    offset = None
    while True:
        pts, offset = client.scroll(cfg.collection_image, limit=500,
                                    offset=offset, with_payload=True,
                                    with_vectors=False)
        if not pts:
            break
        updates: dict[str, str] = {}
        for p in pts:
            pl = p.payload or {}
            mid = pl.get("media_id")
            if not mid or (limit and total >= limit):
                continue
            total += 1
            cap = caps.get(mid, "")
            if cap and pl.get("caption") != cap:
                updates[str(p.id)] = cap
            else:
                skipped += 1
        for pid, cap in updates.items():
            vector_store._with_retry(
                lambda pid=pid, cap=cap: client.set_payload(
                    cfg.collection_image, {"caption": cap}, [pid]))
            fixed += 1
        print(f"  ...scanned {total}, fixed {fixed}", flush=True)
        if offset is None or (limit and total >= limit):
            break
    print(f"done: scanned={total} fixed={fixed} already_ok={skipped}")


async def _pg_captions_all() -> dict[int, str]:
    from sqlalchemy import select

    from sict_crawler import db
    from sict_crawler.models import MediaAsset

    out: dict[int, str] = {}
    async with db.get_session() as session:
        res = await session.execute(
            select(MediaAsset.id, MediaAsset.caption).where(
                MediaAsset.caption != ""))
        for mid, cap in res.all():
            out[mid] = cap
    return out


if __name__ == "__main__":
    lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--limit" else 0
    main(lim)
