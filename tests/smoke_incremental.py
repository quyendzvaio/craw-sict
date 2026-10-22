"""Smoke incremental index spec v2 trên service thật (PG + Qdrant *_v2 đã nạp bằng loader).

Lấy HTML từ snapshot (không fetch) nên kết quả xác định:
  1. trang đã nạp bằng bulk → index_page = "unchanged" (hash_v2 bulk == incremental)
  2. ép index lại (force) → "changed", số point chữ/ảnh/frame/occurrence/đính kèm KHÔNG đổi
     (cùng id uuid5; ảnh/frame chỉ thuộc bài này được dựng lại)
  3. remove_document → bài + file đính kèm biến mất; index lại → "new", số point như ban đầu
     (file đính kèm tải lại từ site, cần mạng)
  4. trang listing → "listing", đẩy link vào frontier
  Graph (D20): số cạnh entity_links của bài cũng phải giữ nguyên qua 2 và 3.
Chạy trong image worker (cần ML + tesseract), ví dụ:
  python -m tests.smoke_incremental --artifacts _snapshot/2026-10-12/artifacts/v2-multilingual-e5-small
Không chạy trong CI (cần service). Ghi thật vào PG/Qdrant đang trỏ tới — chỉ dùng trên môi trường dev.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qdrant_client.models import FieldCondition, Filter, MatchValue  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from sict_crawler.core.config import get_settings  # noqa: E402
from sict_crawler.model.orm import (  # noqa: E402
    Document,
    EntityLink,
    MediaOccurrence,
    VideoOccurrence,
)
from sict_crawler.repository import db, storage, vector_store  # noqa: E402
from sict_crawler.service.index import indexer


class FakeFrontier:
    def __init__(self):
        self.pushed: list[tuple[str, int]] = []

    def push(self, url: str, depth: int) -> None:
        self.pushed.append((url, depth))


def _flt(pk: int) -> Filter:
    return Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=pk))])


async def state(s, url: str) -> dict | None:
    q = get_settings().qdrant
    c = vector_store.get_client()
    d = await s.scalar(select(Document).where(Document.url == url))
    if d is None:
        return None
    kids = (await s.scalars(select(Document.id).where(Document.parent_url == url))).all()
    return {"text": c.count(q.collection_text, count_filter=_flt(d.id), exact=True).count,
            "media": c.count(q.collection_image, count_filter=_flt(d.id), exact=True).count,
            "occ": await s.scalar(select(func.count()).select_from(MediaOccurrence)
                                  .where(MediaOccurrence.document_id == d.id)),
            "vocc": await s.scalar(select(func.count()).select_from(VideoOccurrence)
                                   .where(VideoOccurrence.document_id == d.id)),
            "entity_links": await s.scalar(select(func.count()).select_from(EntityLink)
                                           .where(EntityLink.document_id == d.id)),
            "attachments": len(kids),
            "attachment_text": sum(c.count(q.collection_text, count_filter=_flt(k), exact=True).count
                                   for k in kids)}


async def main(art: Path) -> bool:
    man = json.loads((art / "manifest.json").read_text())
    snap = Path(man["snapshot"])
    docs = [json.loads(x) for x in (art / "documents.jsonl").read_text().splitlines() if x.strip()]
    kids = {d["parent_url"] for d in docs if d.get("parent_url")}
    pages = [d for d in docs if d["source"] == "page"]
    with_files = next(d for d in pages if d["images"] and d["url"] in kids)
    with_video = next((d for d in pages if d["videos"]), None)

    def html(d: dict) -> str:
        return gzip.decompress((snap / d["html_path"]).read_bytes()).decode("utf-8", "ignore")

    await db.init_db()  # bảng mới (graph D20) như các lối vào thật
    st = storage.Storage()
    ok = True
    async with db.get_session() as s:
        for d in [x for x in (with_files, with_video) if x]:
            before = await state(s, d["url"])
            r1 = await indexer.index_page(s, st, d["url"], html(d))
            r2 = await indexer.index_page(s, st, d["url"], html(d), force=True)
            after = await state(s, d["url"])
            good = r1 == "unchanged" and r2 == "changed" and before == after
            ok &= good
            print(f"[{'OK' if good else 'FAIL'}] reindex {d['url']}\n  {r1}/{r2} {before} → {after}")
        ff = FakeFrontier()
        listing = "https://sict.haui.edu.vn/vn/tin-tuc"
        r = await indexer.index_page(s, st, listing, html(with_files), depth=1, frontier=ff)
        ok &= r == "listing" and bool(ff.pushed)
        print(f"[{'OK' if r == 'listing' else 'FAIL'}] listing → {r}, {len(ff.pushed)} link")
        d = with_files
        before = await state(s, d["url"])
        row = await s.scalar(select(Document).where(Document.url == d["url"]))
        n = await indexer.remove_document(s, row)
        gone = await state(s, d["url"])
        r = await indexer.index_page(s, st, d["url"], html(d))
        after = await state(s, d["url"])
        good = gone is None and r == "new" and before == after
        ok &= good
        print(f"[{'OK' if good else 'FAIL'}] remove {n} tài liệu → re-add {r}\n  {before} → {after}")
    print("SMOKE", "OK" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    raise SystemExit(0 if asyncio.run(main(Path(ap.parse_args().artifacts))) else 1)
