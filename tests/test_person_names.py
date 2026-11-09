"""Nhánh "cụm tên riêng" (2026-11-05): lấy cụm viết hoa trong câu hỏi, danh sách dense lọc nguyên cụm vào RRF,
evidence đòi cụm tên có mặt nguyên cụm trong ngữ cảnh. Fake Qdrant/embedder, không cần dịch vụ."""
import pytest

from sict_crawler.core.config import get_settings
from sict_crawler.model.schemas import Source
from sict_crawler.repository import cache, vector_store
from sict_crawler.service.embedding import embedder
from sict_crawler.service.retrieval import agent, query_rewriter, retriever
from sict_crawler.service.retrieval.query_rewriter import name_phrases


# --- lấy cụm tên riêng --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q, want", [
    ("khoảnh khắc của sinh viên Phạm Văn Tùng", ["Phạm Văn Tùng"]),
    ("TS. Nguyễn Bá Nghiễn học tiến sĩ ở đâu?", ["Nguyễn Bá Nghiễn"]),          # viết tắt có dấu chấm cắt cụm
    ("thầy Hà Mạnh Đào dạy khoa nào vậy", ["Hà Mạnh Đào"]),
    ("Phạm Văn Tùng", ["Phạm Văn Tùng"]),                                      # cả câu là cụm → giữ cả từ đầu
    ("Phạm Văn Tùng là ai", ["Văn Tùng"]),                                      # từ đầu câu không tính
    ("Thầy Hà Mạnh Đào dạy khoa nào", ["Hà Mạnh Đào"]),                        # "Thầy" đầu câu không dính vào tên
    ("Lê Đình Quốc, Trần Quang Lộc là ai", ["Đình Quốc", "Trần Quang Lộc"]),     # dấu phẩy cắt cụm; "Lê" đầu câu
    ("Ai là Lê Đình Quốc, Trần Quang Lộc?", ["Lê Đình Quốc", "Trần Quang Lộc"]),
    ("Khuất Quang Ngọc và Hoàng Tùng Lâm dự sự kiện gì?", ["Quang Ngọc", "Hoàng Tùng Lâm"]),
    ('ảnh "Lương Minh Anh" ở đâu', ["Lương Minh Anh"]),
])
def test_name_phrases_positive(q, want):
    assert name_phrases(q) == want


@pytest.mark.parametrize("q", [
    "Năm 2026 Trường CNTT và Truyền thông có bao nhiêu chương trình đào tạo?",   # g004: "Năm" đầu câu, số, viết tắt
    "Năm Trường có bao nhiêu ngành",                                               # "Năm Trường" đầu câu không là tên
    "Đội HaUI.No_Solution_AI vào chung kết NAPROCK PROCON 2026 gồm những ai?",   # m30: token lẫn ký hiệu, toàn hoa
    "CNTT", "SICT là gì", "Học phí năm 2025 bao nhiêu?", "pham van tung la ai", "Thông báo mới nhất?", "",
    "Trụ sở Trường ở đâu?",                                                          # 1 từ hoa giữa câu
])
def test_name_phrases_negative(q):
    assert name_phrases(q) == []


def test_name_phrases_keeps_mid_sentence_two_word_runs():
    # cụm 2 từ hoa giữa câu vẫn là ứng viên ("Trường Công" trong "Hiệu trưởng Trường Công nghệ…"): cụm quá phổ biến
    # bị vector_store.search_text_phrase bỏ bằng cap số điểm, không phải bằng luật chữ
    assert name_phrases("Hiệu trưởng Trường Công nghệ thông tin và Truyền thông là ai?") == ["Trường Công"]
    assert name_phrases("Năm Trường Công nghệ thông tin có bao nhiêu ngành") == ["Trường Công"]   # "Năm" đầu câu bỏ


# --- evidence đòi cụm tên trong ngữ cảnh ----------------------------------------------------------------------------

def _src(content, cosine=0.8):
    return Source(url="u", title="t", kind="text", score=0.9, excerpt=content[:50], content=content, cosine=cosine)


def test_evidence_requires_name_phrase_as_a_whole(monkeypatch):
    monkeypatch.setattr(get_settings().retrieval, "min_cosine", 0.45)
    monkeypatch.setattr(get_settings().retrieval, "min_term_cover", 0.8)
    monkeypatch.setattr(get_settings().retrieval, "weak_top_cosine", 0.75)   # test này kiểm tên riêng, không phải cosine
    q = "khoảnh khắc của sinh viên Phạm Văn Tùng"
    scattered = "khoảnh khắc sinh viên. ThS. Nguyễn Thanh Tùng. TS. Phạm Huy Thông. Phạm Văn Hà văn hóa."
    ev = agent.evidence(q, [_src(scattered)], [], scattered)          # đủ token rời nhưng không có nguyên cụm
    assert ev["sufficient"] is False and "tên riêng Phạm Văn Tùng" in ev["reason"]
    whole = "khoảnh khắc sinh viên Phạm Văn Tùng tại OlpAI'25"
    assert agent.evidence(q, [_src(whole)], [], whole)["sufficient"] is True
    folded = "khoanh khac sinh vien PHAM VAN TUNG"                     # so chuỗi bỏ dấu, không phân biệt hoa thường
    assert agent.evidence(q, [_src(folded)], [], folded)["sufficient"] is True


def test_evidence_without_name_phrase_unchanged(monkeypatch):
    monkeypatch.setattr(get_settings().retrieval, "min_cosine", 0.45)
    monkeypatch.setattr(get_settings().retrieval, "min_term_cover", 0.8)
    monkeypatch.setattr(get_settings().retrieval, "weak_top_cosine", 0.75)
    ctx = "học phí học kỳ phụ"
    assert agent.evidence("Học phí học kỳ phụ?", [_src(ctx)], [], ctx)["sufficient"] is True


# --- nhánh RRF ----------------------------------------------------------------------------------------------------

def _hit(doc, score):
    return {"url": f"https://x/{doc}", "title": "t", "content": "c", "score": score, "document_id": doc,
            "chunk_id": f"d{doc}", "kind": "text"}


def test_hybrid_adds_dense_and_bm25_lists_per_phrase(monkeypatch):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    seen, calls = [], {"dense": [], "bm25": []}
    name_f = Filter(must=[FieldCondition(key="content", match=MatchValue(value="pvt"))])

    def dense(vec, limit=8, filter_document_id=None, query_filter=None, collection=None):
        calls["dense"].append(query_filter)
        return [_hit(9, 0.4)] if query_filter is not None else [_hit(1, 0.9), _hit(2, 0.8)]

    def bm25(text, terms, limit=8, query_filter=None, collection=None):
        calls["bm25"].append(query_filter)
        return [_hit(9, 3.0)] if query_filter is not None else []

    def phrase(ph, max_hits=200):
        seen.append(ph)
        return name_f if ph == "Phạm Văn Tùng" else None       # "Trường Công" quá phổ biến → None
    monkeypatch.setattr(vector_store, "search_text", dense)
    monkeypatch.setattr(vector_store, "keyword_search", bm25)
    monkeypatch.setattr(vector_store, "phrase_filter", phrase)
    hits = query_rewriter.hybrid_search_text([1.0], ["x"], limit=8, text="q", phrases=["Phạm Văn Tùng", "Trường Công"])
    assert seen == ["Phạm Văn Tùng", "Trường Công"]
    # cụm hợp lệ: +1 dense +1 BM25 lọc cụm (các nhánh chạy song song → không so thứ tự gọi)
    for leg in ("dense", "bm25"):
        assert len(calls[leg]) == 2 and calls[leg].count(None) == 1 and name_f in calls[leg]
    assert [h["document_id"] for h in hits][0] == 9                                # 2 danh sách hạng 1 > 1 danh sách
    doc9 = next(h for h in hits if h["document_id"] == 9)
    assert doc9["via"] == "name" and doc9["dense_score"] == 0.4   # hit chỉ từ nhánh tên giữ cosine để evidence dùng
    # không có cụm / không có index → kết quả như cũ
    monkeypatch.setattr(vector_store, "phrase_filter", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no index")))
    assert [h["document_id"] for h in query_rewriter.hybrid_search_text([1.0], ["x"], text="q", phrases=["Lê Sỹ Toàn"])] == [1, 2]
    assert [h["document_id"] for h in query_rewriter.hybrid_search_text([1.0], ["x"], text="q", phrases=None)] == [1, 2]


def test_rrf_keeps_cosine_from_any_dense_list():
    bm25_first = [_hit(1, 3.0)]                                   # BM25 xếp trước, không có cosine
    dense = [{**_hit(1, 0.6), "dense_score": 0.6}, {**_hit(2, 0.5), "dense_score": 0.5}]
    out = query_rewriter._rrf(bm25_first, dense)
    assert [h["document_id"] for h in out] == [1, 2]              # thứ tự RRF không đổi
    assert out[0]["dense_score"] == 0.6 and out[0]["score"] > out[1]["score"]
    assert query_rewriter._rrf(dense, bm25_first)[0]["dense_score"] == 0.6


def test_retrieve_passes_name_phrases_of_original_question(monkeypatch):
    monkeypatch.setattr(get_settings().embedding, "backend", "api")
    monkeypatch.setattr(cache, "get", lambda k: None)
    monkeypatch.setattr(cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "embed_query", lambda q: [1.0, 1.0])
    monkeypatch.setattr(vector_store, "has_sparse", lambda *a, **k: True)
    monkeypatch.setattr(vector_store, "search_text", lambda *a, **k: [_hit(1, 0.9)])
    monkeypatch.setattr(vector_store, "keyword_search", lambda *a, **k: [])
    monkeypatch.setattr(vector_store, "point_vectors", lambda need: {})
    monkeypatch.setattr(query_rewriter, "rewrite", lambda x: {"rewritten": x, "terms": [], "route_llm": None})
    got = {}
    orig = query_rewriter.hybrid_search_text

    def spy(*a, **k):
        got["phrases"] = k.get("phrases")
        return orig(*a, **k)
    monkeypatch.setattr(query_rewriter, "hybrid_search_text", spy)
    retriever.retrieve("khoảnh khắc của sinh viên Phạm Văn Tùng", 5)
    assert got["phrases"] == ["Phạm Văn Tùng"]
    retriever.retrieve("Học phí năm 2025", 5)
    assert got["phrases"] == []


