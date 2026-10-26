"""Graph D20 — MỘT đường code dựng / cập nhật graph, Postgres là nguồn sự thật duy nhất.

Đầu vào luôn đọc từ PG + Qdrant (không từ đối tượng trong bộ nhớ của loader/indexer):
  tài liệu (documents), chunk (payload Qdrant), ảnh (media_occurrence: alt, đoạn sát ảnh; media: OCR),
  video (video_occurrence, videos, OCR khung hình).
  rebuild            bulk + hằng đêm: trích ứng viên mọi bài → alias/chặn đã duyệt → gộp toàn cục
                     (graph.build_gazetteer) → thực thể, cạnh, cạnh thực thể–thực thể → payload Qdrant.
                     Người gần giống cán bộ trong danh mục → hàng chờ duyệt (không tự gộp).
  update_documents   incremental: ứng viên của bài mới/sửa → graph.resolve_mention (cùng luật gộp) →
                     thực thể mới nếu cần → cạnh của bài → payload các point liên quan.
  sync_payloads      payload `entities` / `entities_strong` SINH LẠI từ entity_links (ghi đè, không cộng
                     dồn) → tên bị gỡ khỏi bài cũng biến khỏi ảnh dùng chung.
Hàng chờ duyệt: review-export → sửa cột status (merged | distinct | blocked) → review-import → các
quyết định thành entity_aliases (áp ở mọi lần dựng sau) → rebuild.

Usage:
  python -m sict_crawler.service.graph.graphsync rebuild
  python -m sict_crawler.service.graph.graphsync sync-payloads
  python -m sict_crawler.service.graph.graphsync review-export review.csv | review-import review.csv
  python -m sict_crawler.service.graph.graphsync block event "hoc may" | alias person "nguyen huong lan" "nguyen thi huong lan"
  python -m sict_crawler.service.graph.graphsync stats
Callers: loader, indexer.index_page, incremental, api (load_gazetteer/load_relations), CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sict_crawler.core.config import get_settings
from sict_crawler.model.orm import (
    Document,
    EntityAlias,
    EntityLink,
    EntityRelation,
    EntityReview,
    GraphEntity,
    Media,
    MediaOccurrence,
    Video,
    VideoOccurrence,
)
from sict_crawler.repository import vector_store
from sict_crawler.service.graph import graph as G
from sict_crawler.service.index import doc_rules as rules

NEAR_DUP_MIN_DOCS = 3      # chỉ so tên gần giống với người đã gặp ở ≥ N bài hoặc cán bộ (giới hạn chi phí)


@dataclass
class DocInput:
    pk: int
    doc: dict
    chunks: list[tuple[str, str]] = field(default_factory=list)       # (chunk_id, text)
    media: list[G.MediaRef] = field(default_factory=list)             # ảnh (key = sha256) + video
    media_ids: dict[str, int] = field(default_factory=dict)           # sha256 → media.id
    video_ids: dict[str, int] = field(default_factory=dict)           # video_key → videos.id

    def own_alts(self) -> list[str]:
        t = G.fold(self.doc.get("title", ""))
        return [m.alt for m in self.media if m.kind == "image" and m.alt and G.fold(m.alt) != t]

    def contexts(self) -> list[str]:
        return [m.context for m in self.media if m.context]

    def mentions(self) -> list[G.Mention]:
        ms = G.doc_mentions(self.doc, "\n".join(t for _, t in self.chunks), self.own_alts(),
                            self.contexts(), [m.ocr for m in self.media if m.ocr])
        staff = G.staff_from_profile(self.doc)
        if staff is not None:
            ms = [m for m in ms if (m.kind, m.key) != (staff.kind, staff.key)] + [staff]
        return ms

    def link(self, gaz: G.Gazetteer, eids: dict[tuple[str, str], int]) -> list[dict]:
        """Cạnh của tài liệu, toàn bộ theo luật (khớp danh bạ)."""
        g = G.link_document(self.doc, self.chunks, self.media, gaz)
        rows = G.link_rows(g, self.pk, self.media_ids, self.video_ids, eids)
        for r in rows:
            r.setdefault("roles", [])
        return rows


# ------------------------------------------------------------------ đọc đầu vào từ PG + Qdrant
def _scroll_chunks(doc_ids: list[int] | None) -> dict[int, list[tuple[int, str, str]]]:
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
    q = get_settings().qdrant
    client = vector_store.get_client()
    must = [FieldCondition(key="document_id", match=MatchAny(any=doc_ids))] if doc_ids else []
    flt = Filter(must=must or None,
                 must_not=[FieldCondition(key="kind", match=MatchValue(value="media_caption"))])
    out: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
    offset = None
    while True:
        pts, offset = client.scroll(q.collection_text, scroll_filter=flt, limit=1000, offset=offset,
                                    with_payload=["document_id", "chunk_id", "idx", "content"],
                                    with_vectors=False)
        for p in pts:
            pl = p.payload
            out[pl["document_id"]].append((pl.get("idx", 0), pl.get("chunk_id") or str(p.id),
                                           pl.get("content", "")))
        if offset is None:
            break
    return out


async def load_inputs(session, doc_ids: list[int] | None = None) -> list[DocInput]:
    stmt = select(Document.id, Document.url, Document.title, Document.section, Document.doc_type).where(
        Document.hash_v2.is_not(None))
    if doc_ids is not None:
        stmt = stmt.where(Document.id.in_(doc_ids))
    rows = (await session.execute(stmt)).all()
    ins = {pk: DocInput(pk, {"url": url, "title": title or "", "section": sec, "doc_type": dt_})
           for pk, url, title, sec, dt_ in rows}
    if not ins:
        return []
    ids = list(ins)
    for pk, chunks in _scroll_chunks(ids if doc_ids is not None else None).items():
        if pk in ins:
            ins[pk].chunks = [(cid, text) for _, cid, text in sorted(chunks)]
    occ = (await session.execute(
        select(MediaOccurrence.document_id, MediaOccurrence.alt, MediaOccurrence.context,
               Media.id, Media.sha256, Media.ocr_text)
        .join(Media, Media.id == MediaOccurrence.media_id)
        .where(MediaOccurrence.document_id.in_(ids)).order_by(MediaOccurrence.pos))).all()
    for pk, alt, ctx, mid, sha, ocr in occ:
        ins[pk].media.append(G.MediaRef(sha, "image", alt or "", ctx or "", ocr or ""))
        ins[pk].media_ids[sha] = mid
    vocc = (await session.execute(
        select(VideoOccurrence.document_id, Video.id, Video.video_key, Video.title)
        .join(Video, Video.id == VideoOccurrence.video_id)
        .where(VideoOccurrence.document_id.in_(ids)))).all()
    vids = {vid for _, vid, _, _ in vocc}
    frame_ocr: dict[int, list[str]] = defaultdict(list)
    if vids:
        for vid, text in (await session.execute(
                select(Media.video_id, Media.ocr_text).where(Media.video_id.in_(vids))
                .order_by(Media.video_id, Media.timestamp))).all():
            frame_ocr[vid].append(text or "")
    for pk, vid, key, title in vocc:
        ins[pk].media.append(G.MediaRef(key, "video", title or "", "", rules.frames_ocr_join(frame_ocr[vid])))
        ins[pk].video_ids[key] = vid
    return list(ins.values())


async def load_alias_map(session) -> dict[tuple[str, str], str | None]:
    return {(k, a): e for k, a, e in (await session.execute(
        select(EntityAlias.kind, EntityAlias.alias_key, EntityAlias.entity_key))).all()}


async def load_gazetteer(session) -> G.Gazetteer:
    rows = (await session.execute(select(GraphEntity.id, GraphEntity.kind, GraphEntity.key,
                                         GraphEntity.name, GraphEntity.aliases,
                                         GraphEntity.doc_count, GraphEntity.attrs))).all()
    return G.Gazetteer([G.Entity(kind=k, key=key, name=n, aliases=list(a or []), doc_count=c,
                                 id=i, attrs=dict(at or {}))
                        for i, k, key, n, a, c, at in rows])


async def load_relations(session) -> list[tuple]:
    return [tuple(r) for r in (await session.execute(select(
        EntityRelation.src_id, EntityRelation.dst_id, EntityRelation.weight,
        EntityRelation.doc_count, EntityRelation.media_count, EntityRelation.roles))).all()]


# ------------------------------------------------------------------ ghi PG
async def upsert_entities(session, ents) -> dict[tuple[str, str], int]:
    rows = [{"kind": e.kind, "key": e.key[:300], "name": e.name[:300], "aliases": list(e.aliases),
             "doc_count": e.doc_count, "attrs": dict(e.attrs)} for e in ents]
    out: dict[tuple[str, str], int] = {}
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(GraphEntity).values(rows[i:i + 1000])
        stmt = stmt.on_conflict_do_update(
            index_elements=["kind", "key"],
            set_={c: stmt.excluded[c] for c in ("name", "aliases", "doc_count", "attrs")})
        res = await session.execute(stmt.returning(GraphEntity.id, GraphEntity.kind, GraphEntity.key))
        out.update({(k, key): i_ for i_, k, key in res.all()})
    return out


async def write_links(session, document_ids: list[int] | None, rows: list[dict]) -> None:
    """Thay toàn bộ cạnh của các tài liệu này (None = mọi tài liệu)."""
    if document_ids is None:
        await session.execute(delete(EntityLink))
    else:
        for i in range(0, len(document_ids), 1000):
            await session.execute(delete(EntityLink).where(
                EntityLink.document_id.in_(document_ids[i:i + 1000])))
    for i in range(0, len(rows), 2000):
        await session.execute(pg_insert(EntityLink).values(rows[i:i + 2000]))


async def rebuild_relations(session) -> int:
    """Tính lại TOÀN BỘ cạnh thực thể ↔ thực thể từ entity_links (vài giây). Caller commit."""
    links = [dict(r._mapping) for r in (await session.execute(select(
        EntityLink.entity_id, EntityLink.document_id, EntityLink.media_id, EntityLink.video_id,
        EntityLink.rel, EntityLink.weight, EntityLink.roles))).all()]
    ents = (await session.execute(select(GraphEntity.id, GraphEntity.kind, GraphEntity.doc_count,
                                         GraphEntity.attrs))).all()
    kinds = {i: k for i, k, _, _ in ents}
    # sự kiện / CLB 1 bài chưa vào danh mục chuẩn: phần lớn là cụm trích nhầm → không tạo quan hệ
    exclude = {i for i, k, n, a in ents if k != "person" and n < 2 and not (a or {}).get("catalog")}
    rows = G.relations_from_links(links, kinds, exclude)
    await session.execute(delete(EntityRelation))
    for i in range(0, len(rows), 2000):
        await session.execute(pg_insert(EntityRelation).values(rows[i:i + 2000]))
    return len(rows)


async def _queue_near_duplicates(session, ents: list[G.Entity], pool,
                                 doc_of: dict[tuple[str, str], int] | None = None) -> int:
    """Người mới/ít gặp có tên gần giống cán bộ hoặc người hay gặp → hàng chờ duyệt (không gộp)."""
    decided = set((await load_alias_map(session)).keys())
    rows = []
    for e in ents:
        if e.kind != "person" or ("person", e.key) in decided or e.attrs.get("staff"):
            continue
        for c in G.near_duplicates(e, pool):
            rows.append({"kind": "person", "key": e.key, "surface": e.name[:300],
                         "reason": "near_duplicate", "candidate_key": c.key,
                         "document_id": (doc_of or {}).get(("person", e.key)),
                         "context": f"{e.name} ({e.doc_count} bài) ~ {c.name} ({c.doc_count} bài"
                                    f"{', cán bộ' if c.attrs.get('staff') else ''})",
                         "status": "pending"})
    for i in range(0, len(rows), 1000):
        await session.execute(pg_insert(EntityReview).values(rows[i:i + 1000]).on_conflict_do_nothing())
    return len(rows)


def _pool(gaz: G.Gazetteer) -> dict[str, list[G.Entity]]:
    return G.near_dup_index([e for e in gaz.entities.values() if e.kind == "person"
                             and (e.attrs.get("staff") or e.doc_count >= NEAR_DUP_MIN_DOCS)])


# ------------------------------------------------------------------ dựng / cập nhật
async def rebuild(session, sync: bool = True) -> dict:
    """Dựng lại toàn bộ graph từ PG + Qdrant (bulk sau loader, hằng đêm từ incremental)."""
    import time
    t0 = time.time()
    tm: dict[str, float] = {}

    def lap(name: str) -> None:
        nonlocal t0
        tm[name] = round(time.time() - t0, 1)
        print(f"[graphsync] {name}: {tm[name]}s", flush=True)
        t0 = time.time()

    inputs = await load_inputs(session)
    lap("load")
    amap = await load_alias_map(session)
    ms_by_doc = [G.apply_aliases(di.mentions(), amap) for di in inputs]
    gaz = G.build_gazetteer(ms_by_doc)
    lap("gazetteer")
    eids = await upsert_entities(session, list(gaz.entities.values()))
    keep = set(eids.values())
    stale = [i for (i,) in (await session.execute(select(GraphEntity.id))).all() if i not in keep]
    for i in range(0, len(stale), 1000):
        await session.execute(delete(GraphEntity).where(GraphEntity.id.in_(stale[i:i + 1000])))
    for (k, key), i in eids.items():
        gaz.entities[(k, key)].id = i
    rows = []
    for di in inputs:
        rows += di.link(gaz, eids)
    # mục danh mục chuẩn khớp chủ yếu trong thân bài (luật chỉ trích tiêu đề) → doc_count = số bài có cạnh
    cat_ids = {i: gaz.entities[k] for k, i in eids.items() if gaz.entities[k].attrs.get("catalog")}
    if cat_ids:
        n_docs: dict[int, set] = defaultdict(set)
        for r in rows:
            if r["entity_id"] in cat_ids:
                n_docs[r["entity_id"]].add(r["document_id"])
        for i, e in cat_ids.items():
            e.doc_count = max(e.doc_count, len(n_docs.get(i, ())))
        await upsert_entities(session, list(cat_ids.values()))
    lap("link")
    await write_links(session, None, rows)
    n_rel = await rebuild_relations(session)
    lap("write")
    # hàng chờ tên gần giống tính lại từ đầu mỗi lần dựng; mục đã duyệt (merged/distinct/blocked) giữ
    await session.execute(delete(EntityReview).where(EntityReview.reason == "near_duplicate",
                                                     EntityReview.status == "pending"))
    n_rev = await _queue_near_duplicates(session, list(gaz.entities.values()), _pool(gaz))
    lap("review")
    await session.commit()
    stats = {"documents": len(inputs), "entities": len(gaz), "removed_entities": len(stale),
             "entity_links": len(rows), "entity_relations": n_rel, "review_queued": n_rev,
             "staff": sum(bool(e.attrs.get("staff")) for e in gaz.entities.values())}
    if sync:
        stats.update(await sync_payloads(session, None))
        lap("payload")
    stats["seconds"] = tm
    return stats


async def update_documents(session, doc_ids: list[int]) -> dict:
    """Incremental: graph của các bài mới/sửa theo cùng luật bulk, rồi payload các point liên quan.
    Gộp toàn cục (biến thể chỉ gặp ở nhiều bài sau này) để rebuild hằng đêm làm."""
    inputs = await load_inputs(session, doc_ids)
    if not inputs:
        return {"documents": 0}
    amap = await load_alias_map(session)
    gaz = await load_gazetteer(session)
    known = dict(gaz.entities)
    changed: dict[tuple[str, str], G.Entity] = {}
    new: list[G.Entity] = []
    for di in inputs:
        for m in G.apply_aliases(di.mentions(), amap):
            e = G.resolve_mention(m, gaz)
            if e is None:
                e = G.Entity(kind=m.kind, key=m.key, name=m.name, aliases=list(m.aliases), doc_count=1,
                             attrs=dict(m.attrs))
                new.append(e)
                known[(e.kind, e.key)] = e
                gaz = G.Gazetteer(list(known.values()))
            elif m.key != e.key and m.key not in e.aliases:
                e.aliases = sorted({*e.aliases, m.key})        # biến thể → alias của thực thể có sẵn
                changed[(e.kind, e.key)] = e
            if m.attrs and not e.attrs.get("staff"):
                e.attrs.update(m.attrs)
                changed[(e.kind, e.key)] = e
    if new or changed:
        ids = await upsert_entities(session, [*new, *changed.values()])
        for (k, key), i in ids.items():
            known[(k, key)].id = i
        gaz = G.Gazetteer(list(known.values()))
    eids = {(e.kind, e.key): e.id for e in gaz.entities.values() if e.id is not None}
    rows = []
    for di in inputs:
        rows += di.link(gaz, eids)
    await write_links(session, [di.pk for di in inputs], rows)
    n_rev = await _queue_near_duplicates(session, new, _pool(gaz))
    await session.commit()
    stats = {"documents": len(inputs), "new_entities": len(new), "entity_links": len(rows),
             "review_queued": n_rev}
    stats.update(await sync_payloads(session, [di.pk for di in inputs]))
    return stats


# ------------------------------------------------------------------ payload Qdrant từ PG
async def sync_payloads(session, doc_ids: list[int] | None = None) -> dict:
    """Ghi đè payload `entities` (chunk, caption, ảnh/frame) + `entities_strong` (ảnh/frame) theo
    entity_links hiện tại. doc_ids None = toàn bộ; có doc_ids = point của các bài đó + ảnh/video
    chúng chứa (tính trên MỌI bài chứa ảnh/video đó)."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny
    q = get_settings().qdrant
    client = vector_store.get_client()
    ek = {i: f"{k}:{key}" for i, k, key in (await session.execute(
        select(GraphEntity.id, GraphEntity.kind, GraphEntity.key))).all()}

    def links_stmt():
        return select(EntityLink.document_id, EntityLink.entity_id, EntityLink.rel, EntityLink.chunk_id,
                      EntityLink.media_id, EntityLink.video_id, EntityLink.weight)

    stmt = links_stmt()
    if doc_ids is not None:
        stmt = stmt.where(EntityLink.document_id.in_(doc_ids))
    about: dict[int, list[str]] = defaultdict(list)
    chunk_m: dict[str, list[str]] = defaultdict(list)
    cap: dict[tuple[int, str, int], list[str]] = defaultdict(list)    # (doc, "m"|"v", id) → keys
    for d, e, rel, cid, mid, vid, w in (await session.execute(stmt)).all():
        k = ek.get(e)
        if k is None:
            continue
        if rel == "about":
            about[d].append(k)
        elif rel == "mentions" and cid:
            chunk_m[cid].append(k)
        elif rel == "depicts":
            cap[(d, "m", mid) if mid else (d, "v", vid)].append(k)

    # media / video bị ảnh hưởng (kể cả ảnh của bài nay không còn cạnh → xoá tên cũ)
    if doc_ids is None:
        media_ids = [i for (i,) in (await session.execute(select(Media.id).where(Media.video_id.is_(None)))).all()]
        video_ids = [i for (i,) in (await session.execute(select(Video.id))).all()]
    else:
        media_ids = [i for (i,) in (await session.execute(select(MediaOccurrence.media_id).where(
            MediaOccurrence.document_id.in_(doc_ids)))).all()]
        video_ids = [i for (i,) in (await session.execute(select(VideoOccurrence.video_id).where(
            VideoOccurrence.document_id.in_(doc_ids)))).all()]
    m_all: dict[int, list[str]] = defaultdict(list)
    m_strong: dict[int, list[str]] = defaultdict(list)
    v_all: dict[int, list[str]] = defaultdict(list)
    v_strong: dict[int, list[str]] = defaultdict(list)
    for chunk in [media_ids[i:i + 5000] for i in range(0, len(media_ids), 5000)]:
        for e, mid, w in (await session.execute(select(EntityLink.entity_id, EntityLink.media_id,
                                                       EntityLink.weight).where(
                EntityLink.rel == "depicts", EntityLink.media_id.in_(chunk)))).all():
            m_all[mid].append(ek[e])
            if w >= G.STRONG_MIN:
                m_strong[mid].append(ek[e])
    if video_ids:
        for e, vid, w in (await session.execute(select(EntityLink.entity_id, EntityLink.video_id,
                                                       EntityLink.weight).where(
                EntityLink.rel == "depicts", EntityLink.video_id.in_(video_ids)))).all():
            v_all[vid].append(ek[e])
            if w >= G.STRONG_MIN:
                v_strong[vid].append(ek[e])

    # payload mong muốn (từ PG) vs hiện tại (Qdrant) → chỉ ghi point khác, gom point cùng payload vào
    # 1 lệnh set_payload (1 lệnh / point tốn ~70 ms; 1 lệnh / 500 point ~0,2 s)
    tcols = vector_store.text_collections()   # chunk (text) + caption (ocr, khi tách)
    want: dict[str, dict] = {c: {} for c in (*tcols, q.collection_image)}
    have: dict[str, dict] = {c: {} for c in (*tcols, q.collection_image)}
    sha_id = {sha: i for i, sha in (await session.execute(select(Media.id, Media.sha256))).all()}
    vkey_id = {k: i for i, k in (await session.execute(select(Video.id, Video.video_key))).all()}
    flt = Filter(must=[FieldCondition(key="document_id", match=MatchAny(any=doc_ids))]) if doc_ids else None
    for tc in tcols:
        offset = None
        while True:
            pts, offset = client.scroll(tc, scroll_filter=flt, limit=1000, offset=offset,
                                        with_payload=["document_id", "kind", "chunk_id", "media_kind",
                                                      "media_key", "video_key", "entities"], with_vectors=False)
            for p in pts:
                pl = p.payload
                d = pl.get("document_id")
                if pl.get("kind") == "media_caption":
                    if pl.get("media_kind") == "video":
                        keys = cap.get((d, "v", vkey_id.get(pl.get("video_key"))), [])
                    else:
                        keys = cap.get((d, "m", sha_id.get(pl.get("media_key"))), [])
                else:
                    keys = [*about.get(d, []), *chunk_m.get(pl.get("chunk_id") or str(p.id), [])]
                want[tc][p.id] = (("entities", tuple(dict.fromkeys(keys))),)
                have[tc][p.id] = (("entities", tuple(pl.get("entities") or ())),)
            if offset is None:
                break

    def media_payload(a: list[str], st: list[str]) -> tuple:
        return (("entities", tuple(dict.fromkeys(a))), ("entities_strong", tuple(dict.fromkeys(st))))

    pid_of = dict((await session.execute(select(Media.id, Media.point_id).where(
        Media.id.in_(media_ids)))).all()) if media_ids else {}
    for mid, pid in pid_of.items():
        if pid:
            want[q.collection_image][pid] = media_payload(m_all.get(mid, []), m_strong.get(mid, []))
    if video_ids:
        for vid, pid in (await session.execute(select(Media.video_id, Media.point_id).where(
                Media.video_id.in_(video_ids)))).all():
            if pid:
                want[q.collection_image][pid] = media_payload(v_all.get(vid, []), v_strong.get(vid, []))
    ids = list(want[q.collection_image])
    for i in range(0, len(ids), 1000):
        for p in client.retrieve(q.collection_image, ids[i:i + 1000],
                                 with_payload=["entities", "entities_strong"]):
            have[q.collection_image][str(p.id)] = media_payload(p.payload.get("entities") or [],
                                                               p.payload.get("entities_strong") or [])
    stats = {}
    for coll in want:
        groups: dict[tuple, list] = defaultdict(list)
        for pid, pl in want[coll].items():
            cur = have[coll].get(str(pid), have[coll].get(pid))
            # point không còn trong Qdrant (VD video/ảnh mà bài cuối cùng chứa nó vừa bỏ → detach đã xoá point,
            # dòng media vẫn giữ để dùng lại) → không có gì để đồng bộ; set_payload sẽ lỗi NOT_FOUND
            if cur is None and coll == q.collection_image:
                continue
            if cur != pl:
                groups[pl].append(pid)
        n_pts = 0
        for pl, pids in groups.items():
            payload = {k: list(v) for k, v in pl}
            for i in range(0, len(pids), 1000):
                part = pids[i:i + 1000]
                vector_store._with_retry(lambda part=part, payload=payload, coll=coll: client.set_payload(
                    coll, payload, points=part, wait=True))
                n_pts += len(part)
        stats[coll] = {"points": len(want[coll]), "changed": n_pts, "writes": len(groups)}
    out = {"payload_text": stats[q.collection_text], "payload_media": stats[q.collection_image]}
    if vector_store.ocr_collection() != q.collection_text:
        out["payload_ocr"] = stats[vector_store.ocr_collection()]
    return out


# ------------------------------------------------------------------ hàng chờ duyệt
REVIEW_FIELDS = ["id", "kind", "key", "surface", "reason", "candidate_key", "candidate_name", "context",
                 "suggestion", "why", "evidence_key", "evidence_candidate", "status", "target_key", "note"]


def suggest_pair(a: G.Entity, b: G.Entity) -> tuple[str, str]:
    """Gợi ý THẬN TRỌNG cho cặp người gần giống (người duyệt quyết định, gợi ý không tự áp dụng)."""
    ta, tb = a.fkey.split(), b.fkey.split()
    rare = min(a.doc_count, b.doc_count)
    if a.fkey == b.fkey:
        if rare <= 2:
            return "merged?", "chỉ khác dấu, 1 bên hiếm gặp — nhiều khả năng gõ sai dấu (xem đoạn trích)"
        return "uncertain", "chỉ khác dấu nhưng cả 2 đều gặp nhiều — có thể 2 người (Hùng/Hưng)"
    if ta[0] != tb[0] or ta[-1] != tb[-1]:
        return "distinct", "khác họ hoặc tên riêng"
    extra = set(ta) ^ set(tb)
    if not (set(ta) <= set(tb) or set(tb) <= set(ta)):
        return "distinct", "tên đệm khác nhau"
    if extra <= {"thi", "van"}:
        return "merged?", "chỉ thiếu/thừa \"Thị\"/\"Văn\" — hay bị lược, nhưng có thể khác giới: xem đoạn trích"
    return "uncertain", f"thừa/thiếu tên đệm {sorted(extra)} — thường là người khác trùng tên"


async def _evidence(session, kind: str, key: str, limit: int = 2) -> str:
    rows = (await session.execute(
        select(Document.title, EntityLink.evidence).join(GraphEntity, GraphEntity.id == EntityLink.entity_id)
        .join(Document, Document.id == EntityLink.document_id)
        .where(GraphEntity.kind == kind, GraphEntity.key == key).limit(limit))).all()
    return " || ".join(f"[{t[:60]}] {e[:140]}" for t, e in rows)


async def review_export(session, path: str, status: str = "pending", staff_only: bool = False) -> int:
    gaz = await load_gazetteer(session)
    rows = (await session.execute(select(EntityReview).where(EntityReview.status == status)
                                  .order_by(EntityReview.reason, EntityReview.key))).scalars().all()
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        w.writeheader()
        for r in rows:
            a = gaz.entities.get((r.kind, r.key))
            b = gaz.entities.get((r.kind, r.candidate_key)) if r.candidate_key else None
            if staff_only and not ((a and a.attrs.get("staff")) or (b and b.attrs.get("staff"))):
                continue
            sug, why = suggest_pair(a, b) if (a and b and r.kind == "person") else ("", "")
            w.writerow({"id": r.id, "kind": r.kind, "key": r.key, "surface": r.surface, "reason": r.reason,
                        "candidate_key": r.candidate_key, "candidate_name": b.name if b else "",
                        "context": r.context, "suggestion": sug, "why": why,
                        "evidence_key": await _evidence(session, r.kind, r.key),
                        "evidence_candidate": await _evidence(session, r.kind, r.candidate_key) if b else "",
                        "status": r.status, "target_key": r.candidate_key, "note": ""})
            n += 1
    return n


async def set_alias(session, kind: str, alias_key: str, entity_key: str | None, source: str = "manual",
                    note: str = "") -> None:
    norm = G.person_key if kind == "person" else (lambda x: G.fold(x).strip())
    # khoá trong hàng chờ đã chuẩn hoá → giữ nguyên; tên gõ tay (có dấu) → chuẩn hoá
    ak = alias_key if kind == "person" and any(ch.isdigit() for ch in alias_key) else norm(alias_key)
    tk = None
    if entity_key:
        tk = entity_key if kind == "person" and any(ch.isdigit() for ch in entity_key) else norm(entity_key)
    stmt = pg_insert(EntityAlias).values(kind=kind, alias_key=ak, entity_key=tk, source=source, note=note)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["kind", "alias_key"],
        set_={"entity_key": stmt.excluded.entity_key, "source": stmt.excluded.source,
              "note": stmt.excluded.note}))


async def review_import(session, path: str) -> dict:
    """status: merged (key là alias của target_key) | distinct (2 thực thể khác nhau) |
    blocked (key không phải thực thể) | pending (bỏ qua).

    Dòng tìm theo id, id không có / không khớp thì theo (kind, key, reason, candidate_key): file đã duyệt
    trên DB dev áp được lên DB khác (production dựng hàng chờ với id khác)."""
    stats = defaultdict(int)
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            st = (row.get("status") or "").strip()
            if st not in ("merged", "distinct", "blocked"):
                continue
            rev = await session.get(EntityReview, int(row["id"])) if (row.get("id") or "").strip() else None
            if rev is None or rev.key != row["key"] or rev.candidate_key != row["candidate_key"]:
                rev = await session.scalar(select(EntityReview).where(
                    EntityReview.kind == row["kind"], EntityReview.key == row["key"],
                    EntityReview.reason == row["reason"], EntityReview.candidate_key == row["candidate_key"]))
            if rev is None:
                stats["missing"] += 1
                continue
            if st == "merged":
                await set_alias(session, rev.kind, rev.key, row.get("target_key") or rev.candidate_key,
                                "review", row.get("note", ""))
            elif st == "blocked":
                await set_alias(session, rev.kind, rev.key, None, "review", row.get("note", ""))
            rev.status = st
            stats[st] += 1
    await session.commit()
    return dict(stats)


async def graph_stats(session) -> dict:
    kinds = dict((await session.execute(select(GraphEntity.kind, func.count()).group_by(GraphEntity.kind))).all())
    return {"entities": kinds,
            "staff": await session.scalar(select(func.count()).select_from(GraphEntity).where(
                GraphEntity.attrs["staff"].as_boolean().is_(True))),
            "links": await session.scalar(select(func.count()).select_from(EntityLink)),
            "relations": await session.scalar(select(func.count()).select_from(EntityRelation)),
            "aliases": await session.scalar(select(func.count()).select_from(EntityAlias)),
            "review": dict((await session.execute(select(EntityReview.status, func.count())
                                                  .group_by(EntityReview.status))).all())}


def main() -> None:
    from sict_crawler.repository import db
    ap = argparse.ArgumentParser(description="Graph D20: dựng / đồng bộ / duyệt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild")
    sub.add_parser("sync-payloads")
    sub.add_parser("stats")
    e = sub.add_parser("review-export")
    e.add_argument("path")
    e.add_argument("--staff-only", action="store_true", help="chỉ cặp có 1 bên là cán bộ trong danh mục")
    i = sub.add_parser("review-import")
    i.add_argument("path")
    b = sub.add_parser("block")
    b.add_argument("kind", choices=G.KINDS)
    b.add_argument("key")
    a = sub.add_parser("alias")
    a.add_argument("kind", choices=G.KINDS)
    a.add_argument("alias_key")
    a.add_argument("entity_key")
    args = ap.parse_args()

    async def run():
        vector_store.require_v2()
        await db.init_db()
        async with db.get_session() as s:
            if args.cmd == "rebuild":
                return await rebuild(s)
            if args.cmd == "sync-payloads":
                return await sync_payloads(s, None)
            if args.cmd == "stats":
                return await graph_stats(s)
            if args.cmd == "review-export":
                return {"exported": await review_export(s, args.path, staff_only=args.staff_only)}
            if args.cmd == "review-import":
                return await review_import(s, args.path)
            if args.cmd == "block":
                await set_alias(s, args.kind, args.key, None)
            else:
                await set_alias(s, args.kind, args.alias_key, args.entity_key)
            await s.commit()
            return {"ok": True, "hint": "chạy `graphsync rebuild` để áp dụng"}

    print(json.dumps(asyncio.run(run()), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
