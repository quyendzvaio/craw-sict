"""Nạp artifact (compute.py) → Postgres + Qdrant `*_v2` + S3 — bước 3 của D16, chạy trên CPU.

Không embed lại chunk/ảnh (vector có sẵn trong .npy). Point id = uuid5 của artifact → chạy lại ghi
đè, không nhân bản. Collection v1 (`sict_text`/`sict_image`) không bị đụng → rollback API = trỏ
QDRANT_COLLECTION_TEXT/IMAGE về tên cũ. Bảng `documents` upsert theo url, giữ id cũ (point v1 vẫn
trỏ đúng); cột v1 (content_hash, raw_s3_key) giữ nguyên.

Thứ tự: PG documents (upsert theo url, giữ id cũ) → videos → media (ảnh + frame, đưa file lên store)
→ occurrence → Qdrant text (chunk + caption: dense E5 + sparse BM25) → Qdrant image (ảnh + frame)
→ kiểm tra số point = số dòng artifact.

Usage:
  python -m sict_crawler.loader --artifacts _snapshot/2026-10-12/artifacts/v2-multilingual-e5-small
  python -m sict_crawler.loader --artifacts A --recreate        # xoá & tạo lại collection *_v2
  python -m sict_crawler.loader --artifacts A --skip-media-upload   # store đã có file media
  python -m sict_crawler.loader --artifacts A --upload-media        # store S3: BẮT BUỘC có cờ mới upload
  python -m sict_crawler.loader --artifacts A --media-only --upload-media   # chỉ upload media (cutover)
  python -m sict_crawler.loader --artifacts A --tag-frames      # chỉ gắn bộ khung (media.frame_sets) cho DB nạp trước
Collection đích = config/env (QDRANT_COLLECTION_TEXT/IMAGE), phải là schema v2.
Callers: CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from . import db, indexer, storage, vector_store
from . import indexing as ix
from .compute import stable_id
from .config import get_settings
from .models import MediaOccurrence, VideoOccurrence


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _npy(path: Path, n: int) -> np.ndarray:
    if n == 0:
        return np.zeros((0, 0), dtype=np.float32)
    arr = np.load(path)
    if len(arr) != n:
        raise SystemExit(f"{path.name}: {len(arr)} vector ≠ {n} dòng jsonl — artifact hỏng")
    return arr


class Loader:
    def __init__(self, art: Path, snap: Path | None, upload_media: bool = True,
                 allow_s3_upload: bool = False):
        self.art = art
        self.manifest = json.loads((art / "manifest.json").read_text())
        self.snap = snap or Path(self.manifest["snapshot"])
        self.cfg = get_settings()
        self.st = storage.Storage()
        self.stats: dict = {}
        # Upload lên S3 chỉ khi được bật TƯỜNG MINH (--upload-media): store local (dev) thì hard link
        # như thường; có credential AWS mà không có cờ → không ghi gì lên S3.
        self.upload = upload_media and (self.st.local or allow_s3_upload)
        if upload_media and not self.st.local and not allow_s3_upload:
            self.log("store là S3 nhưng KHÔNG có --upload-media → bỏ qua upload file media "
                     "(chỉ ghi Postgres + Qdrant)")

    def log(self, msg: str) -> None:
        print(f"[loader {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def check(self) -> None:
        """Vector artifact phải cùng model với encoder câu hỏi của API, nếu không điểm vô nghĩa."""
        emb = self.cfg.embedding
        if emb.backend == "api":   # D22: một model cho cả chữ và ảnh; số chiều + tiền tố query phải khớp
            want = {"backend": "api", "text_model": emb.model, "image_model": emb.model, "dim": emb.dim,
                    "query_prefix": emb.query_prefix}
        else:
            want = {"text_model": emb.text_model, "image_model": emb.image_model}
            if self.manifest.get("backend") == "api":
                raise SystemExit("artifact nhúng bằng backend api nhưng config đang backend torch — không nạp")
        for k, w in want.items():
            got = self.manifest.get(k)
            if got != w:
                raise SystemExit(f"manifest {k}={got!r} ≠ config {w!r} — không nạp")
        q = self.cfg.qdrant
        if emb.backend == "api" and (q.text_dim != emb.dim or q.image_dim != emb.dim):
            raise SystemExit(f"qdrant text_dim/image_dim ({q.text_dim}/{q.image_dim}) ≠ embedding.dim {emb.dim} "
                             "(đặt QDRANT_TEXT_DIM / QDRANT_IMAGE_DIM)")
        cols = (*vector_store.text_collections(), q.collection_image)
        if not all(c.endswith(("_v2", "_v3")) for c in cols):
            raise SystemExit(f"collection đích {'/'.join(cols)} không phải "
                             "*_v2/*_v3 — không ghi đè index v1 (đặt QDRANT_COLLECTION_TEXT/IMAGE/OCR)")

    def _put(self, key: str, path: Path) -> None:
        if self.upload and self.st.put_file(key, path):
            self.stats["uploaded"] = self.stats.get("uploaded", 0) + 1

    def _contexts(self, docs) -> dict[tuple[str, str], str]:
        """(doc_id, url ảnh) → đoạn văn sát ảnh. Artifact mới có sẵn `images[].context` (compute);
        artifact cũ thì parse lại HTML snapshot (~1–2 phút)."""
        import gzip

        from . import parser
        out: dict[tuple[str, str], str] = {}
        for d in docs:
            ims = d.get("images") or []
            if ims and all("context" in i for i in ims):
                out.update({(d["doc_id"], i["url"]): i["context"] for i in ims})
                continue
            if not d.get("html_path"):
                continue
            try:
                html = gzip.decompress((self.snap / d["html_path"]).read_bytes()).decode("utf-8", "ignore")
            except OSError:
                continue
            p = parser.parse_html(d["url"], html)
            out.update({(d["doc_id"], u): c for u, c in zip(p.image_urls, p.image_contexts)})
        return out

    async def run(self, recreate: bool = False) -> dict:
        self.check()
        q = self.cfg.qdrant
        client = vector_store.get_client()
        if recreate:
            for name in (*vector_store.text_collections(), q.collection_image):
                if client.collection_exists(name):
                    client.delete_collection(name)
                    self.log(f"xoá collection {name}")
        vector_store.ensure_collections()
        vector_store.require_v2()
        await db.init_db()

        a = self.art
        docs = _jsonl(a / "documents.jsonl")
        chunks = _jsonl(a / "chunks.jsonl")
        images = _jsonl(a / "images.jsonl")
        caps_i = _jsonl(a / "captions_image.jsonl")
        frames = _jsonl(a / "frames.jsonl")
        caps_v = _jsonl(a / "captions_video.jsonl")
        videos = _jsonl(a / "videos.jsonl")
        v_chunks = _npy(a / "chunks.npy", len(chunks))
        v_images = _npy(a / "images.npy", len(images))
        v_caps_i = _npy(a / "captions_image.npy", len(caps_i))
        v_frames = _npy(a / "frames.npy", len(frames))
        v_caps_v = _npy(a / "captions_video.npy", len(caps_v))
        doc_by_key = {d["doc_id"]: d for d in docs}

        # ---- Postgres
        frame_sha: dict[str, str] = {}
        for f in frames:
            frame_sha[f["frame_id"]] = hashlib.sha256((a / f["path"]).read_bytes()).hexdigest()
        async with db.get_session() as s:
            pk = await indexer.upsert_documents(s, docs)
            self.log(f"documents: {len(pk)}")
            ocr_by_key = {r["key"]: r["text"] for r in _jsonl(a / "ocr_cache.jsonl")}
            vid_pk = await indexer.upsert_videos(s, [{
                "video_key": v["video_key"], "url": v["url"], "title": (v.get("title") or "")[:512],
                "subs": "", "frames": v.get("frames", 0),
                "status": "ok" if v.get("frames") else "blocked"} for v in videos])
            media_rows = []
            for r in images:
                ext = Path(r["path"]).suffix.lstrip(".") or "jpg"
                key = indexer.media_key_for(r["sha256"], ext)
                self._put(key, self.snap / r["path"])
                media_rows.append({"sha256": r["sha256"], "kind": "image", "s3_key": key,
                                   "source_url": r["urls"][0], "ocr_text": r.get("ocr", ""),
                                   "ocr_status": "done", "ocr_attempts": 1, "video_id": None,
                                   "timestamp": None, "point_id": r["image_id"]})
            for f in frames:
                sha = frame_sha[f["frame_id"]]
                key = indexer.media_key_for(sha, "png")
                self._put(key, a / f["path"])
                media_rows.append({"sha256": sha, "kind": "video_frame", "s3_key": key,
                                   "source_url": f["url"],
                                   "ocr_text": ocr_by_key.get(f"frame:{f['frame_id']}", ""),
                                   "ocr_status": "done", "ocr_attempts": 1,
                                   "video_id": vid_pk.get(f["video_key"]),
                                   "timestamp": f["timestamp"], "point_id": f["frame_id"],
                                   "frame_sets": [q.collection_image]})
            # sha trùng (cùng khung hình ở 2 video) → 1 dòng media; giữ dòng cuối
            media_rows = list({r["sha256"]: r for r in media_rows}.values())
            mpk = await indexer.upsert_media(s, media_rows)
            self.log(f"media: {len(mpk)} (ảnh {len(images)}, frame {len(frames)}), "
                     f"upload mới {self.stats.get('uploaded', 0)}")
            ctx = self._contexts(docs)
            occ = [{"document_id": pk[pg["doc_id"]], "media_id": mpk[r["sha256"]],
                    "alt": pg.get("alt", ""), "pos": pg.get("pos", 0),
                    "block_idx": pg.get("block"), "heading_path": pg.get("path", []),
                    "context": next((ctx[(pg["doc_id"], u)] for u in r["urls"] if (pg["doc_id"], u) in ctx), ""),
                    "caption_point_id": stable_id("caption", "image", r["sha256"], pg["doc_id"])}
                   for r in images for pg in r["pages"] if pg["doc_id"] in pk]
            occ = list({(o["document_id"], o["media_id"]): o for o in occ}.values())
            await indexer.add_occurrences(s, MediaOccurrence, occ, ("document_id", "media_id"))
            struct = {d["doc_id"]: ix.block_index(d.get("structure")) for d in docs}   # 1 lần / bài
            vurl = {v["video_key"]: v["url"] for v in videos}
            vocc = [{"document_id": pk[c["doc_id"]], "video_id": vid_pk[c["media_ref"]],
                     "caption_point_id": c["caption_id"],
                     "block_idx": struct[c["doc_id"]].get(vurl.get(c["media_ref"], ""), (None,))[0]}
                    for c in caps_v if c["doc_id"] in pk and c["media_ref"] in vid_pk]
            await indexer.add_occurrences(s, VideoOccurrence, vocc, ("document_id", "video_id"))
            await s.commit()
        self.log(f"occurrence: ảnh {len(occ)}, video {len(vocc)}")

        # ---- Qdrant chữ: chunk → text, caption → ocr (cùng collection khi không tách; vector có sẵn, chỉ thêm sparse)
        img_by_sha = {r["sha256"]: r for r in images}
        vid_by_key = {v["video_key"]: v for v in videos}
        frames_by_video: dict[str, list[dict]] = {}
        for f in frames:
            frames_by_video.setdefault(f["video_key"], []).append(f)
        pts = [vector_store.text_point(c["chunk_id"], v, c["text"],
                                       indexer.chunk_payload(c, pk[c["doc_id"]]))
               for c, v in zip(chunks, v_chunks)]
        vector_store.upsert_points(q.collection_text, pts)
        n_chunk = len(pts)
        pts = []
        for c, v in zip(caps_i, v_caps_i):
            r = img_by_sha[c["sha256"]]
            pts.append(vector_store.text_point(c["caption_id"], v, c["text"], indexer.caption_payload(
                c, pk[c["doc_id"]], r["urls"][0], c["sha256"])))
        for c, v in zip(caps_v, v_caps_v):
            vid = vid_by_key.get(c["media_ref"], {})
            fr = sorted(frames_by_video.get(c["media_ref"], []), key=lambda f: f["timestamp"])
            pts.append(vector_store.text_point(c["caption_id"], v, c["text"], indexer.caption_payload(
                c, pk[c["doc_id"]], vid.get("url", ""), frame_sha[fr[0]["frame_id"]] if fr else None,
                c["media_ref"])))
        vector_store.upsert_points(vector_store.ocr_collection(), pts)
        n_cap = len(pts)
        self.log(f"text points: chunk {n_chunk} → {q.collection_text}, caption {n_cap} "
                 f"→ {vector_store.ocr_collection()}")

        # ---- Qdrant image: ảnh + frame
        from qdrant_client.models import PointStruct
        pts = []
        for r, v in zip(images, v_images):
            pages = [(pk[p["doc_id"]], doc_by_key[p["doc_id"]]) for p in r["pages"] if p["doc_id"] in pk]
            alt = next((p.get("alt", "") for p in r["pages"] if p.get("alt")), "")
            key = indexer.media_key_for(r["sha256"], Path(r["path"]).suffix.lstrip(".") or "jpg")
            pts.append(PointStruct(id=r["image_id"], vector=v.tolist(), payload=indexer.media_payload(
                "image", r["sha256"], key, r["urls"][0], pages, indexer.image_caption(alt, r.get("ocr", "")),
                urls=r["urls"])))
        pages_by_video: dict[str, list[tuple[int, dict]]] = {}
        for c in caps_v:
            if c["doc_id"] in pk:
                pages_by_video.setdefault(c["media_ref"], []).append((pk[c["doc_id"]], doc_by_key[c["doc_id"]]))
        for f, v in zip(frames, v_frames):
            sha = frame_sha[f["frame_id"]]
            title = vid_by_key.get(f["video_key"], {}).get("title", "")
            pts.append(PointStruct(id=f["frame_id"], vector=v.tolist(), payload=indexer.media_payload(
                "video_frame", sha, indexer.media_key_for(sha, "png"), f["url"],
                pages_by_video.get(f["video_key"], []), title, timestamp=f["timestamp"],
                video_key=f["video_key"])))
        vector_store.upsert_points(q.collection_image, pts)
        n_img = len(pts)
        self.log(f"image points: {n_img} (ảnh {len(images)}, frame {len(frames)})")

        # ---- graph (D20): dựng từ PG + Qdrant vừa ghi (cùng đường code với incremental/hằng đêm),
        # payload `entities` sinh từ PG
        from . import graphsync
        async with db.get_session() as s:
            g = await graphsync.rebuild(s)
        self.stats.update({k: g[k] for k in ("entities", "entity_links", "entity_relations",
                                             "review_queued", "staff")})
        self.log(f"graph: {json.dumps(g, ensure_ascii=False)}")

        # ---- kiểm tra
        want = {q.collection_text: n_chunk, q.collection_image: n_img}
        want[vector_store.ocr_collection()] = want.get(vector_store.ocr_collection(), 0) + n_cap
        got = {name: client.count(name, exact=True).count for name in want}
        ok = got == want
        self.stats.update({"documents": len(pk), "points": got, "expected": want, "ok": ok})
        if any(got[k] > want[k] for k in want):
            self.log("cảnh báo: collection có point ngoài artifact (nạp chồng snapshot cũ?) — "
                     "chạy lại với --recreate để sạch")
        self.log(json.dumps(self.stats, ensure_ascii=False))
        return self.stats


async def tag_frames(art: Path) -> dict:
    """Chỉ gắn bộ khung của artifact (collection ảnh đích) vào media.frame_sets — DB đã nạp trước khi có cột này.
    Không ghi Qdrant, không upload."""
    ld = Loader(art, None, False, False)
    ld.check()
    tag = ld.cfg.qdrant.collection_image
    shas = [hashlib.sha256((art / f["path"]).read_bytes()).hexdigest() for f in _jsonl(art / "frames.jsonl")]
    await db.init_db()
    async with db.get_session() as s:
        n = await indexer.tag_frame_set(s, shas, tag)
        await s.commit()
    stats = {"tag": tag, "frames": len(shas), "tagged": n, "ok": True}
    ld.log(json.dumps(stats, ensure_ascii=False))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Nạp artifact compute → PG + Qdrant *_v2 + S3")
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--snapshot", default=None, help="mặc định lấy từ manifest.json")
    ap.add_argument("--recreate", action="store_true", help="xoá & tạo lại collection *_v2 trước")
    ap.add_argument("--skip-media-upload", action="store_true",
                    help="không đưa file media lên store (kể cả local)")
    ap.add_argument("--upload-media", action="store_true",
                    help="cho phép upload file media lên S3 (store S3). Không có cờ này → không upload")
    ap.add_argument("--media-only", action="store_true",
                    help="chỉ upload file media (không ghi PG/Qdrant) — dùng với --upload-media")
    ap.add_argument("--tag-frames", action="store_true",
                    help="chỉ gắn bộ khung của artifact vào media.frame_sets (không ghi Qdrant, không upload)")
    args = ap.parse_args()
    if args.tag_frames:
        raise SystemExit(0 if asyncio.run(tag_frames(Path(args.artifacts)))["ok"] else 1)
    ld = Loader(Path(args.artifacts), Path(args.snapshot) if args.snapshot else None,
                not args.skip_media_upload, args.upload_media)
    stats = ld.upload_media_only() if args.media_only else asyncio.run(ld.run(args.recreate))
    raise SystemExit(0 if stats.get("ok") else 1)


if __name__ == "__main__":
    main()
