"""Ghi index spec v2 vào Postgres + Qdrant `*_v2` — 1 đường ghi cho cả bulk lẫn incremental.

  - loader.py (bulk): artifact của compute.py → các hàm `upsert_*` + `*_payload` ở đây.
  - index_page (incremental, main._process_page): 1 trang HTML → cùng quy tắc compute (page_doc,
    doc_chunks, chunk_row, caption text) + cùng id uuid5 → point trùng id ghi đè, không nhân bản.

Postgres chỉ giữ metadata/quan hệ (documents, media, media_occurrence, videos, video_occurrence);
nội dung chunk nằm trong payload Qdrant. Ảnh/frame: 1 point / 1 file (sha256), `document_id` là
danh sách bài chứa nó → xoá 1 bài chỉ gỡ id bài khỏi point (AC-B5).

Callers: loader.py, main._process_page, incremental.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, literal_column, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import compute as C
from . import indexing as ix
from . import vector_store
from .config import get_settings
from .models import Document, Media, MediaOccurrence, Video, VideoOccurrence

MAX_IMAGES_PER_PAGE = 30
MAX_VIDEOS_PER_PAGE = 5
MAX_FILE_BYTES = 40 * 1024 * 1024
DECORATIVE_DOCS = 20      # ảnh xuất hiện ở > N bài = trang trí (cùng ngưỡng compute --boilerplate-docs)
_IMG_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
            "image/webp": "webp", "image/bmp": "bmp"}
MIN_IMAGE_BYTES = 1000


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uniq(xs) -> list:
    return list(dict.fromkeys(x for x in xs if x is not None))


# ---------------------------------------------------------------- payload Qdrant
def chunk_payload(row: dict, document_id: int, entities: list[str] | None = None) -> dict:
    return {"kind": row["kind"], "entities": entities or [], "document_id": document_id, "doc_key": row["doc_id"],
            "chunk_id": row["chunk_id"], "idx": row["idx"], "url": row["url"],
            "title": row["title"], "heading": row["heading"], "content": row["text"],
            "section": row["section"], "section_label": row.get("section_label"),
            "doc_type": row["doc_type"], "published_at": row["published_at"],
            "year": row["year"], "parent_url": row.get("parent_url"),
            **({"heading_path": row["heading_path"], "block_range": row["block_range"]}
               if row.get("block_range") is not None else {}),   # D23: chỉ chunk dựng từ block
            **({"page": row["page"]} if row.get("page") is not None else {})}   # D24: trang PDF/PPTX


def caption_rec(doc: dict, media_kind: str, caption_id: str, text: str,
                timestamp: float | None = None, heading_path: list[str] | None = None,
                block: int | None = None) -> dict:
    """Bản ghi caption cùng dạng captions_*.jsonl của compute (dùng cho incremental).
    heading_path/block (D23): mục + block chứa ảnh trong bài, chỉ có khi bài có cấu trúc."""
    rec = {"caption_id": caption_id, "media_kind": media_kind, "doc_id": doc["doc_id"],
           "url": doc["url"], "title": doc["title"], "section": doc["section"],
           "doc_type": doc["doc_type"], "published_at": doc["published_at"], "year": doc["year"],
           "text": text, "timestamp": timestamp}
    if heading_path:
        rec["heading_path"], rec["block"] = heading_path, block
    return rec


def caption_payload(cap: dict, document_id: int, media_url: str, media_key: str | None,
                    video_key: str | None = None, entities: list[str] | None = None) -> dict:
    """Caption (sict_text_v2): url = ảnh/video (nguồn media), page_url = bài chứa nó."""
    return {"kind": "media_caption", "media_kind": cap["media_kind"], "entities": entities or [],
            "caption_id": cap["caption_id"], "media_key": media_key, "video_key": video_key,
            "document_id": document_id, "doc_key": cap["doc_id"], "url": media_url,
            "page_url": cap["url"], "title": cap["title"], "caption": cap["text"],
            "section": cap["section"], "doc_type": cap["doc_type"],
            "published_at": cap["published_at"], "year": cap["year"],
            "timestamp": cap.get("timestamp"),
            **({"heading_path": cap["heading_path"], "block_idx": cap.get("block")}
               if cap.get("heading_path") else {})}   # D23: mục chứa ảnh trong bài


def media_payload(kind: str, sha: str, s3_key: str, url: str, pages: list[tuple[int, dict]],
                  caption: str = "", urls: list[str] | None = None, timestamp: float | None = None,
                  video_key: str | None = None, entities: list[str] | None = None,
                  entities_strong: list[str] | None = None) -> dict:
    """Point ảnh/frame (sict_image_v2). pages: [(document_id PG, doc dict)] — metadata dạng danh
    sách (lọc cứng khớp khi BẤT KỲ bài chứa nào khớp)."""
    docs = [d for _, d in pages]
    pubs = [d["published_at"] for d in docs if d.get("published_at")]
    return {"kind": kind, "media_key": sha, "s3_key": s3_key, "url": url, "urls": urls or [url],
            "document_id": _uniq(i for i, _ in pages), "page_urls": _uniq(d["url"] for d in docs),
            "title": docs[0]["title"] if docs else "", "caption": caption[:600],
            "section": _uniq(d.get("section") for d in docs),
            "doc_type": _uniq(d.get("doc_type") for d in docs),
            "year": _uniq(d.get("year") for d in docs),
            "published_at": max(pubs) if pubs else None,
            "timestamp": timestamp, "video_key": video_key,
            "entities": entities or [], "entities_strong": entities_strong or []}


def image_caption(alt: str, ocr: str) -> str:
    return " | ".join(x for x in (alt.strip(), ocr.strip()[:300]) if x)


def media_key_for(sha: str, ext: str) -> str:
    return f"{get_settings().s3.prefix_media}{sha}.{ext}"


# ---------------------------------------------------------------- Postgres
def doc_hash(d: dict) -> str:
    """hash_v2: trang = sha1 markdown sạch; đính kèm = sha256 file (tên file trong snapshot)."""
    if d.get("content_hash"):
        return d["content_hash"]
    if d.get("file_sha256"):
        return d["file_sha256"]
    return Path(d.get("file_path", "")).stem


def _doc_values(d: dict, raw_key: str | None = None) -> dict:
    pub = dt.datetime.fromisoformat(d["published_at"]) if d.get("published_at") else None
    meta = {"spec": ix.SPEC_VERSION, "source": d.get("source"), "section_label": d.get("section_label"),
            "n_chunks": d.get("n_chunks")}
    if d.get("parent_title"):
        meta["parent_title"] = d["parent_title"]
    return {"url": d["url"], "title": (d.get("title") or "")[:512], "raw_s3_key": raw_key or "",
            "content_hash": "", "meta": meta, "crawled_at": _now(), "doc_key": d["doc_id"],
            "section": d.get("section"), "doc_type": d.get("doc_type"), "published_at": pub,
            "year": d.get("year"), "parent_url": d.get("parent_url"), "hash_v2": doc_hash(d),
            "structure": d.get("structure"), "hash_struct": d.get("hash_struct")}


async def upsert_documents(session, docs: list[dict], raw_key: str | None = None) -> dict[str, int]:
    """Upsert theo url (giữ id cũ → point v1 của cùng bài vẫn trỏ đúng khi rollback).
    content_hash/raw_s3_key cũ (v1) giữ nguyên trừ khi có raw_key mới. → {doc_key: id}."""
    out: dict[str, int] = {}
    for i in range(0, len(docs), 500):
        part = [_doc_values(d, raw_key) for d in docs[i:i + 500]]
        stmt = pg_insert(Document).values(part)
        upd = {c: stmt.excluded[c] for c in ("title", "crawled_at", "doc_key", "section", "doc_type",
                                              "published_at", "year", "parent_url", "hash_v2", "structure", "hash_struct")}
        upd["meta"] = Document.meta.op("||")(stmt.excluded.meta)
        if raw_key:
            upd["raw_s3_key"] = stmt.excluded.raw_s3_key
        stmt = stmt.on_conflict_do_update(index_elements=["url"], set_=upd)
        res = await session.execute(stmt.returning(Document.id, Document.doc_key))
        out.update({k: i_ for i_, k in res.all()})
    return out


async def _upsert_returning(session, model, rows: list[dict], key: str,
                            update: tuple[str, ...], union: tuple[str, ...] = ()) -> dict[str, int]:
    """`union`: cột JSONB danh sách — hợp với giá trị đang có thay vì ghi đè."""
    out: dict[str, int] = {}
    col = getattr(model, key)
    tbl = model.__tablename__
    for i in range(0, len(rows), 500):
        stmt = pg_insert(model).values(rows[i:i + 500])
        set_ = {c: stmt.excluded[c] for c in update}
        for c in union:
            set_[c] = literal_column(
                f"(SELECT coalesce(jsonb_agg(DISTINCT e), '[]'::jsonb) "
                f"FROM jsonb_array_elements({tbl}.{c} || excluded.{c}) AS e)")
        stmt = stmt.on_conflict_do_update(index_elements=[key], set_=set_)
        res = await session.execute(stmt.returning(model.id, col))
        out.update({k: i_ for i_, k in res.all()})
    return out


async def upsert_videos(session, rows: list[dict]) -> dict[str, int]:
    return await _upsert_returning(session, Video, rows, "video_key",
                                   ("url", "title", "subs", "frames", "status"))


async def upsert_media(session, rows: list[dict]) -> dict[str, int]:
    """frame_sets hợp với bộ đã có (cùng khung ở cả bộ v2 lẫn v3 → 1 dòng, 2 tag)."""
    rows = [{"frame_sets": [], **r} for r in rows]
    return await _upsert_returning(session, Media, rows, "sha256",
                                   ("kind", "s3_key", "source_url", "ocr_text", "ocr_status",
                                    "ocr_attempts", "video_id", "timestamp", "point_id"), union=("frame_sets",))


def frame_set_tag() -> str:
    """Bộ khung = collection ảnh chứa point của khung (models.Media.frame_sets)."""
    return get_settings().qdrant.collection_image


def pick_frame_set(frames: list, tag: str) -> tuple[list, bool]:
    """Khung (models.Media) của 1 video → (khung thuộc bộ `tag`, cần gắn tag?). Chưa có khung nào của bộ `tag`
    (video chỉ có ở index cũ, hoặc DB nạp trước khi có frame_sets) → nhận trọn MỘT bộ cũ — bộ có dòng mới nhất —
    chứ không trộn các bộ: trộn làm 1 video mang 2 bộ khung (2026-10-21: doc "Giới thiệu" 10 → 23 khung)."""
    cur = [m for m in frames if tag in (m.frame_sets or [])]
    if cur or not frames:
        return cur, False
    groups: dict[tuple, list] = {}
    for m in frames:
        groups.setdefault(tuple(sorted(m.frame_sets or [])), []).append(m)
    best = max(groups.values(), key=lambda g: max(m.id for m in g))
    return sorted(best, key=lambda m: m.timestamp if m.timestamp is not None else -1.0), True


async def tag_frame_set(session, shas: list[str], tag: str) -> int:
    """Gắn bộ `tag` cho các khung theo sha256 (DB nạp trước khi có frame_sets: loader --tag-frames)."""
    n = 0
    for i in range(0, len(shas), 1000):
        res = await session.execute(sql_text(
            "UPDATE media SET frame_sets = frame_sets || to_jsonb(CAST(:tag AS text)) "
            "WHERE kind = 'video_frame' AND sha256 = ANY(:shas) AND NOT frame_sets ? :tag"),
            {"tag": tag, "shas": shas[i:i + 1000]})
        n += res.rowcount or 0
    return n


async def add_occurrences(session, model, rows: list[dict], cols: tuple[str, str]) -> None:
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(model).values(rows[i:i + 1000])
        upd = {"caption_point_id": stmt.excluded.caption_point_id}
        if model is MediaOccurrence:
            upd.update(alt=stmt.excluded.alt, pos=stmt.excluded.pos, context=stmt.excluded.context,
                       block_idx=stmt.excluded.block_idx, heading_path=stmt.excluded.heading_path)
        elif model is VideoOccurrence:
            upd["block_idx"] = stmt.excluded.block_idx
        await session.execute(stmt.on_conflict_do_update(index_elements=list(cols), set_=upd))


# ---------------------------------------------------------------- Qdrant
def write_text(items: list[tuple[str, str, dict]], batch: int | None = None) -> int:
    """items: (point_id, chuỗi embed, payload) → embed + sparse BM25 → collection text (chunk) / ocr (caption)."""
    from . import embedder
    if not items:
        return 0
    bs = batch or get_settings().embedding.batch_size
    pts = []
    for i in range(0, len(items), bs):
        part = items[i:i + bs]
        vecs = embedder.embed_texts([t for _, t, _ in part])
        pts += [vector_store.text_point(pid, v, t, p) for (pid, t, p), v in zip(part, vecs)]
    by_col: dict[str, list] = {}
    for p in pts:
        by_col.setdefault(vector_store.collection_for(p.payload), []).append(p)
    for name, part in by_col.items():
        vector_store.upsert_points(name, part)
    return len(pts)


def write_media_point(pid: str, vec: list[float], payload: dict) -> None:
    from qdrant_client.models import PointStruct
    vector_store.upsert_points(get_settings().qdrant.collection_image,
                               [PointStruct(id=pid, vector=list(vec), payload=payload)])


# ---------------------------------------------------------------- incremental: 1 trang
async def clear_document(session, doc: Document) -> None:
    """Gỡ dữ liệu v2 của 1 bài (point chữ + caption, gỡ bài khỏi point media, occurrence)."""
    from sqlalchemy import delete
    vector_store.delete_by_document(doc.id)
    await session.execute(delete(MediaOccurrence).where(MediaOccurrence.document_id == doc.id))
    await session.execute(delete(VideoOccurrence).where(VideoOccurrence.document_id == doc.id))


async def remove_document(session, doc: Document) -> int:
    """404/410: xoá bài + file đính kèm của nó. Postgres trước (DELETE thẳng, FK ON DELETE CASCADE
    lo chunks/media_assets/occurrence — không nạp hàng v1 vào ORM), commit, rồi mới gỡ point Qdrant:
    lỗi giữa chừng chỉ để lại point mồ côi (đối soát dọn được), không bao giờ còn bài trong PG mà
    mất point. Trả số tài liệu đã xoá."""
    from sqlalchemy import delete
    ids = [doc.id, *(await session.scalars(select(Document.id).where(
        Document.parent_url == doc.url, Document.doc_type == "attachment"))).all()]
    await session.execute(delete(Document).where(Document.id.in_(ids)))
    await session.commit()
    for i in ids:
        vector_store.delete_by_document(i)
    return len(ids)


async def _download(url: str, max_bytes: int) -> tuple[bytes, str] | None:
    import httpx
    ua = get_settings().crawl.user_agent
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                     headers={"User-Agent": ua}) as hc:
            r = await hc.get(url)
    except Exception:
        return None
    if r.status_code != 200 or not r.content or len(r.content) > max_bytes:
        return None
    return r.content, r.headers.get("content-type", "").split(";")[0].strip().lower()


async def _index_images(session, st, doc: dict, doc_pk: int, blocks_at: dict) -> list[dict]:
    """Ảnh của bài → media + point (dùng chung theo sha) + occurrence. Trả các lần xuất hiện để
    index_page dựng caption sau khi có graph."""
    from . import embedder, ocr
    occ: list[dict] = []
    seen: set[str] = set()
    for pos, im in enumerate(doc["images"][:MAX_IMAGES_PER_PAGE]):
        m = await session.scalar(select(Media).where(
            Media.source_url == im["url"], Media.kind == "image").limit(1))
        data = None
        if m is None:
            got = await _download(im["url"], 20 * 1024 * 1024)
            if not got or not got[1].startswith("image/") or len(got[0]) < MIN_IMAGE_BYTES:
                continue
            data, ctype = got
            sha = _sha256(data)
            m = await session.scalar(select(Media).where(Media.sha256 == sha))
            if m is None:
                ext = _IMG_EXT.get(ctype) or (Path(urlparse(im["url"]).path).suffix.lstrip(".").lower()[:4]
                                              or "jpg")
                key, _ = st.put_media(data, ext)
                try:
                    text = await asyncio.to_thread(ocr.ocr_text, data)
                    status = "done"
                except Exception:
                    text, status = "", "failed"
                m = Media(sha256=sha, kind="image", s3_key=key, source_url=im["url"], ocr_text=text,
                          ocr_status=status, ocr_attempts=1, point_id=C.stable_id("image", sha))
                session.add(m)
                await session.flush()
        if m.sha256 in seen:
            continue  # cùng ảnh lặp trong 1 bài → 1 lần xuất hiện, 1 caption
        seen.add(m.sha256)
        n_occ = await session.scalar(select(func.count()).select_from(MediaOccurrence).where(
            MediaOccurrence.media_id == m.id, MediaOccurrence.document_id != doc_pk))
        if n_occ >= DECORATIVE_DOCS:
            continue  # ảnh trang trí dùng khắp site
        if not vector_store.attach_document([m.point_id], doc_pk, doc["url"]):
            # point chưa có (ảnh mới) hoặc đã bị xoá khi bài cuối cùng chứa nó bị gỡ → tạo lại
            if data is None:
                data = st.get_media(m.s3_key)
            vecs, ok = embedder.embed_image_bytes([data])
            if not ok:
                continue
            write_media_point(m.point_id, vecs[0], media_payload(
                "image", m.sha256, m.s3_key, im["url"], [(doc_pk, doc)],
                image_caption(im.get("alt", ""), m.ocr_text)))
        cid = C.stable_id("caption", "image", m.sha256, doc["doc_id"])
        blk, path = blocks_at.get(im["url"], (None, []))
        occ.append({"m": m, "url": im["url"], "alt": im.get("alt", ""), "ctx": im.get("context", ""),
                    "cid": cid, "text": C.image_caption_text(doc, im.get("alt", ""), m.ocr_text, path),
                    "path": path, "blk": blk})
        await add_occurrences(session, MediaOccurrence, [{
            "document_id": doc_pk, "media_id": m.id, "alt": im.get("alt", ""), "pos": pos,
            "context": im.get("context", ""), "caption_point_id": cid,
            "block_idx": blk, "heading_path": path}], ("document_id", "media_id"))
    return occ


async def _index_videos(session, st, doc: dict, doc_pk: int, blocks_at: dict) -> list[dict]:
    from . import embedder, ocr, video
    occ: list[dict] = []
    tag = frame_set_tag()
    for vurl in list(dict.fromkeys(doc["videos"]))[:MAX_VIDEOS_PER_PAGE]:
        key = C.video_key(vurl)
        v = await session.scalar(select(Video).where(Video.video_key == key))
        if v is None or v.status == "blocked":
            frames = await asyncio.to_thread(video.extract_keyframes, vurl)
            try:
                info = await asyncio.to_thread(video.fetch_video_info, vurl)
            except Exception:
                info = {}
            vid = (await upsert_videos(session, [{
                "video_key": key, "url": vurl, "title": (info.get("title") or "")[:512],
                "subs": info.get("subs") or "", "frames": 0,
                "status": "ok" if frames else "blocked"}]))[key]
            v = await session.get(Video, vid, populate_existing=True)
            if frames:
                vecs, ok = embedder.embed_image_bytes([b for b, _ in frames])
                order = video.dedup_indices(vecs, get_settings().video.dedup_threshold)
                rows = []
                for k in order:
                    b, ts = frames[ok[k]]
                    sha = _sha256(b)
                    s3_key, _ = st.put_media(b, "png")
                    try:
                        text = await asyncio.to_thread(ocr.ocr_text, b)
                    except Exception:
                        text = ""
                    fid = C.stable_id("frame", key, f"{ts:.2f}")
                    rows.append({"sha256": sha, "kind": "video_frame", "s3_key": s3_key,
                                 "source_url": vurl, "ocr_text": text, "ocr_status": "done",
                                 "ocr_attempts": 1, "video_id": v.id, "timestamp": ts,
                                 "point_id": fid, "frame_sets": [tag]})
                    write_media_point(fid, vecs[k], media_payload(
                        "video_frame", sha, s3_key, vurl, [(doc_pk, doc)], v.title, timestamp=ts,
                        video_key=key))
                await upsert_media(session, rows)
                v.frames = len(rows)
        frames_m, adopt = pick_frame_set((await session.scalars(
            select(Media).where(Media.video_id == v.id).order_by(Media.timestamp))).all(), tag)
        if adopt:   # bộ cũ thành bộ của collection này; point dựng lại bên dưới (lost) từ byte đã lưu
            for m in frames_m:
                m.frame_sets = [*(m.frame_sets or []), tag]
        found = set(vector_store.attach_document([m.point_id for m in frames_m], doc_pk, doc["url"]))
        lost = [m for m in frames_m if m.point_id not in found]
        if lost:  # bài cuối cùng chứa video vừa bị gỡ (clear_document) → dựng lại point frame
            vecs, ok = embedder.embed_image_bytes([st.get_media(m.s3_key) for m in lost])
            for j, vec in zip(ok, vecs):
                m = lost[j]
                write_media_point(m.point_id, vec, media_payload(
                    "video_frame", m.sha256, m.s3_key, v.url, [(doc_pk, doc)], v.title,
                    timestamp=m.timestamp, video_key=key))
        ocr_join = C.frames_ocr_join([m.ocr_text for m in frames_m])
        cid = C.stable_id("caption", "video", key, doc["doc_id"])
        text = C.video_caption_text(doc, {"title": v.title, "subs": v.subs}, ocr_join)
        occ.append({"v": v, "key": key, "frames": frames_m, "cid": cid, "text": text,
                    "ocr_join": ocr_join})
        vblk = blocks_at.get(v.url, (None, []))[0]
        await add_occurrences(session, VideoOccurrence, [{
            "document_id": doc_pk, "video_id": v.id, "caption_point_id": cid, "block_idx": vblk}],
            ("document_id", "video_id"))
    return occ


async def _index_attachments(session, doc: dict, attachments: list[tuple[str, str]]) -> list[int]:
    """File đính kèm của trang: tải → nhận định dạng theo byte → trích text (OCR PDF scan) →
    tài liệu riêng (doc_type=attachment, parent_url). File bị gỡ khỏi trang → xoá tài liệu."""
    children = {d.url: d for d in (await session.scalars(select(Document).where(
        Document.parent_url == doc["url"], Document.doc_type == "attachment"))).all()}
    keep: set[str] = set()
    done: list[int] = []
    for furl, desc in attachments:
        if not furl.startswith(("http://", "https://")) or furl in keep:
            continue
        keep.add(furl)
        got = await _download(furl, MAX_FILE_BYTES)
        if not got or got[1].startswith("text/html"):
            continue
        data = got[0]
        sha = _sha256(data)
        child = children.get(furl) or await session.scalar(select(Document).where(Document.url == furl))
        if child is not None and child.hash_v2 == sha:
            continue
        same = await session.scalar(select(Document.id).where(
            Document.hash_v2 == sha, Document.url != furl).limit(1))
        if same is not None:
            continue  # cùng file đã index dưới URL khác
        suffix = Path(urlparse(furl).path).suffix[:6] or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(data)
            tmp.flush()
            pages, status = await asyncio.to_thread(ix.extract_file_pages, Path(tmp.name), 120, True)
        if status not in ("ok", "ok_ocr"):
            continue
        ad = {**C.attachment_doc(furl, desc, doc), "file_sha256": sha}
        text = "\n\n".join(t for _, t in pages)
        rows = [C.chunk_row(c) for c in C.doc_chunks(ad, text, pages=pages)]
        ad["n_chunks"] = len(rows)
        if child is not None:
            vector_store.delete_by_document(child.id)
        pk = (await upsert_documents(session, [ad]))[ad["doc_id"]]
        write_text([(r["chunk_id"], r["text"], chunk_payload(r, pk)) for r in rows])
        done.append(pk)
    for furl, child in children.items():
        if furl not in keep:
            await remove_document(session, child)
    return done


async def index_page(session, st, url: str, html: str, depth: int = 0, frontier=None,
                     force: bool = False) -> str:
    """Incremental theo index spec v2. Trả trạng thái: new | changed | unchanged | duplicate |
    listing | error_page | content_fallback. Bỏ qua khi markdown sạch không đổi (hash_v2) — bộ đếm
    lượt xem trong HTML không còn làm bài "đổi" mỗi lần tải. Caller không cần commit."""
    from . import parser
    from .snapshot import link_depth
    vector_store.require_v2()
    status, doc, md, parsed = C.page_doc(url, html)
    if parsed is None:
        parsed = parser.parse_html(url, html)

    def push_links() -> None:
        if frontier is not None:
            for link in parsed.links:
                frontier.push(link, link_depth(depth, link))

    existing = await session.scalar(select(Document).where(Document.url == url))
    # D23: markdown giống nhưng cấu trúc đổi (ảnh/video/file thêm bớt, đổi chỗ heading) → index lại.
    # Bài chưa có hash_struct (index trước D23): chỉ ghi bù cấu trúc, không index lại.
    new_struct = doc.get("hash_struct") if status == "ok" else None
    struct_changed = bool(existing is not None and existing.hash_struct and new_struct
                          and existing.hash_struct != new_struct)
    if status != "ok" or (existing is not None and existing.hash_v2 == doc["content_hash"]
                          and not force and not struct_changed):
        if existing is not None:
            existing.crawled_at = _now()
            if status == "ok" and existing.hash_struct is None and doc.get("hash_struct"):
                existing.structure, existing.hash_struct = doc["structure"], doc["hash_struct"]
            await session.commit()
        if status != "ok":
            push_links()  # listing: nguồn link bài mới
            return status
        return "unchanged"
    dup = None if struct_changed else await session.scalar(select(Document.url).where(
        Document.hash_v2 == doc["content_hash"], Document.url != url).limit(1))
    if dup is not None:  # cùng bài đăng ở URL khác (AC-R6) → giữ bản đã có
        if existing is not None:
            existing.hash_v2, existing.crawled_at = None, _now()
            await session.commit()
            await clear_document(session, existing)
            await session.commit()
        return "duplicate"
    if existing is not None:
        # đánh dấu "chưa index xong" TRƯỚC khi gỡ point: lỗi giữa chừng → lần sau xử lý lại,
        # không bị coi là "unchanged" khi point đã mất
        existing.hash_v2 = None
        await session.commit()
        await clear_document(session, existing)

    raw_key = st.put_raw_html(url, html.encode())
    rows = [C.chunk_row(c) for c in C.doc_chunks(doc, md, blocks=parsed.blocks)]
    doc["n_chunks"] = len(rows)
    pk = (await upsert_documents(session, [doc], raw_key))[doc["doc_id"]]
    blocks_at = ix.block_index(doc.get("structure"))   # media → (block, heading_path), 1 lần / bài
    imgs = await _index_images(session, st, doc, pk, blocks_at)
    vids = await _index_videos(session, st, doc, pk, blocks_at)

    items = [(r["chunk_id"], r["text"], chunk_payload(r, pk)) for r in rows]
    for o in imgs:
        items.append((o["cid"], o["text"], caption_payload(
            caption_rec(doc, "image", o["cid"], o["text"], heading_path=o["path"], block=o["blk"]),
            pk, o["url"], o["m"].sha256)))
    for o in vids:
        first = o["frames"][0] if o["frames"] else None
        items.append((o["cid"], o["text"], caption_payload(
            caption_rec(doc, "video", o["cid"], o["text"], first.timestamp if first else None), pk,
            o["v"].url, first.sha256 if first else None, o["key"])))
    write_text(items)
    att = await _index_attachments(session, doc, parsed.attachments)
    await session.commit()
    # graph (D20): cùng đường code với bulk/rebuild, đọc lại từ PG + Qdrant; payload sinh từ PG.
    from . import graphsync
    await graphsync.update_documents(session, [pk, *att])
    push_links()
    return "changed" if existing is not None else "new"
