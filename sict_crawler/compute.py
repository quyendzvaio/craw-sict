"""Compute nặng từ snapshot → artifact (chạy trên máy GPU; CPU vẫn chạy được, chỉ chậm hơn).

snapshot (snapshot.py) → parse (index spec v2, indexing.py) → chunk → embed E5 → ảnh CLIP + OCR
→ caption E5 → video keyframe + phụ đề → artifact. Artifact KHÔNG phụ thuộc kho vector: load vào
Qdrant (collection `*_v2`, Giai đoạn 3) không phải embed lại. Id cố định (uuid5) = point id.

Stages (chạy riêng được, stage đã có output thì bỏ qua trừ khi --force):
  docs    pages.jsonl + files.jsonl → documents.jsonl + chunks.jsonl (lọc listing, trang lỗi,
          trang fallback, tài liệu trùng nội dung, chunk boilerplate xuyên > N bài)
  text    chunks.jsonl → chunks.npy (E5 passage)
  images  ảnh không phải trang trí (xuất hiện ≤ N bài) → images.jsonl/.npy (CLIP) + OCR
          → captions_image.jsonl/.npy (E5, mỗi lần xuất hiện 1 caption riêng: AC-B4)
  videos  mỗi video 1 lần theo YouTube id → frames.jsonl/.npy (CLIP) + captions_video.jsonl/.npy
Output: <out>/manifest.json ghi model, dim, device, số lượng, thời gian từng stage.

Usage (máy GPU: image worker bản GPU, EMBED_DEVICE=auto):
  python -m sict_crawler.compute --snapshot _snapshot/2026-10-12
  python -m sict_crawler.compute --snapshot S --stages docs,text --text-model intfloat/multilingual-e5-base
Callers: CLI.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import indexing as ix
from . import parser
from .config import get_settings

STAGES = ("docs", "text", "images", "videos")
NS = uuid.UUID("6f1c2a8e-5b3d-4e7a-9c1f-2d8b4a6e0c35")  # namespace id cố định cho artifact


def stable_id(kind: str, *parts) -> str:
    return str(uuid.uuid5(NS, ":".join([kind, *map(str, parts)])))


def _jsonl_read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _jsonl_write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _npy_write(path: Path, vecs) -> None:
    arr = np.asarray(vecs, dtype=np.float32)
    tmp = path.with_name(path.stem + ".tmp.npy")
    np.save(tmp, arr)
    tmp.replace(path)


def _header(d: dict) -> str:
    return ix.DocMeta(**{k: d[k] for k in ix.DocMeta.__dataclass_fields__}).header()


def _attachment_title(desc: str, url: str) -> str:
    d = (desc or "").strip(" -:•–")
    for junk in ("Tải về tại đây.", "Tải về tại đây", "tại đây", "Tải về", "Download"):
        d = d.replace(junk, "")
    d = d.strip(" -:•.–")
    return d[:200] or Path(url).name


# ---------- quy tắc dùng chung bulk (Compute) + incremental (indexer.index_page) ----------
def page_doc(url: str, html: str, final_url: str | None = None, include_fallback: bool = False):
    """HTML 1 trang → (status, doc, markdown sạch, parsed). status: ok | listing | error_page |
    content_fallback. doc = metadata spec v2 + content_hash (sha1 markdown sạch — không đổi theo
    bộ đếm lượt xem như hash HTML thô) + ảnh/video vùng nội dung."""
    from .snapshot import is_error_page, page_type
    if page_type(url) == "listing":
        return "listing", None, "", None
    p = parser.parse_html(final_url or url, html)
    if is_error_page(p.markdown):
        return "error_page", None, "", p
    if p.content_fallback and not include_fallback:
        return "content_fallback", None, "", p
    meta = ix.build_meta(url, p.title, p.published_text, p.section_label)
    md = ix.clean_markdown(p.markdown)
    doc = {**meta.to_dict(), "source": "page", "content_hash": hashlib.sha1(md.encode()).hexdigest(),
           "images": [{"url": u, "alt": a, "context": c}
                      for u, a, c in zip(p.image_urls, p.image_alts, p.image_contexts)],
           "videos": p.video_urls}
    if p.blocks:                                    # D23: cấu trúc trong trang (không đụng content_hash)
        doc["structure"] = ix.outline(p.blocks)
        doc["hash_struct"] = ix.hash_struct(doc["structure"])
    return "ok", doc, md, p


def _block_ref(blocks_at: dict, media_url: str) -> dict:
    """Vị trí media trong bài (D23) từ `ix.block_index(doc["structure"])`: {"block": idx, "path": heading_path};
    rỗng nếu bài chưa có cấu trúc."""
    hit = blocks_at.get(media_url)
    return {"block": hit[0], "path": hit[1]} if hit else {}


def attachment_doc(file_url: str, desc: str, parent: dict) -> dict:
    """File đính kèm → doc (metadata thời gian/mục thừa kế trang chứa nó)."""
    meta = ix.build_meta(file_url, _attachment_title(desc, file_url),
                         doc_type="attachment", parent_url=parent["url"])
    meta.published_at, meta.year = parent["published_at"], parent["year"]
    meta.section, meta.section_label = parent["section"], parent["section_label"]
    return {**meta.to_dict(), "source": "file", "parent_title": parent["title"]}


def doc_chunks(doc: dict, text: str, semantic: bool = True, blocks: list[dict] | None = None,
               pages: list[tuple[int | None, str]] | None = None) -> list[ix.ChunkRec]:
    """Chunk của một doc. chunking.structured (index v3): có blocks → chunk theo cấu trúc (D23); file có
    trang thật (`pages` từ ix.extract_file_pages) → chunk theo trang (D24). Không thì chunk theo markdown
    như spec v2."""
    ch = get_settings().chunking
    meta = ix.DocMeta(**{k: doc[k] for k in ix.DocMeta.__dataclass_fields__})
    if ch.structured and blocks:
        return ix.build_chunks_structured(meta, blocks, ch.min_tokens, ch.max_tokens, ch.overlap_ratio)
    if ch.structured and pages and any(n is not None for n, _ in pages):
        return ix.build_chunks_paged(meta, pages, ch.min_tokens, ch.max_tokens, ch.overlap_ratio, semantic)
    return ix.build_chunks(meta, text, ch.min_tokens, ch.max_tokens, ch.overlap_ratio, semantic)


def chunk_row(c: ix.ChunkRec) -> dict:
    row = {"chunk_id": stable_id("chunk", c.meta["doc_id"], c.idx), "idx": c.idx, "kind": c.kind,
           "heading": c.heading, "text": c.text, "body_hash": c.body_hash, **c.meta}
    if c.block_range is not None:                   # D23: chỉ có khi chunk dựng từ block
        row["heading_path"], row["block_range"] = c.heading_path, list(c.block_range)
    if c.page is not None:                          # D24: trang PDF/PPTX
        row["page"] = c.page
    return row


def image_caption_text(doc: dict, alt: str, ocr: str, path: list[str] | None = None) -> str:
    """Caption ảnh = header + (mục chứa ảnh, khi chunking.structured) + alt + OCR."""
    where = ix.path_str(path) if path and get_settings().chunking.structured else ""
    return ". ".join(x for x in (_header(doc), where, alt, ocr) if x and x.strip())[:900]


def video_caption_text(doc: dict, info: dict, ocr_join: str) -> str:
    parts = [_header(doc), f"Video: {info.get('title', '')}" if info.get("title") else "",
             (info.get("subs") or "")[:1500], ocr_join]
    return ". ".join(p for p in parts if p)[:2000]


def frames_ocr_join(texts: list[str]) -> str:
    return " | ".join(dict.fromkeys(t for t in texts if len(t) >= 10))[:400]


def video_key(url: str) -> str:
    from .snapshot import youtube_id
    return youtube_id(url) or hashlib.sha1(url.encode()).hexdigest()[:16]


class Compute:
    def __init__(self, snapshot: Path, out: Path, boilerplate_docs: int = 20,
                 semantic: bool = True, include_fallback: bool = False, limit_docs: int | None = None):
        self.snap = snapshot
        self.out = out
        self.boilerplate_docs = boilerplate_docs
        self.semantic = semantic
        self.include_fallback = include_fallback
        self.limit_docs = limit_docs
        out.mkdir(parents=True, exist_ok=True)
        self.cfg = get_settings()
        mpath = out / "manifest.json"
        self.manifest = json.loads(mpath.read_text()) if mpath.exists() else {
            "spec_version": ix.SPEC_VERSION, "snapshot": str(snapshot), "stages": {}}

    def log(self, msg: str) -> None:
        print(f"[compute {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def _save_manifest(self) -> None:
        from . import embedder
        emb = self.cfg.embedding
        self.manifest.update({
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "text_model": emb.model if emb.backend == "api" else emb.text_model,
            "image_model": emb.model if emb.backend == "api" else emb.image_model,
            "device": "api" if emb.backend == "api" else (embedder._device or emb.device),
            "boilerplate_docs": self.boilerplate_docs, "semantic_chunking": self.semantic,
            "structured_chunks": bool(self.cfg.chunking.structured)})
        if emb.backend == "api":   # D22: loader so khớp với encoder câu hỏi (cùng model, số chiều, tiền tố query)
            self.manifest.update({"backend": "api", "dim": emb.dim, "query_prefix": emb.query_prefix})
        (self.out / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2))

    def _batch(self) -> int:
        from . import embedder
        if self.cfg.embedding.backend == "api":   # đủ lớn để các luồng gọi song song đều có việc
            return self.cfg.embedding.api_batch * max(1, self.cfg.embedding.api_workers)
        return 128 if embedder.get_device().startswith("cuda") else self.cfg.embedding.batch_size

    # ---------------- docs ----------------
    def stage_docs(self) -> dict:
        pages = _jsonl_read(self.snap / "pages.jsonl")
        docs, chunks = [], []
        skipped = Counter()
        seen_content: dict[str, str] = {}
        for pg in pages:
            if self.limit_docs and len(docs) >= self.limit_docs:
                break
            if pg.get("page_type") == "listing":
                skipped["listing"] += 1
                continue
            if pg.get("error_page"):
                skipped["error_page"] += 1
                continue
            if pg.get("content_fallback") and not self.include_fallback:
                skipped["content_fallback"] += 1
                continue
            try:
                html = gzip.decompress((self.snap / pg["html_path"]).read_bytes())
            except OSError:
                skipped["html_missing"] += 1
                continue
            url = pg["url"]
            status, doc, md, parsed = page_doc(url, html.decode("utf-8", "ignore"), pg.get("final_url"),
                                          self.include_fallback)
            if status != "ok":
                skipped[status] += 1
                continue
            if doc["content_hash"] in seen_content:  # cùng bài đăng ở nhiều mục/URL → giữ bản đầu (AC-R6)
                skipped["duplicate_content"] += 1
                continue
            seen_content[doc["content_hash"]] = url
            # media lấy từ lần parse này (parser mới nhất), không dùng danh sách snapshot ghi lúc crawl
            docs.append({**doc, "html_path": pg["html_path"]})
            chunks.extend(doc_chunks(doc, md, self.semantic, parsed.blocks))
        page_meta = {d["url"]: d for d in docs}

        seen_files: set[str] = set()
        for f in _jsonl_read(self.snap / "files.jsonl"):
            if f["sha256"] in seen_files:  # cùng file, nhiều URL
                skipped["file_duplicate"] += 1
                continue
            seen_files.add(f["sha256"])
            parent = next((pg for pg in f.get("pages", []) if pg["url"] in page_meta), None)
            if parent is None:
                skipped["file_orphan"] += 1
                continue
            pages, status = ix.extract_file_pages(self.snap / f["path"], ocr_scanned=True)
            if status == "ok_ocr":
                skipped["file_ocr_scanned_pdf"] += 1  # đếm (không bỏ): PDF scan đã OCR
            elif status != "ok":
                skipped[f"file_{status.split(':')[0]}"] += 1
                continue
            d = {**attachment_doc(f["url"], parent.get("desc", ""), page_meta[parent["url"]]),
                 "file_path": f["path"]}
            docs.append(d)
            chunks.extend(doc_chunks(d, "\n\n".join(t for _, t in pages), self.semantic, pages=pages))

        # boilerplate: cùng thân chunk xuất hiện ở > N tài liệu (AC-H7)
        per_hash = defaultdict(set)
        for c in chunks:
            per_hash[c.body_hash].add(c.meta["doc_id"])
        bp = {h for h, ds in per_hash.items() if len(ds) > self.boilerplate_docs}
        rows = []
        for c in chunks:
            if c.body_hash in bp:
                skipped["boilerplate_chunk"] += 1
                continue
            rows.append(chunk_row(c))
        n_by_doc = Counter(r["doc_id"] for r in rows)
        for d in docs:
            d["n_chunks"] = n_by_doc.get(d["doc_id"], 0)
        _jsonl_write(self.out / "documents.jsonl", docs)
        _jsonl_write(self.out / "chunks.jsonl", rows)
        return {"documents": len(docs), "chunks": len(rows),
                "attachments": sum(d["source"] == "file" for d in docs), "skipped": dict(skipped)}

    # ---------------- text ----------------
    def stage_text(self) -> dict:
        from . import embedder
        rows = _jsonl_read(self.out / "chunks.jsonl")
        bs = self._batch()
        vecs = []
        for i in range(0, len(rows), bs):
            vecs.extend(embedder.embed_texts([r["text"] for r in rows[i:i + bs]]))
            if (i // bs) % 20 == 0:
                self.log(f"text {min(i + bs, len(rows))}/{len(rows)}")
        _npy_write(self.out / "chunks.npy", vecs)
        return {"vectors": len(vecs), "dim": len(vecs[0]) if vecs else 0}

    # ---------------- images ----------------
    def _ocr_cached(self, items: list[tuple[str, bytes]]) -> dict[str, str]:
        """OCR song song (tesseract là subprocess → thread đủ), cache theo sha để resume."""
        from . import ocr
        cache_path = self.out / "ocr_cache.jsonl"
        cache = {r["key"]: r["text"] for r in _jsonl_read(cache_path)}
        todo = [(k, b) for k, b in items if k not in cache]
        if todo:
            workers = max(1, (os.cpu_count() or 2) - 1)
            with ThreadPoolExecutor(workers) as pool, open(cache_path, "a", encoding="utf-8") as f:
                for n, (k, text) in enumerate(zip([k for k, _ in todo],
                                                  pool.map(lambda kb: ocr.ocr_text(kb[1]), todo))):
                    cache[k] = text
                    f.write(json.dumps({"key": k, "text": text}, ensure_ascii=False) + "\n")
                    if n % 200 == 0:
                        f.flush()
                        self.log(f"ocr {n}/{len(todo)}")
        return cache

    def stage_images(self) -> dict:
        from . import embedder
        docs = {d["url"]: d for d in _jsonl_read(self.out / "documents.jsonl") if d["source"] == "page"}
        images = {r["url"]: r for r in _jsonl_read(self.snap / "images.jsonl")}
        occ = defaultdict(list)  # sha → [(doc, alt, pos, image rec)]
        blocks_at = {d["doc_id"]: ix.block_index(d.get("structure")) for d in docs.values()}   # 1 lần / bài
        for d in docs.values():
            for pos, im in enumerate(d.get("images", [])):
                rec = images.get(im["url"])
                if rec:
                    occ[rec["sha256"]].append((d, im.get("alt", ""), pos, rec))
        decor = {sha for sha, os_ in occ.items()
                 if len({d["doc_id"] for d, *_ in os_}) > self.boilerplate_docs}
        keep = [sha for sha in occ if sha not in decor]
        bs = self._batch()
        img_rows, img_vecs = [], []
        for i in range(0, len(keep), bs):
            part = keep[i:i + bs]
            data = [(self.snap / occ[s][0][3]["path"]).read_bytes() for s in part]
            vecs, ok = embedder.embed_image_bytes(data)
            for j, v in zip(ok, vecs):
                sha = part[j]
                img_rows.append({"image_id": stable_id("image", sha), "sha256": sha,
                                 "path": occ[sha][0][3]["path"],
                                 "urls": sorted({o[3]["url"] for o in occ[sha]}),
                                 "pages": [{"doc_id": d["doc_id"], "url": d["url"], "alt": alt, "pos": pos,
                                            **_block_ref(blocks_at[d["doc_id"]], rec["url"])}
                                           for d, alt, pos, rec in occ[sha]]})
                img_vecs.append(v)
            if (i // bs) % 10 == 0:
                self.log(f"clip {min(i + bs, len(keep))}/{len(keep)}")
        ocr_text = self._ocr_cached([(r["sha256"], (self.snap / r["path"]).read_bytes())
                                     for r in img_rows])
        cap_rows, seen_caps = [], set()
        for r in img_rows:
            r["ocr"] = ocr_text.get(r["sha256"], "")
            for pg in r["pages"]:
                d = docs[pg["url"]]
                cid = stable_id("caption", "image", r["sha256"], d["doc_id"])
                if cid in seen_caps:  # cùng ảnh lặp trong 1 bài → 1 caption
                    continue
                seen_caps.add(cid)
                text = image_caption_text(d, pg["alt"], r["ocr"], pg.get("path"))
                cap_rows.append({"caption_id": cid,
                                 "media_kind": "image", "media_ref": r["image_id"], "sha256": r["sha256"],
                                 "doc_id": d["doc_id"], "url": d["url"], "title": d["title"],
                                 "section": d["section"], "doc_type": d["doc_type"],
                                 "published_at": d["published_at"], "year": d["year"],
                                 "text": text, "timestamp": None,
                                 "heading_path": pg.get("path", []), "block": pg.get("block")})
        cap_vecs = []
        for i in range(0, len(cap_rows), bs):
            cap_vecs.extend(embedder.embed_texts([c["text"] for c in cap_rows[i:i + bs]]))
        _jsonl_write(self.out / "images.jsonl", img_rows)
        _npy_write(self.out / "images.npy", img_vecs)
        _jsonl_write(self.out / "captions_image.jsonl", cap_rows)
        _npy_write(self.out / "captions_image.npy", cap_vecs)
        return {"unique_images": len(occ), "decorative_skipped": len(decor),
                "embedded": len(img_rows), "captions": len(cap_rows)}

    # ---------------- videos ----------------
    def stage_videos(self) -> dict:
        from . import embedder, video
        docs = {d["url"]: d for d in _jsonl_read(self.out / "documents.jsonl") if d["source"] == "page"}
        frames_dir = self.out / "frames"
        frame_rows, frame_vecs, cap_rows, vid_rows = [], [], [], []
        stats = Counter()
        from .snapshot import youtube_id
        by_video: dict[str, dict] = {}
        for d in docs.values():
            for vurl in d.get("videos", []):
                k = youtube_id(vurl) or vurl
                by_video.setdefault(k, {"url": vurl, "video_id": youtube_id(vurl), "pages": []})
                if d["url"] not in by_video[k]["pages"]:
                    by_video[k]["pages"].append(d["url"])
        for v in by_video.values():
            pages = [docs[u] for u in v["pages"] if u in docs]
            if not pages:
                stats["no_indexed_page"] += 1
                continue
            if len({d["doc_id"] for d in pages}) > self.boilerplate_docs:
                stats["decorative"] += 1
                continue
            key = video_key(v["url"])
            frames = video.extract_keyframes(v["url"])
            info = video.fetch_video_info(v["url"])
            if not frames:
                stats["blocked_or_failed"] += 1  # AC-D4: bỏ qua êm, bài vẫn index bằng chữ
            kept = []
            if frames:
                vecs, ok = embedder.embed_image_bytes([b for b, _ in frames])
                order = video.dedup_indices(vecs, self.cfg.video.dedup_threshold)
                for k in order:
                    b, ts = frames[ok[k]]
                    fid = stable_id("frame", key, f"{ts:.2f}")
                    rel = f"frames/{key}/{ts:08.2f}.png"
                    (frames_dir / key).mkdir(parents=True, exist_ok=True)
                    (self.out / rel).write_bytes(b)
                    kept.append((fid, ts, rel, b))
                    frame_rows.append({"frame_id": fid, "video_key": key, "url": v["url"],
                                       "timestamp": ts, "path": rel})
                    frame_vecs.append(vecs[k])
            ocr_text = self._ocr_cached([(f"frame:{fid}", b) for fid, _, _, b in kept])
            ocr_join = frames_ocr_join([ocr_text.get(f"frame:{fid}", "") for fid, *_ in kept])
            for d in pages:  # mỗi bài nhúng video có 1 caption (AC-R3: 1 video = 1 điểm)
                cap_rows.append({"caption_id": stable_id("caption", "video", key, d["doc_id"]),
                                 "media_kind": "video", "media_ref": key,
                                 "frame_ids": [fid for fid, *_ in kept],
                                 "doc_id": d["doc_id"], "url": d["url"], "title": d["title"],
                                 "section": d["section"], "doc_type": d["doc_type"],
                                 "published_at": d["published_at"], "year": d["year"],
                                 "text": video_caption_text(d, info, ocr_join),
                                 "timestamp": kept[0][1] if kept else None})
            vid_rows.append({"video_key": key, "url": v["url"], "title": info.get("title", ""),
                             "subs_chars": len(info.get("subs", "")), "frames": len(kept),
                             "pages": [d["url"] for d in pages]})
            stats["processed"] += 1
        bs = self._batch()
        cap_vecs = []
        for i in range(0, len(cap_rows), bs):
            cap_vecs.extend(embedder.embed_texts([c["text"] for c in cap_rows[i:i + bs]]))
        _jsonl_write(self.out / "videos.jsonl", vid_rows)
        _jsonl_write(self.out / "frames.jsonl", frame_rows)
        _npy_write(self.out / "frames.npy", frame_vecs)
        _jsonl_write(self.out / "captions_video.jsonl", cap_rows)
        _npy_write(self.out / "captions_video.npy", cap_vecs)
        return {**stats, "frames": len(frame_rows), "captions": len(cap_rows)}

    # ---------------- driver ----------------
    _OUTPUTS = {"docs": "chunks.jsonl", "text": "chunks.npy", "images": "images.npy",
                "videos": "frames.npy"}

    def run(self, stages: list[str], force: bool = False) -> dict:
        from . import embedder
        self.log(f"backend={self.cfg.embedding.backend} "
                 f"device={'api' if self.cfg.embedding.backend == 'api' else embedder.get_device()} stages={stages}")
        for st in stages:
            if not force and (self.out / self._OUTPUTS[st]).exists() and st in self.manifest["stages"]:
                self.log(f"skip {st} (đã có output, dùng --force để chạy lại)")
                continue
            t0 = time.time()
            self.log(f"stage {st} ...")
            res = getattr(self, f"stage_{st}")()
            res["seconds"] = round(time.time() - t0, 1)
            self.manifest["stages"][st] = res
            self._save_manifest()
            self.log(f"stage {st} done: {json.dumps(res, ensure_ascii=False)}")
        return self.manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute nặng từ snapshot → artifact")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", default=None, help="mặc định <snapshot>/artifacts/<spec>-<model>")
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--text-model", default=None, help="ghi đè embedding.text_model (đánh giá model)")
    ap.add_argument("--boilerplate-docs", type=int, default=20)
    ap.add_argument("--no-semantic", action="store_true", help="chunk cửa sổ thay vì semantic")
    ap.add_argument("--include-fallback", action="store_true")
    ap.add_argument("--limit-docs", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cfg = get_settings()
    if args.text_model:
        cfg.embedding.text_model = args.text_model
    snap = Path(args.snapshot)
    out = Path(args.out) if args.out else (
        snap / "artifacts" / f"{ix.SPEC_VERSION}-{cfg.embedding.text_model.rsplit('/', 1)[-1]}")
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        ap.error(f"stage không hợp lệ: {bad}")
    Compute(snap, out, args.boilerplate_docs, not args.no_semantic, args.include_fallback,
            args.limit_docs).run(stages, args.force)


if __name__ == "__main__":
    main()
