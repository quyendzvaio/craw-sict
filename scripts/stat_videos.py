"""Thong ke video .mp4 vs YouTube tren toan bo raw HTML da crawl (one-off)."""
import asyncio
from collections import Counter

from sqlalchemy import select

from sict_crawler.model.orm import Document
from sict_crawler.repository import db, storage
from sict_crawler.service.crawl import parser


def classify(u):
    lu = u.lower()
    if "youtube.com" in lu or "youtu.be" in lu:
        return "youtube"
    if ".mp4" in lu.split("?")[0]:
        return "mp4"
    return "other:" + lu.split("/")[2] if "/" in lu else "other"


async def main():
    st = storage.Storage()
    await db.init_db()
    counts = Counter()
    examples = {}
    n_docs = n_with_video = 0
    async with db.get_session() as session:
        ids = (await session.scalars(select(Document.id))).all()
        for doc_id in ids:
            doc = await session.get(Document, doc_id)
            if not doc or not doc.raw_s3_key:
                continue
            n_docs += 1
            try:
                raw = await asyncio.to_thread(st.read_raw_html,
                                              doc.raw_s3_key)
            except Exception:
                continue
            urls = parser.parse_html(doc.url,
                                     raw.decode(errors="replace")).video_urls
            if urls:
                n_with_video += 1
            for u in urls:
                c = classify(u)
                counts[c] += 1
                examples.setdefault(c, u)
    print("docs scanned:", n_docs)
    print("docs with video:", n_with_video)
    for k, v in counts.most_common():
        print("%s: %d  (vd: %s)" % (k, v, examples[k][:120]))


asyncio.run(main())
