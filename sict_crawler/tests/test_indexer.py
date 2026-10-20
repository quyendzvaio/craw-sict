"""Index spec v2: sparse BM25 bỏ dấu, payload indexer, collection v2 + gắn/gỡ bài khỏi media dùng
chung (AC-B5), guard v1/v2. Qdrant chạy local mode (":memory:") — không cần service.

Run: pytest sict_crawler/tests/test_indexer.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler import compute as C
from sict_crawler import indexer, router, sparse, vector_store
from sict_crawler.config import get_settings


def test_sparse_accent_insensitive():
    assert sparse.tokens("Học phí ngành CNTT năm 2025") == ["hoc", "phi", "nganh", "cntt", "nam", "2025"]
    a, b = sparse.query_vector("học phí"), sparse.query_vector("hoc phi")
    assert a == b and len(a["indices"]) == 2
    d = sparse.doc_vector("Học phí học phí ngành")
    w = dict(zip(d["indices"], d["values"]))
    hoc = sparse.token_index("hoc")
    assert w[hoc] > w[sparse.token_index("nganh")] > 0   # tf 2 > tf 1, bão hoà theo k1
    assert sparse.doc_vector("!!! ...") == {"indices": [], "values": []}


def _doc(i: int, section: str = "tin-tuc", year: int | None = 2025) -> dict:
    url = f"https://sict.haui.edu.vn/vn/tin-tuc/bai-{i}/{i}"
    return {"doc_id": f"k{i}", "url": url, "title": f"Bài {i}", "section": section,
            "section_label": "Tin tức", "doc_type": "article",
            "published_at": f"{year}-03-0{i}T08:00:00+07:00" if year else None, "year": year,
            "parent_url": None}


def test_media_payload_lists_and_caption_payload():
    p = indexer.media_payload("image", "ab" * 32, "media/x.jpg", "https://sict/a.jpg",
                              [(1, _doc(1)), (2, _doc(2, "thong-bao", 2024)), (1, _doc(1))],
                              caption="alt | ocr")
    assert p["document_id"] == [1, 2]                     # 1 point / file, nhiều bài
    assert p["section"] == ["tin-tuc", "thong-bao"] and p["year"] == [2025, 2024]
    assert p["published_at"].startswith("2025-03-01") or p["published_at"].startswith("2025")
    assert p["media_key"] == "ab" * 32 and p["page_urls"][0].endswith("/1")
    cap = indexer.caption_rec(_doc(3), "image", "cid", "Bài 3. alt")
    cp = indexer.caption_payload(cap, 7, "https://sict/a.jpg", "ab" * 32)
    assert cp["kind"] == "media_caption" and cp["url"] == "https://sict/a.jpg"
    assert cp["page_url"].endswith("/3") and cp["document_id"] == 7


def test_chunk_row_ids_stable():
    """Cùng URL + idx → cùng point id ở bulk và incremental (upsert ghi đè, không nhân bản)."""
    d = _doc(1)
    recs = C.doc_chunks(d, "# Tiêu đề\n\n" + "Nội dung đoạn văn dài. " * 30, semantic=False)
    r1 = [C.chunk_row(c)["chunk_id"] for c in recs]
    r2 = [C.chunk_row(c)["chunk_id"] for c in C.doc_chunks(d, "# Tiêu đề\n\n" + "Nội dung đoạn văn dài. " * 30,
                                                           semantic=False)]
    assert r1 == r2 and r1[0] == C.stable_id("chunk", "k1", 0)
    p = indexer.chunk_payload(C.chunk_row(recs[0]), 42)
    assert p["document_id"] == 42 and p["content"].startswith("[") and p["year"] == 2025


def test_router_gate_multi_doc_media():
    """Point ảnh v2 có document_id là danh sách: coupled nếu BẤT KỲ bài chứa nó đứng đầu."""
    text = [{"document_id": 10}, {"document_id": 11}]
    cands = [{"media_key": "a", "kind": "image", "document_id": [99, 11], "modality": "clip",
              "raw_score": 0.3, "url": "a"},
             {"media_key": "b", "kind": "image", "document_id": [98], "modality": "clip",
              "raw_score": 0.35, "url": "b"}]
    out = router.gate(router.Route("optional"), text, cands, top_k=5, clip_min=0.24, caption_min=0.82)
    assert [h["media_key"] for h in out] == ["a"]
    out = router.gate(router.Route("required"), text, cands, top_k=5, clip_min=0.24, caption_min=0.82)
    assert [h["media_key"] for h in out] == ["a", "b"]   # bài đứng đầu trước


@pytest.fixture()
def mem_qdrant(monkeypatch):
    from qdrant_client import QdrantClient
    q = get_settings().qdrant
    monkeypatch.setattr(q, "collection_text", "t_v2")
    monkeypatch.setattr(q, "collection_image", "i_v2")
    monkeypatch.setattr(q, "text_dim", 4)
    monkeypatch.setattr(q, "image_dim", 4)
    monkeypatch.setattr(vector_store, "_client", QdrantClient(":memory:"))
    vector_store._caps.clear()
    yield vector_store.get_client()
    vector_store._caps.clear()


def _pid(n: int) -> str:
    return str(uuid.UUID(int=n))


def test_v2_collection_sparse_search_and_detach(mem_qdrant):
    from qdrant_client.models import PointStruct
    vector_store.ensure_collections()
    vector_store.require_v2()
    assert vector_store.has_sparse()
    pts = [vector_store.text_point(_pid(1), [1, 0, 0, 0], "Học phí ngành Công nghệ thông tin",
                                   {"kind": "text", "document_id": 1, "url": "u1"}),
           vector_store.text_point(_pid(2), [0, 1, 0, 0], "Lịch thi học kỳ 2",
                                   {"kind": "text", "document_id": 2, "url": "u2"}),
           vector_store.text_point(_pid(3), [0, 0, 1, 0], "Ảnh lễ tốt nghiệp",
                                   {"kind": "media_caption", "document_id": 1, "url": "img"})]
    vector_store.upsert_points("t_v2", pts)
    hits = vector_store.keyword_search("hoc phi cong nghe", ["hoc", "phi"], limit=5)
    assert hits and hits[0]["url"] == "u1"           # không dấu khớp chữ có dấu, có xếp hạng
    img = indexer.media_payload("image", "s" * 64, "k", "img", [(1, _doc(1)), (2, _doc(2))])
    only1 = indexer.media_payload("image", "t" * 64, "k2", "img2", [(1, _doc(1))])
    vector_store.upsert_points("i_v2", [PointStruct(id=_pid(10), vector=[1, 0, 0, 0], payload=img),
                                        PointStruct(id=_pid(11), vector=[0, 1, 0, 0], payload=only1)])
    vector_store.delete_by_document(1)
    left = {p.id: p.payload for p in mem_qdrant.scroll("i_v2", limit=10)[0]}
    assert left == {_pid(10): {**img, "document_id": [2]}}   # ảnh dùng chung còn, ảnh riêng xoá (AC-B5)
    assert mem_qdrant.count("t_v2").count == 1               # chunk + caption của bài 1 đã xoá
    assert vector_store.attach_document([_pid(10), _pid(11)], 5, "u5") == [_pid(10)]
    p = mem_qdrant.retrieve("i_v2", [_pid(10)])[0].payload
    assert p["document_id"] == [2, 5] and "u5" in p["page_urls"]


def test_v3_captions_in_own_ocr_collection(mem_qdrant, monkeypatch):
    """v3: caption (tiêu đề + mục + OCR) nằm ở collection_ocr riêng; tìm kiếm / xoá / ghi bao cả hai."""
    monkeypatch.setattr(get_settings().qdrant, "collection_ocr", "o_v2")
    vector_store.ensure_collections()
    assert vector_store.text_collections() == ["t_v2", "o_v2"] and mem_qdrant.collection_exists("o_v2")
    assert vector_store.has_sparse("o_v2")
    monkeypatch.setattr("sict_crawler.embedder.embed_texts",
                        lambda ts, kind="passage": [[1, 0, 0, 0] if "phí" in t else [0, 1, 0, 0] for t in ts])
    indexer.write_text([(_pid(1), "Học phí ngành CNTT", {"kind": "text", "document_id": 1, "url": "u1"}),
                        (_pid(2), "Ảnh học phí | chữ OCR", {"kind": "media_caption", "document_id": 1, "url": "img"}),
                        (_pid(3), "Lịch thi", {"kind": "text", "document_id": 2, "url": "u2"})])
    assert mem_qdrant.count("t_v2").count == 2 and mem_qdrant.count("o_v2").count == 1
    dense = vector_store.search_text([1, 0, 0, 0], limit=1)               # mỗi collection 1 → chunk + caption
    assert sorted(h["kind"] for h in dense) == ["media_caption", "text"]
    assert [h["url"] for h in vector_store.keyword_search("hoc phi", ["hoc"], limit=5,
                                                          collection="o_v2")] == ["img"]
    assert {h["url"] for h in vector_store.keyword_search("hoc phi", ["hoc"], limit=5)} == {"u1", "img"}
    vector_store.delete_by_document(1)
    assert mem_qdrant.count("t_v2").count == 1 and mem_qdrant.count("o_v2").count == 0


def test_v1_collection_refused_for_v2_writes(mem_qdrant):
    from qdrant_client.models import Distance, VectorParams
    mem_qdrant.create_collection("t_v2", vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    assert not vector_store.has_sparse()
    with pytest.raises(RuntimeError, match="schema v2"):
        vector_store.require_v2()


def test_doc_hash():
    assert indexer.doc_hash({"content_hash": "abc"}) == "abc"
    assert indexer.doc_hash({"file_path": "files/68/6883e2.pdf"}) == "6883e2"
    assert indexer.doc_hash({"file_sha256": "ff", "file_path": "x"}) == "ff"


class _FakeSession:
    """db.get_session giả cho reconcile: trả các dòng documents cố định, ghi lại UPDATE."""

    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        if stmt.__visit_name__ == "update":
            self.log.append(stmt)

        class R:
            def all(_s):
                return self.rows
        return R()

    async def commit(self):
        pass


def _reconcile_setup(mem_qdrant, monkeypatch, pg_rows):
    import asyncio

    from qdrant_client.models import PointStruct

    from sict_crawler import db, reconcile
    vector_store.ensure_collections()
    vector_store.upsert_points("t_v2", [
        vector_store.text_point(_pid(i), [1, 0, 0, 0], f"chunk {i}", {"kind": "text", "document_id": d})
        for i, d in enumerate([1, 1, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3])])
    vector_store.upsert_points("i_v2", [
        PointStruct(id=_pid(100), vector=[1, 0, 0, 0], payload={"document_id": [1, 2]}),
        PointStruct(id=_pid(101), vector=[0, 1, 0, 0], payload={"document_id": [2]})])
    log: list = []
    monkeypatch.setattr(db, "get_session", lambda: _FakeSession(pg_rows, log))
    return asyncio, reconcile, log


def test_reconcile_orphans(mem_qdrant, monkeypatch):
    # PG còn bài 1, 3 (bài 2 đã xoá) + bài 4 đã index (hash_v2, n_chunks) nhưng không có point
    rows = [(1, "h", {"n_chunks": 2}), (3, "h", {"n_chunks": 9}), (4, "h", {"n_chunks": 3})]
    asyncio, reconcile, log = _reconcile_setup(mem_qdrant, monkeypatch, rows)
    st = asyncio.run(reconcile.reconcile())
    assert st["orphan_text"] == 1 and st["image_detach"] == 1 and st["image_orphan"] == 1
    assert st["docs_missing_points"] == 1 and len(log) == 1      # bài 4 → hash_v2 = NULL
    assert mem_qdrant.count("t_v2").count == 11
    left = {p.id: p.payload["document_id"] for p in mem_qdrant.scroll("i_v2")[0]}
    assert left == {_pid(100): [1]}


def test_reconcile_guard_aborts_on_empty_pg(mem_qdrant, monkeypatch):
    asyncio, reconcile, log = _reconcile_setup(mem_qdrant, monkeypatch, [])
    with pytest.raises(reconcile.ReconcileAbort):
        asyncio.run(reconcile.reconcile())
    assert mem_qdrant.count("t_v2").count == 12                   # không xoá gì
