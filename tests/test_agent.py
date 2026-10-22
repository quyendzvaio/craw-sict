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


def test_judge_fail_open_without_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from sict_crawler.core import config

    config._settings = None  # ép đọc lại env hiện tại
    try:
        v = _agent.judge_context("q?", "ctx")
        assert v["sufficient"] is True
    finally:
        config._settings = None


def test_judge_parses_verdict(monkeypatch):
    from sict_crawler.service.retrieval import vlm

    monkeypatch.setattr(vlm, "_text_call", lambda *a, **k: '{"sufficient": false, "reason": "thiếu"}')
    import os

    os.environ["LLM_API_KEY"] = "x"
    from sict_crawler.core import config

    config._settings = None
    try:
        v = _agent.judge_context("q?", "ctx ctx ctx")
        assert v["sufficient"] is False and v["reason"] == "thiếu"
        monkeypatch.setattr(vlm, "_text_call", lambda *a, **k: "not-json{{{")
        v2 = _agent.judge_context("q?", "ctx ctx ctx")
        assert v2["sufficient"] is True  # parse fail → fail-open
    finally:
        os.environ.pop("LLM_API_KEY", None)
        config._settings = None


def test_run_first_try_sufficient(monkeypatch):
    from sict_crawler.service.retrieval import retriever

    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: _srcs())
    monkeypatch.setattr(_agent, "judge_context",
                        lambda *a, **k: {"sufficient": True, "reason": "ok"})
    res = _agent.run("học phí?", 5)
    assert res["attempts"] == 1 and res["sufficient"] is True
    assert len(res["text"]) == 1 and "[1] (text) t" in res["context"]


def test_run_retries_then_sufficient(monkeypatch):
    from sict_crawler.service.retrieval import retriever

    calls = []
    monkeypatch.setattr(retriever, "retrieve",
                        lambda q, k, *a, **kw: (calls.append(k), _srcs())[1])
    verdicts = iter([{"sufficient": False, "reason": "mỏng"},
                     {"sufficient": True, "reason": "đủ"}])
    monkeypatch.setattr(_agent, "judge_context", lambda *a, **k: next(verdicts))
    res = _agent.run("học phí?", 5)
    assert res["attempts"] == 2 and res["sufficient"] is True
    assert calls == [5, 10]  # attempt2 top_k x2


def test_run_exhausted_marks_insufficient(monkeypatch):
    from sict_crawler.repository import vector_store
    from sict_crawler.service.retrieval import retriever

    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: ([], []))
    # lexical fallback không chạm Qdrant thật (BM25 khớp cả từ phổ biến "không", "tại")
    monkeypatch.setattr(vector_store, "keyword_search", lambda *a, **k: [])
    monkeypatch.setattr(_agent, "judge_context",
                        lambda *a, **k: {"sufficient": False, "reason": "rỗng"})
    res = _agent.run("xyzabc không tồn tại?", 5)
    assert res["attempts"] == _agent.MAX_ATTEMPTS
    assert res["sufficient"] is False
    assert res["context"] == "" and len(res["log"]) >= 3


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
        test_judge_fail_open_without_key(mp)
        print("OK — judge fail-open khi không key")
        test_judge_parses_verdict(mp)
        print("OK — judge parse JSON verdict")
        mp.undo()
        test_run_first_try_sufficient(mp)
        print("OK — đủ ngay attempt 1")
        mp.undo()
        test_run_retries_then_sufficient(mp)
        print("OK — retry top_k x2 rồi đủ")
        mp.undo()
        test_run_exhausted_marks_insufficient(mp)
        print("OK — cạn attempts → insufficient + log")
    finally:
        mp.undo()
        os.environ.pop("LLM_API_KEY", None)
        config._settings = None
