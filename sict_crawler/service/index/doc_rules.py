"""Quy tắc dựng tài liệu / chunk / caption của index spec v2 — một nơi duy nhất cho bulk
(pipeline.compute) và incremental (indexer.index_page), id ổn định uuid5 = point id."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from sict_crawler.core.config import get_settings
from sict_crawler.service.crawl import parser
from sict_crawler.service.crawl.urls import is_error_page, page_type, youtube_id
from sict_crawler.service.index import indexing as ix

NS = uuid.UUID("6f1c2a8e-5b3d-4e7a-9c1f-2d8b4a6e0c35")  # namespace id cố định cho artifact


def stable_id(kind: str, *parts) -> str:
    return str(uuid.uuid5(NS, ":".join([kind, *map(str, parts)])))


def _header(d: dict) -> str:
    return ix.DocMeta(**{k: d[k] for k in ix.DocMeta.__dataclass_fields__}).header()


def _attachment_title(desc: str, url: str) -> str:
    d = (desc or "").strip(" -:•–")
    for junk in ("Tải về tại đây.", "Tải về tại đây", "tại đây", "Tải về", "Download"):
        d = d.replace(junk, "")
    d = d.strip(" -:•.–")
    return d[:200] or Path(url).name


def page_doc(url: str, html: str, final_url: str | None = None, include_fallback: bool = False):
    """HTML 1 trang → (status, doc, markdown sạch, parsed). status: ok | listing | error_page |
    content_fallback. doc = metadata spec v2 + content_hash (sha1 markdown sạch — không đổi theo
    bộ đếm lượt xem như hash HTML thô) + ảnh/video vùng nội dung."""
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
    return youtube_id(url) or hashlib.sha1(url.encode()).hexdigest()[:16]
