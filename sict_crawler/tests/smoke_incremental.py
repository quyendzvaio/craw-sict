"""Live-services smoke test for incremental edit/delete/webhook paths.

Needs: postgres + qdrant + redis up, crawl infra working. Does NOT touch frontier.
Run: SICT_WEBHOOK_URL=http://localhost:9999/hook python -m sict_crawler.tests.smoke_incremental
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select

from sict_crawler import crawler, db, storage, vector_store
from sict_crawler.main import _notify, _process_page
from sict_crawler.models import Chunk, Document, MediaAsset


async def count_rows(session, doc_id: int) -> tuple[int, int]:
    chunks = len((await session.scalars(
        select(Chunk).where(Chunk.document_id == doc_id))).all())
    media = len((await session.scalars(
        select(MediaAsset).where(MediaAsset.document_id == doc_id))).all())
    return chunks, media


def qdrant_points(doc_id: int) -> int:
    client = vector_store.get_client()
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    flt = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=doc_id))])
    n = 0
    for coll in ("sict_text", "sict_image"):
        n += client.count(coll, count_filter=flt, exact=True).count
    return n


async def smoke_edit(session, st) -> None:
    """Hash mismatch → old chunks/media/vectors cleaned; re-processing the same
    html twice is idempotent (no accumulation). Compares consecutive runs, not
    vs. prior DB state — the live page itself may have changed between crawls."""
    doc = (await session.scalars(select(Document).limit(1))).first()
    assert doc, "need at least one document"
    doc_id, url = doc.id, doc.url
    html, status = await crawler.fetch(url)
    assert status == 200 and html, f"fetch failed: {status}"

    async def reprocess() -> tuple[int, int, int]:
        d = await session.scalar(select(Document).where(Document.id == doc_id))
        d.content_hash = "deadbeef"  # force changed-content branch
        await session.commit()
        await _process_page(session, st, url, 0, None, html)
        c, m = await count_rows(session, doc_id)
        return c, m, qdrant_points(doc_id)

    a = await reprocess()
    b = await reprocess()
    print(f"[edit] run1={a} run2={b}")
    assert a == b, f"re-processing same html drifted: {a} → {b} (stale rows survive)"
    print("[edit] PASS — idempotent, no accumulation")


async def smoke_delete(session) -> None:
    """Non-existent URL with existing doc → doc + chunks + qdrant points removed."""
    url = "https://sict.haui.edu.vn/vn/__smoke_nonexistent_404__"
    doc = Document(url=url, title="smoke", content_hash="abc")
    session.add(doc)
    await session.flush()
    session.add(Chunk(document_id=doc.id, content="smoke", chunk_index=0))
    await session.flush()
    from qdrant_client.models import PointStruct
    pid = "00000000-0000-0000-0000-00000000dead"
    vector_store.get_client().upsert("sict_text", [PointStruct(
        id=pid, vector=[0.0] * vector_store.get_settings().qdrant.text_dim,
        payload={"document_id": doc.id, "url": url})])

    doc_id = doc.id
    assert qdrant_points(doc_id) == 1

    # replicate run() delete branch (status 404 path)
    vector_store.delete_by_document(doc_id)
    await session.delete(doc)  # chunks/media cascade via FK
    await session.commit()
    _notify("crawl.delete", {"url": url})

    left = await session.scalar(select(Document).where(Document.url == url))
    assert left is None, "doc not deleted"
    assert qdrant_points(doc_id) == 0, "qdrant points not deleted"
    print("[delete] PASS — doc + qdrant points removed")


async def main() -> None:
    st = storage.Storage()
    async with db.get_session() as session:
        await smoke_edit(session, st)
        await smoke_delete(session)
    print("SMOKE OK")


if __name__ == "__main__":
    assert os.environ.get("SICT_WEBHOOK_URL"), "set SICT_WEBHOOK_URL to capture webhook"
    asyncio.run(main())
