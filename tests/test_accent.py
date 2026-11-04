"""Khôi phục dấu cho câu hỏi không dấu (retrieval.accent). Mô hình nhỏ dựng từ vài câu, không cần Qdrant."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.service.retrieval import accent as A

CORPUS = ["Cố vấn học tập của trường gồm các thầy cô.", "Cố vấn học tập hỗ trợ sinh viên.",
          "Đề tài nghiên cứu khoa học của sinh viên năm 2022.", "Đề tài nghiên cứu khoa học cấp trường.",
          "Khoa Công nghệ thông tin.", "Học máy và trí tuệ nhân tạo.", "Phòng máy tính tầng 5."]


def _restorer():
    return A.Restorer(A.build(CORPUS * 3))


def test_restore_keeps_digits_case_and_acronyms():
    r = _restorer()
    assert r.restore("co van hoc tap cua truong") == "cố vấn học tập của trường"
    assert r.restore("De tai nghien cuu khoa hoc nam 2022?") == "Đề tài nghiên cứu khoa học năm 2022?"
    assert r.restore("CNTT co van") == "CNTT cố vấn"            # viết tắt in hoa giữ nguyên


def test_maybe_restore_only_for_unaccented_vietnamese(monkeypatch):
    monkeypatch.setattr(A, "_restorer", _restorer)
    assert A.maybe_restore("co van hoc tap") == "cố vấn học tập"
    assert A.maybe_restore("Cố vấn học tập") is None            # đã có dấu
    assert A.maybe_restore("Who is the dean?") is None          # không phải tiếng Việt khôi phục được
    assert A.maybe_restore("KSTN 2026") is None


def test_rewrite_adds_restored_query_when_enabled(monkeypatch):
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.retrieval import query_rewriter as QR
    monkeypatch.setattr(A, "_restorer", _restorer)
    monkeypatch.setattr(get_settings().llm, "rewrite_query", False)
    monkeypatch.setattr(get_settings().retrieval, "restore_accents", True)
    r = QR.rewrite("co van hoc tap cua truong")
    assert r["rewritten"] == "cố vấn học tập của trường" and r["restored"] == "cố vấn học tập của trường"
    monkeypatch.setattr(get_settings().retrieval, "restore_accents", False)
    r = QR.rewrite("co van hoc tap cua truong")
    assert r["rewritten"] == "co van hoc tap cua truong" and r["restored"] is None


def test_retrieve_embeds_restored_question_for_dense(monkeypatch):
    """Câu không dấu đã khôi phục: vector dense là của câu có dấu, không thêm danh sách phụ trùng BM25 của câu gốc."""
    from sict_crawler.service.retrieval import query_rewriter as QR, retriever as R
    embedded, calls = [], {}
    monkeypatch.setattr(QR, "rewrite", lambda q: {"rewritten": "cố vấn học tập", "terms": ["co", "van"],
                                                  "route_llm": None, "restored": "cố vấn học tập"})
    monkeypatch.setattr(R.embedder, "embed_query", lambda t: embedded.append(t) or [1.0])
    def hybrid(tvec, terms, **k):
        calls.update(k)
        return []
    monkeypatch.setattr(QR, "hybrid_search_text", hybrid)
    monkeypatch.setattr(R.cache, "get", lambda k: None)
    monkeypatch.setattr(R.cache, "put", lambda *a, **k: None)
    R.retrieve("co van hoc tap", top_k=3)
    assert embedded[0] == "cố vấn học tập" and calls["text"] == "co van hoc tap" and not calls["alt_queries"]
