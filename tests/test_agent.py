"""Self-check cho agent loop (mock, không cần network/services).

Run: python -m tests.test_agent  |  pytest tests/test_agent.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.model.schemas import Source
from sict_crawler.service.retrieval import agent as _agent


def _srcs():
    return [Source(url="u", title="t", kind="text", score=0.9, excerpt="e")], []


def test_run_does_not_call_llm_judge(monkeypatch):
    """D27: không còn LLM chấm ngữ cảnh — agent chỉ truy xuất 1 lượt, không gọi LLM (phần viết lại câu hỏi nằm trong
    retrieve)."""
    from sict_crawler.service.retrieval import retriever, vlm

    called = []
    monkeypatch.setattr(vlm, "_text_call", lambda *a, **k: called.append(a) or "")
    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: _srcs())
    res = _agent.run("học phí?", 5)
    assert called == [] and res["attempts"] == 1
    assert len(res["text"]) == 1 and "[1] (text) t" in res["context"]
    assert any(x.startswith("evidence:") for x in res["log"])


def test_run_single_retrieval_pass(monkeypatch):
    from sict_crawler.service.retrieval import retriever

    calls = []
    monkeypatch.setattr(retriever, "retrieve", lambda q, k, *a, **kw: (calls.append(k), _srcs())[1])
    _agent.run("học phí?", 5)
    assert calls == [5]   # trước D27: 5 rồi 10 khi LLM chấm "chưa đủ"


def test_run_empty_falls_back_to_lexical_and_is_insufficient(monkeypatch):
    from sict_crawler.repository import vector_store
    from sict_crawler.service.retrieval import retriever

    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: ([], []))
    # lexical fallback không chạm Qdrant thật (BM25 khớp cả từ phổ biến "không", "tại")
    kw = []
    monkeypatch.setattr(vector_store, "keyword_search", lambda *a, **k: kw.append(a) or [])
    res = _agent.run("xyzabc không tồn tại?", 5)
    assert kw and res["attempts"] == 2
    assert res["sufficient"] is False and res["context"] == ""


def test_run_insufficient_when_question_terms_missing(monkeypatch):
    from sict_crawler.service.retrieval import retriever

    off = [Source(url="u", title="Thông báo học phí", kind="text", score=0.9, excerpt="học phí học kỳ phụ",
                  content="học phí học kỳ phụ", cosine=0.7)], []
    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: off)
    assert _agent.run("Giá vé xem phim CGV hôm nay?", 5)["sufficient"] is False
    assert _agent.run("Học phí học kỳ phụ?", 5)["sufficient"] is True


if __name__ == "__main__":
    import os

    class _MP:
        def __init__(self):
            self._orig = []

        def setattr(self, obj, name, val):
            self._orig.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def delenv(self, k, raising=True):
            os.environ.pop(k, None)

        def setenv(self, k, v):
            os.environ[k] = v

        def undo(self):
            for obj, name, val in reversed(self._orig):
                setattr(obj, name, val)
            self._orig.clear()

    os.environ.pop("LLM_API_KEY", None)
    from sict_crawler.core import config

    config._settings = None
    mp = _MP()
    try:
        for t in (test_run_does_not_call_llm_judge, test_run_single_retrieval_pass,
                  test_run_empty_falls_back_to_lexical_and_is_insufficient,
                  test_run_insufficient_when_question_terms_missing):
            t(mp)
            mp.undo()
            print("OK —", t.__name__)
    finally:
        mp.undo()
        os.environ.pop("LLM_API_KEY", None)
        config._settings = None
