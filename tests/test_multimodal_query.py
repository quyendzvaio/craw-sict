"""D21: các use case đa phương thức của retrieve() trên không gian jina chung (backend api). Fake Qdrant/embedder."""
import pytest

from sict_crawler.core.config import get_settings
from sict_crawler.repository import cache, vector_store
from sict_crawler.service.embedding import embedder
from sict_crawler.service.retrieval import query_rewriter, retriever, router, sources

TXT, IMG = [1.0, 0.0], [0.0, 1.0]


@pytest.fixture
def mm(monkeypatch):
    e = get_settings().embedding
    monkeypatch.setattr(e, "backend", "api")
    calls = {"hybrid": [], "images": [], "text": []}
    monkeypatch.setattr(cache, "get", lambda k: None)
    monkeypatch.setattr(cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "embed_query", lambda q: TXT)
    monkeypatch.setattr(embedder, "embed_query_image_base64", lambda b: [IMG])
    monkeypatch.setattr(query_rewriter, "rewrite", lambda q: {"rewritten": q, "terms": q.split(), "route_llm": None})
    monkeypatch.setattr(query_rewriter, "rerank_text", lambda h, q, top_n=5, **k: [dict(x, raw_score=0.9) for x in h])

    def hybrid(tvec, terms, limit=8, filter_document_id=None, query_filter=None, text=None,
               graph_filter=None, extra_vecs=None):
        calls["hybrid"].append((tvec, extra_vecs))
        out = []
        if tvec is not None:
            out.append({"url": "https://x/vn/hoc-phi", "title": "Học phí", "content": "học phí 2026", "score": 0.9,
                        "document_id": 1, "chunk_id": "c1", "heading_path": ["Mức thu"]})
        for via, _ in extra_vecs or []:                              # hit từ vector ảnh: một đủ ngưỡng, một dưới i2t_min
            out += [{"url": "https://x/vn/su-kien", "title": "Sự kiện", "content": "ảnh sự kiện", "score": 0.5,
                     "document_id": 5, "chunk_id": "c5", "via": via},
                    {"url": "https://x/vn/khac", "title": "Khác", "content": "x", "score": 0.1,
                     "document_id": 9, "chunk_id": "c9", "via": via}]
        return out
    monkeypatch.setattr(query_rewriter, "hybrid_search_text", hybrid)

    def images(vec, limit=5, query_filter=None):
        calls["images"].append(vec)
        if vec == IMG:   # ảnh giống ảnh người dùng gửi
            return [{"kind": "image", "media_key": "sim1", "url": "u/sim1.jpg", "title": "Sự kiện", "score": 0.9,
                     "document_id": [5], "page_urls": ["https://x/vn/su-kien"]},
                    {"kind": "image", "media_key": "weak", "url": "u/weak.jpg", "title": "Y", "score": 0.4,
                     "document_id": [7]}]
        return [{"kind": "image", "media_key": "t2i1", "url": "u/t2i1.jpg", "title": "Học phí", "score": 0.3,
                 "document_id": [1]},
                {"kind": "image", "media_key": "t2i-far", "url": "u/far.jpg", "title": "Z", "score": 0.3,
                 "document_id": [99]}]
    monkeypatch.setattr(vector_store, "search_images", images)

    def search_text(vec, limit=8, filter_document_id=None, query_filter=None, collection=None):
        calls["text"].append((vec, filter_document_id))
        return [{"url": "https://x/vn/su-kien", "title": "Sự kiện", "content": "đoạn sát ảnh", "score": 0.8,
                 "document_id": filter_document_id, "chunk_id": f"link{filter_document_id}", "kind": "text"}]
    monkeypatch.setattr(vector_store, "search_text", search_text)
    monkeypatch.setattr(vector_store, "scroll_text", lambda *a, **k: [])        # không chạm Qdrant thật: ảnh chưa có block
    monkeypatch.setattr(vector_store, "keyword_search", lambda *a, **k: [])
    monkeypatch.setattr(vector_store, "has_sparse", lambda *a, **k: True)
    return calls


def test_u3_u4_image_only(mm):
    t, m, route = retriever.retrieve("", 5, "AAAA")
    assert route.inputs == ["image"] and route.outputs == ["text", "image"] and route.media == "required"
    tvec, extra = mm["hybrid"][0]
    assert tvec is None and extra == [("i2t", IMG)]                       # không có nhánh chữ, chỉ vector ảnh
    assert [s.url for s in t][0] == "https://x/vn/su-kien"                # chunk của bài chứa ảnh giống đứng đầu
    assert t[0].via == "i2i-doc" and "https://x/vn/khac" not in [s.url for s in t]   # hit dưới i2t_min bị lọc
    assert [s.media_key for s in m] == ["sim1"] and m[0].via == "i2i"     # ảnh giống ≥ i2i_min; ảnh 0.4 bị loại
    assert mm["text"] == [(IMG, 5)]                                       # chỉ kéo đoạn của bài có ảnh giống


def test_u2_text_to_image(mm):
    t, m, route = retriever.retrieve("Cho xem ảnh học phí", 5)
    assert route.inputs == ["text"] and route.media == "required"
    assert mm["images"] == [TXT]                                          # vector chữ tìm trong collection ảnh
    assert sorted(s.media_key for s in m) == ["t2i-far", "t2i1"] and all(s.via == "clip" for s in m)
    assert t[0].heading_path == ["Mức thu"] and t[0].via == "text"


def test_u5_u6_u7_text_plus_image(mm):
    t, m, route = retriever.retrieve("học phí", 5, "AAAA")                     # route chữ = none → nâng optional vì có ảnh
    assert route.inputs == ["text", "image"] and route.media == "optional"
    tvec, extra = mm["hybrid"][0]
    assert tvec == TXT and extra == [("i2t", IMG)]                        # U5: RRF dense chữ + dense ảnh
    keys = [s.media_key for s in m]
    assert "sim1" in keys                                                 # U6: ảnh giống không bị buộc thuộc top bài chữ
    assert "t2i-far" not in keys and "t2i1" in keys                       # ảnh chữ→ảnh vẫn phải thuộc bài chữ đứng đầu
    assert route.outputs == ["text", "image"]                             # U7
    assert t[0].via == "i2i-doc" and t[0].url == "https://x/vn/su-kien"   # "ảnh này là gì?": bài chứa ảnh lên đầu


def test_broken_image_only_returns_empty(mm, monkeypatch):
    monkeypatch.setattr(embedder, "embed_query_image_base64", lambda b: [])
    t, m, route = retriever.retrieve("", 5, "not-an-image")
    assert (t, m) == ([], []) and route.inputs == ["image"] and route.outputs == []


def test_gate_i2i_i2t_thresholds():
    c = [{"modality": "i2i", "raw_score": 0.7, "media_key": "a", "document_id": [3]},
         {"modality": "i2i", "raw_score": 0.5, "media_key": "b", "document_id": [3]},
         {"modality": "i2t", "raw_score": 0.35, "kind": "media_caption", "media_kind": "image", "media_key": "c"},
         {"modality": "clip", "raw_score": 0.3, "media_key": "d", "document_id": [3]}]
    out = router.gate(router.Route("required"), [], c, 8, clip_min=0.25, caption_min=0.5, i2i_min=0.6, i2t_min=0.3)
    assert sorted(h["media_key"] for h in out) == ["a", "c", "d"]
    out = router.gate(router.Route("optional"), [], c, 8, clip_min=0.25, caption_min=0.5, i2i_min=0.6, i2t_min=0.3)
    assert sorted(h["media_key"] for h in out) == ["a", "c"]               # optional: i2i/i2t không cần thuộc top bài chữ


def test_context_shows_heading_path():
    [src] = sources.text_sources([{"url": "https://x/vn/a", "title": "Học phí", "content": "nội dung", "score": 1,
                               "heading_path": ["A", "B"], "published_at": "2026-09-11T10:00:00+07:00"}])
    assert sources.build_context([src], []).startswith("[1] (text) Học phí › A › B (11/09/2026)\nnội dung")


def test_rerank_uses_stored_cosine_without_embedding(monkeypatch):
    """raw_score của caption = cosine đã có (dense) hoặc vector đã lưu (BM25) — không nhúng lại (v3: 16 s/câu)."""
    from sict_crawler.service.retrieval import query_rewriter
    monkeypatch.setattr(embedder, "embed_texts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("không nhúng lại")))
    asked = []

    def point_vectors(refs):
        asked.extend(refs)
        return {"p-bm25": [0.6, 0.8]}
    monkeypatch.setattr(vector_store, "point_vectors", point_vectors)
    hits = [{"kind": "text", "url": "a", "score": 0.03},
            {"kind": "media_caption", "url": "b", "score": 0.02, "dense_score": 0.71, "_pid": "p-dense", "_col": "o"},
            {"kind": "media_caption", "url": "c", "score": 0.01, "_pid": "p-bm25", "_col": "o"}]
    out = query_rewriter.rerank_text(hits, "q", top_n=3, reorder=False, qvec=[1.0, 0.0])
    assert [h["url"] for h in out] == ["a", "b", "c"]                      # giữ thứ tự RRF
    assert out[1]["raw_score"] == 0.71 and abs(out[2]["raw_score"] - 0.6) < 1e-9
    assert asked == [("o", "p-bm25")] and "raw_score" not in out[0]
