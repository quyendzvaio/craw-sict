"""Giảm độ trễ truy xuất (2026-10-25): lượt Qdrant chạy song song (giai đoạn → nhánh → collection), không gọi LLM
viết lại câu hỏi theo mặc định. Fake Qdrant / embedder có độ trễ, không cần dịch vụ."""
import time

import pytest

from sict_crawler.core.config import get_settings
from sict_crawler.repository import cache, vector_store
from sict_crawler.service.embedding import embedder
from sict_crawler.service.retrieval import query_rewriter, retriever, vlm

D = 0.2   # độ trễ giả mỗi lượt gọi


def test_parallel_keeps_order_and_raises():
    out = vector_store.parallel([lambda i=i: (time.sleep(D * (3 - i) / 3), i)[1] for i in range(3)])
    assert out == [0, 1, 2]
    with pytest.raises(ZeroDivisionError):
        vector_store.parallel([lambda: 1, lambda: 1 / 0])


class _Pts:
    def __init__(self, name):
        self.points = [type("P", (), {"score": 0.5, "id": f"{name}-1", "payload": {"url": f"https://x/{name}",
                                                                                     "chunk_id": name}})()]


class _SlowClient:
    def __init__(self):
        self.calls = 0

    def query_points(self, name, **kw):
        self.calls += 1
        time.sleep(D)
        return _Pts(name)


def test_collections_searched_in_parallel(monkeypatch):
    c = _SlowClient()
    monkeypatch.setattr(vector_store, "get_client", lambda: c)
    monkeypatch.setattr(vector_store, "text_collections", lambda: ["sict_text_v3", "sict_ocr_v3"])
    t = time.perf_counter()
    hits = vector_store.search_text([0.1, 0.2], limit=3)
    took = time.perf_counter() - t
    assert c.calls == 2 and {h["_col"] for h in hits} == {"sict_text_v3", "sict_ocr_v3"}
    assert took < 1.6 * D, took                                    # tuần tự = 2 × D


def test_hybrid_legs_run_in_parallel(monkeypatch):
    def dense(vec, limit=8, filter_document_id=None, query_filter=None, collection=None):
        time.sleep(D)
        return [{"url": f"https://x/d{vec[0]}", "chunk_id": f"d{vec[0]}", "score": 0.9}]

    def sparse(text, terms, limit=8, query_filter=None, collection=None):
        time.sleep(D)
        return [{"url": f"https://x/s{len(text)}", "chunk_id": f"s{len(text)}", "score": 3.0}]
    monkeypatch.setattr(vector_store, "search_text", dense)
    monkeypatch.setattr(vector_store, "keyword_search", sparse)
    t = time.perf_counter()
    hits = query_rewriter.hybrid_search_text([1.0], ["a"], text="câu gốc", alt_queries=[([2.0], "câu viết lại dài hơn")])
    took = time.perf_counter() - t
    assert len(hits) == 4                                          # 2 dense + 2 BM25, đủ cả 4 danh sách
    assert any("dense_score" in h for h in hits)                  # nhánh dense vẫn giữ cosine cho rerank_text
    assert took < 2.5 * D, took                                    # tuần tự = 4 × D


def test_rewrite_does_not_call_llm_by_default(monkeypatch):
    monkeypatch.setattr(get_settings().llm, "rewrite_query", False)
    monkeypatch.setattr(vlm, "rewrite_and_route", lambda q: (_ for _ in ()).throw(AssertionError("gọi LLM")))
    r = query_rewriter.rewrite("Ngành CNTT học gì?")
    assert "công nghệ thông tin" in r["rewritten"].lower() and r["route_llm"] is None   # chỉ luật viết tắt


def test_rewrite_llm_opt_in(monkeypatch):
    monkeypatch.setattr(get_settings().llm, "rewrite_query", True)
    monkeypatch.setattr(vlm, "rewrite_and_route", lambda q: {"rewritten": "viết lại", "media": "required",
                                                             "wants_file": False})
    r = query_rewriter.rewrite("x")
    assert r["rewritten"] == "viết lại" and r["route_llm"] == {"media": "required", "wants_file": False}


def test_retrieve_runs_text_and_media_legs_concurrently(monkeypatch):
    monkeypatch.setattr(get_settings().embedding, "backend", "api")
    monkeypatch.setattr(cache, "get", lambda k: None)
    monkeypatch.setattr(cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "embed_query", lambda q: [1.0, 0.0])
    monkeypatch.setattr(vector_store, "has_sparse", lambda *a, **k: True)
    monkeypatch.setattr(vector_store, "point_vectors", lambda need: {})

    def hybrid(*a, **k):
        time.sleep(2 * D)
        return [{"url": "https://x/1", "title": "t", "content": "c", "score": 0.9, "document_id": 1, "chunk_id": "c1",
                 "dense_score": 0.7}]

    def media(*a, **k):
        time.sleep(2 * D)
        return []
    monkeypatch.setattr(query_rewriter, "hybrid_search_text", hybrid)
    monkeypatch.setattr(retriever, "_media_candidates", media)
    retriever.retrieve("Cho tôi xem ảnh lễ tốt nghiệp", 5)        # khởi động: lần đầu còn import graph… (không đo)
    t = time.perf_counter()
    text, _, route = retriever.retrieve("Cho tôi xem ảnh lễ tốt nghiệp", 5)
    took = time.perf_counter() - t
    assert route.media == "required" and text
    assert took < 3.2 * D, took                                    # tuần tự = 4 × D
