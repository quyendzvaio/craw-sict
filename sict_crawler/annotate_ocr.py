"""OCR-only media annotation: title + OCR text → caption (no VLM/face/CLIP-torch).

Replaces face.annotate_pending for the text+OCR pipeline:
  caption = "{doc_title}. {ocr_text}" (or doc_title alone when OCR empty)

Callers: main.py --annotate-ocr / --incremental, container svc-ocr + cron (compose).
Schemas: same caption rows as before for Qdrant sict_text (E5):
  {media_id, document_id, url, kind='media_caption', media_kind, title, caption, timestamp}

Video keyframes: frames sharing (document_id, source_url) get one aggregated
caption "title. frame OCRs joined" — upsert a single Qdrant point per video
instead of one per frame (avoids RRF spam), but every frame row in PG still
gets the shared caption so /media serving stays consistent.
"""
from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import func, select

from .models import Document, MediaAsset


def build_caption(doc_title: str, ocr_text: str, max_len: int = 500) -> str:
    """title + OCR → caption. Title alone when OCR empty/too short."""
    title = (doc_title or "").strip()
    ocr = (ocr_text or "").strip()
    if len(ocr) < 10:
        return title[:max_len]
    cap = f"{title}. {ocr}" if title else ocr
    return cap[:max_len]


def _load_bytes(asset_bytes: bytes) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(asset_bytes)).convert("RGB")
    except Exception:
        return None


async def annotate_ocr_batch(session, st, items: list, doc_title: str,
                             doc_text: str = "") -> list[dict]:
    """OCR a list of (MediaAsset, bytes). Sets asset.caption in place.

    Returns caption rows for Qdrant (E5 embed done by caller).
    Idempotent: assets that already have caption are skipped by caller,
    but double-call here won't duplicate rows beyond one call's output.
    """
    from . import ocr as _ocr

    rows: list[dict] = []
    # group video frames by source_url for aggregated caption
    video_groups: dict[str, list] = {}
    singles: list = []
    for a, b in items:
        # subtitle-only / title-only van can OCR them (AC-D)
        if a.caption and "sub:" not in a.caption \
                and "| vid:" not in a.caption:
            continue
        if a.kind == "video_frame" and a.source_url:
            video_groups.setdefault(a.source_url, []).append((a, b))
        else:
            singles.append((a, b))

    for a, b in singles:
        try:
            txt = _ocr.ocr_text(b)
        except Exception:
            txt = ""
        if "sub:" in (a.caption or "") or "| vid:" in (a.caption or ""):
            txt = txt.strip()
            if len(txt) >= 10 and txt not in (a.caption or ""):
                a.caption = f"{a.caption} | ocr: {txt}"[:900]
            cap = a.caption
        else:
            cap = build_caption(doc_title, txt)
        if cap and not a.caption:
            a.caption = cap
        if cap:
            rows.append({"media_id": a.id, "document_id": a.document_id,
                         "url": a.source_url, "kind": "media_caption",
                         "media_kind": a.kind, "title": doc_title,
                         "caption": cap, "timestamp": a.timestamp})

    for src, group in video_groups.items():
        parts: list[str] = []
        for a, b in group:
            try:
                txt = _ocr.ocr_text(b)
            except Exception:
                txt = ""
            txt = txt.strip()
            if len(txt) >= 10 and txt not in parts:
                parts.append(txt)
        ocr_part = " | ".join(parts)[:400]
        base_sub = (group[0][0].caption or "") if group else ""
        if "sub:" in base_sub or "| vid:" in base_sub:
            shared = base_sub
            if ocr_part and ocr_part not in base_sub:
                shared = f"{base_sub} | ocr: {ocr_part}"[:900]
        else:
            shared = build_caption(doc_title, ocr_part)
        for a, _ in group:
            if "sub:" in (a.caption or "") or "| vid:" in (a.caption or ""):
                if ocr_part and ocr_part not in (a.caption or ""):
                    a.caption = f"{a.caption} | ocr: {ocr_part}"[:900]
            elif shared and not a.caption:
                a.caption = shared
        if shared:
            # one Qdrant point per video (use first frame's ids)
            first = group[0][0]
            rows.append({"media_id": first.id, "document_id": first.document_id,
                         "url": src, "kind": "media_caption",
                         "media_kind": "video_frame", "title": doc_title,
                         "caption": shared, "timestamp": first.timestamp})
    return rows


async def annotate_pending_ocr(session, st, limit: int = 500) -> int:
    """Backlog pass: OCR MediaAsset thiếu caption. Per-asset commit.

    Picks assets with caption=='' (persons column ignored — legacy face rows
    with via='skip' but empty caption are still picked, which is what we want
    for the one-time migration off face/VLM).
    Returns number of assets processed.
    """
    from . import embedder, storage as _storage, vector_store

    ids = (await session.scalars(
        select(MediaAsset.id)
        .where((MediaAsset.caption == "") |
               ((MediaAsset.caption.like("%sub:%")) &
                (~MediaAsset.caption.like("%ocr:%"))) |
               ((MediaAsset.caption.like("%| vid:%")) &
                (~MediaAsset.caption.like("%ocr:%")) &
                (~MediaAsset.caption.like("%sub:%"))))
        .order_by(MediaAsset.id)
        .limit(limit))).all()
    if not ids:
        return 0

    done = 0
    synced: list[tuple[str, str]] = []
    for aid in ids:
        a = await session.get(MediaAsset, aid)
        if a is None or (a.caption and "sub:" not in a.caption
                         and "| vid:" not in a.caption):
            continue
        rows: list[dict] = []
        try:
            doc = await session.get(Document, a.document_id)
            b = (_storage.from_base64(a.base64_data) if a.base64_data
                 else st.get_media(a.s3_key))
            rows = await annotate_ocr_batch(
                session, st, [(a, b)],
                doc_title=doc.title if doc and doc.title else "",
                doc_text="")
        except Exception as e:
            print(f"[annotate-ocr] skip {a.id}: {e}")
        if not a.caption:
            # mark processed even on total failure (empty OCR + empty title)
            # so the backlog cursor advances instead of looping forever
            try:
                doc_again = await session.get(Document, a.document_id)
                a.caption = (doc_again.title[:500] if doc_again and doc_again.title
                             else "(no text)")
            except Exception:
                a.caption = "(no text)"
        done += 1
        if a.vector_id and a.caption:
            synced.append((a.vector_id, a.caption))
        await session.commit()
        if rows:
            try:
                vecs = embedder.embed_texts(
                    [r["title"] + ". " + r["caption"] for r in rows])
                vector_store.upsert_media_captions(
                    [{**r, "vector": v} for r, v in zip(rows, vecs)])
            except Exception as e:
                print(f"[annotate-ocr] caption upsert skipped: {e}")
    if synced:
        try:
            vector_store.set_image_captions(synced)
        except Exception as e:
            print(f"[annotate-ocr] payload sync skipped: {e}")
    return done


async def run_annotate_ocr(limit: int, chunk: int = 500) -> int:
    """Chunked driver (new session per chunk → identity-map GC). Returns total."""
    from . import db, storage, vector_store

    if vector_store.has_sparse():  # collection v2: OCR đã chạy lúc index (indexer)
        print("[annotate-ocr] bỏ qua: chỉ dành cho index v1 (media_assets)")
        return 0
    st = storage.Storage()
    vector_store.ensure_collections()
    await db.init_db()
    total = 0
    err_streak = 0
    while total < limit:
        async with db.get_session() as session:
            try:
                n = await annotate_pending_ocr(session, st, min(chunk, limit - total))
                err_streak = 0
            except Exception as e:
                print(f"[annotate-ocr] chunk error: {e}", flush=True)
                n, err_streak = 0, err_streak + 1
        if n == 0 or err_streak >= 3:
            break
        total += n
        print(f"[annotate-ocr] {total}/{limit}", flush=True)
    print(f"annotated-ocr {total} media assets")
    return total


async def backfill_titles_only(session, limit: int = 5000) -> int:
    """Fast path for assets whose image bytes are gone (S3/MinIO key lost):
    set caption=doc title so they stay retrievable via E5 without OCR."""
    ids = (await session.scalars(
        select(MediaAsset.id)
        .where(MediaAsset.caption == "")
        .order_by(MediaAsset.id)
        .limit(limit))).all()
    n = 0
    for aid in ids:
        a = await session.get(MediaAsset, aid)
        if a is None or a.caption:
            continue
        doc = await session.get(Document, a.document_id)
        if doc and doc.title:
            a.caption = doc.title[:500]
            n += 1
    await session.commit()
    return n
