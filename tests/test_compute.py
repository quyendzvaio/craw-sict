"""Compute snapshot → artifact với embedder/OCR/video giả (không GPU, không mạng).

Run: python -m tests.test_compute  |  pytest tests/test_compute.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.core.config import get_settings
from sict_crawler.pipeline import compute
from sict_crawler.pipeline import snapshot as snap
from sict_crawler.service.embedding import embedder
from sict_crawler.service.index import ocr, video
from tests.test_snapshot import ARTICLE, FIX, REGULATION, _handler


def _minimal_pdf(text: str) -> bytes:
    """PDF 1 trang, font Helvetica, chữ ASCII — đủ để pdftotext trích được."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _vec(key: str, dim: int) -> list[float]:
    v = np.frombuffer(hashlib.sha256(key.encode()).digest()[:dim], dtype=np.uint8).astype(np.float32) + 1
    return (v / np.linalg.norm(v)).tolist()


def _fakes():
    saved = (embedder.embed_texts, embedder.embed_image_bytes, embedder.get_device,
             ocr.ocr_text, video.extract_keyframes, video.fetch_video_info)
    embedder.embed_texts = lambda texts: [_vec(t, 8) for t in texts]
    embedder.embed_image_bytes = lambda imgs: ([_vec(str(len(b)) + str(b[:16]), 4) for b in imgs],
                                               list(range(len(imgs))))
    embedder.get_device = lambda: "cpu"
    ocr.ocr_text = lambda b, max_len=300: "HOI CHO TRIEN LAM HANOI MIP FAIR 2026"
    video.extract_keyframes = lambda url, max_frames=None: [(b"frame-a" * 50, 0.0),
                                                            (b"frame-b" * 50, 5.0)]
    video.fetch_video_info = lambda url: {"title": "Video giới thiệu SICT", "subs": "xin chào các bạn",
                                          "duration": 10, "uploader": ""}
    return saved


def _restore(saved):
    (embedder.embed_texts, embedder.embed_image_bytes, embedder.get_device,
     ocr.ocr_text, video.extract_keyframes, video.fetch_video_info) = saved


def _make_snapshot(out: Path) -> None:
    pdf = _minimal_pdf("Quy che dao tao dai hoc theo hoc che tin chi nam 2026 cho sinh vien chinh quy")
    base = _handler([])
    # bài có video nhúng TRONG nội dung (fixture gốc chỉ có video sidebar, đã bị loại)
    article = (FIX / "sict_article.html").read_text(encoding="utf-8").replace(
        '<div class="irs-blog-single-col">',
        '<div class="irs-blog-single-col"><iframe src="https://www.youtube.com/embed/abcdefghijk"></iframe>', 1)

    def h(req):
        if str(req.url).endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        if str(req.url) == ARTICLE:
            return httpx.Response(200, content=article.encode(), headers={"content-type": "text/html"})
        return base(req)

    seeds = get_settings().site.seed_urls
    old = list(seeds)
    seeds[:] = ["https://sict.haui.edu.vn/vn/tin-tuc"]
    try:
        s = snap.Snapshot(out, delay=0.0, max_pages=None, transport=httpx.MockTransport(h))
        s.db.execute("INSERT INTO pages(url, depth) VALUES(?, 1)", (ARTICLE,))
        s.db.execute("INSERT OR IGNORE INTO pages(url, depth) VALUES(?, 1)", (REGULATION,))
        s.db.commit()
        asyncio.run(s.run())
    finally:
        seeds[:] = old


def test_compute_all_stages_and_stable_ids():
    saved = _fakes()
    try:
        with tempfile.TemporaryDirectory() as d:
            sp, out = Path(d) / "snap", Path(d) / "art"
            _make_snapshot(sp)
            c = compute.Compute(sp, out, semantic=False)
            man = c.run(list(compute.STAGES))
            st = man["stages"]

            docs = [json.loads(x) for x in (out / "documents.jsonl").read_text().splitlines()]
            urls = {x["url"]: x for x in docs}
            assert ARTICLE in urls and urls[ARTICLE]["year"] == 2026
            assert REGULATION in urls and urls[REGULATION]["section"] == "quy-che"
            assert not any(x["doc_type"] == "listing" for x in docs)
            assert st["docs"]["skipped"].get("listing", 0) >= 1
            if shutil.which("pdftotext"):
                atts = [x for x in docs if x["doc_type"] == "attachment"]
                assert atts and atts[0]["parent_url"] == REGULATION
                assert atts[0]["section"] == "quy-che"
                assert "Quy chế đào tạo" in atts[0]["title"]

            chunks = [json.loads(x) for x in (out / "chunks.jsonl").read_text().splitlines()]
            vecs = np.load(out / "chunks.npy")
            assert len(chunks) == vecs.shape[0] > 0
            assert len({x["chunk_id"] for x in chunks}) == len(chunks)

            imgs = [json.loads(x) for x in (out / "images.jsonl").read_text().splitlines()]
            caps = [json.loads(x) for x in (out / "captions_image.jsonl").read_text().splitlines()]
            assert imgs and np.load(out / "images.npy").shape[0] == len(imgs)
            assert caps and all("MIP FAIR" in x["text"] for x in caps)
            assert len({x["caption_id"] for x in caps}) == len(caps)

            vcaps = [json.loads(x) for x in (out / "captions_video.jsonl").read_text().splitlines()]
            assert len(vcaps) == 1 and "Video giới thiệu SICT" in vcaps[0]["text"]
            assert st["videos"]["frames"] >= 1

            # chạy lại (--force) → cùng id, cùng thứ tự (load idempotent)
            c2 = compute.Compute(sp, out, semantic=False)
            c2.run(["docs"], force=True)
            again = [json.loads(x)["chunk_id"] for x in (out / "chunks.jsonl").read_text().splitlines()]
            assert again == [x["chunk_id"] for x in chunks]
            # không --force → bỏ qua stage đã xong
            assert c2.run(["text"])["stages"]["text"]["vectors"] == len(chunks)
    finally:
        _restore(saved)


if __name__ == "__main__":
    test_compute_all_stages_and_stable_ids()
    print("OK — compute")
