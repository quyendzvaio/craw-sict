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
                  content="học phí học kỳ phụ", cosine=0.8)], []
    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: off)
    assert _agent.run("Giá vé xem phim CGV hôm nay?", 5)["sufficient"] is False
    assert _agent.run("Học phí học kỳ phụ?", 5)["sufficient"] is True


def test_run_insufficient_keeps_only_coupled_media(monkeypatch):
    """C: câu ngoài phạm vi đòi ảnh ("cho xem ảnh tháp Eiffel", `sufficient` False) không còn ảnh ngẫu nhiên; ảnh thuộc
    bài chữ đứng đầu vẫn giữ. Câu đủ bằng chứng không bị đụng."""
    from sict_crawler.service.retrieval import retriever, router

    text = [Source(url="https://x/a", title="Học phí", kind="text", score=0.9, excerpt="học phí học kỳ phụ",
                   content="học phí học kỳ phụ", cosine=0.8)]
    media = [Source(url="i1", title="Học phí", kind="image", score=0.3, parent_url="https://x/a", via="doc"),
             Source(url="i2", title="Khác", kind="image", score=0.3, parent_url="https://x/z", via="clip")]
    route = router.Route(media="required", outputs=["text", "image"])
    monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: (text, list(media), route))
    res = _agent.run("Cho xem ảnh tháp Eiffel?", 5)
    assert res["sufficient"] is False and [m.url for m in res["media"]] == ["i1"] and "(image)" in res["context"]
    res = _agent.run("Học phí học kỳ phụ?", 5)
    assert res["sufficient"] is True and [m.url for m in res["media"]] == ["i1", "i2"]


def test_evidence_weak_top_cosine_flags_partial():
    """2026-11-10 (đề xuất 2, chủ duyệt nâng 0,55 → 0,75): top-1 cosine dưới weak_top_cosine → sufficient=False
    ("đoạn đầu yếu") — case "kế hoạch đào tạo 2025" trả 8 bài kế hoạch sai bài, cosine ~0,62 cũ không chặn."""
    from sict_crawler.core.config import get_settings
    cfg = get_settings().retrieval
    assert cfg.weak_top_cosine > cfg.min_cosine, "ngưỡng khối phải chặt hơn min_cosine"
    # cosine 0,62: trên min_cosine (0,45) nhưng dưới weak_top_cosine (0,75) → không đủ (case thực tế)
    weak = [Source(url="u", title="Kế hoạch", kind="text", score=0.9, excerpt="kế hoạch đào tạo",
                   content="kế hoạch đào tạo năm học mới", cosine=0.62)]
    ev = _agent.evidence("kế hoạch đào tạo 2025?", weak, [], "kế hoạch đào tạo 2025")
    assert ev["sufficient"] is False and "đoạn đầu yếu" in ev["reason"]
    # cosine 0,50 cũng không đủ
    mid = [Source(url="u", title="Kế hoạch", kind="text", score=0.9, excerpt="kế hoạch đào tạo",
                  content="kế hoạch đào tạo năm 2025 chương trình", cosine=0.50)]
    ev = _agent.evidence("kế hoạch đào tạo 2025?", mid, [], "kế hoạch đào tạo 2025 chương trình")
    assert ev["sufficient"] is False
    # đủ mạnh (≥ 0,75) → không flag
    strong = [Source(url="u", title="Kế hoạch", kind="text", score=0.9, excerpt="kế hoạch đào tạo",
                     content="kế hoạch đào tạo năm 2025 chương trình", cosine=0.78)]
    ev = _agent.evidence("kế hoạch đào tạo 2025?", strong, [], "kế hoạch đào tạo 2025 chương trình")
    assert ev["sufficient"] is True and ev["reason"] == "ok"


def test_evidence_image_only_requires_matching_source():
    """2026-11-10 (chủ duyệt, case A/B/C ảnh người): chỉ ảnh + "người này là ai" —
    A) có ảnh i2i khớp (hoặc i2i-doc) → đủ;  B) chỉ ảnh/text không đạt → không đủ
    (không trả thông tin người sai);  C) rỗng → không đủ."""
    hit = [Source(url="i1", title="PGS.TS Trần Tiến Dũng", kind="image", score=0.9, via="i2i", cosine=0.75)]
    ev = _agent.evidence("", [], hit, "nguồn")          # A: ảnh khớp → đủ
    assert ev["sufficient"] is True
    doc = [Source(url="u", title="Trang giảng viên", kind="text", score=0.9, via="i2i-doc")]
    ev = _agent.evidence("", doc, [], "nguồn")          # A': i2i-doc → đủ
    assert ev["sufficient"] is True
    stray = [Source(url="i2", title="Ảnh khác", kind="image", score=0.9, via="clip")]
    stray_text = [Source(url="u2", title="Bài khác", kind="text", score=0.9, via="text")]
    ev = _agent.evidence("", stray_text, stray, "nguồn")  # B: không i2i/i2i-doc → không đủ
    assert ev["sufficient"] is False and "đạt ngưỡng" in ev["reason"]
    ev = _agent.evidence("", [], [], "")                # C: rỗng → không đủ
    assert ev["sufficient"] is False


def test_router_person_id_in_image_is_optional():
    """2026-11-10 (chủ duyệt): ảnh người + "người này là ai" → media optional (đối chiếu ảnh giúp,
    không đòi thêm ảnh). Không ảnh gửi kèm → luật không tự kích."""
    from sict_crawler.service.retrieval import router
    r = router.classify_rules("người này là ai?", has_image=True)
    assert r.media == "optional"
    r = router.classify_rules("đây là ai trong ảnh?", has_image=True)
    assert r.media == "optional"
    # không có ảnh đầu vào → không khớp ý định nhận diện người
    r = router.classify_rules("người này là ai?", has_image=False)
    assert r.media == "none"


def test_evidence_image_only_with_question_still_runs_rules():
    """"người này là ai" + ảnh → có câu hỏi → luật cosine/cover chạy như thường (nhánh question.strip())."""
    weak = [Source(url="u", title="Bài A", kind="text", score=0.9, via="i2i-doc",
                   content="bài khác hẳn", cosine=0.30)]
    ev = _agent.evidence("người này là ai?", weak, [], "bài khác hẳn")
    assert ev["sufficient"] is False  # cosine 0,30 < min_cosine 0,45


def test_validate_consolidates_post_checks():
    """Validate là điểm neo duy nhất của hậu kiểm: (a) đủ bằng chứng giữ nguyên; (b) thiếu → chỉ
    giữ media gắn bài chữ; (c) thiếu + required + rỗng sau C → note + bỏ output ảnh; (d) optional
    rỗng không note."""
    from sict_crawler.service.retrieval import router

    text = [Source(url="https://x/a", title="Học phí", kind="text", score=0.9, excerpt="học phí",
                   content="học phí học kỳ phụ", cosine=0.8)]
    media = [Source(url="i1", title="Học phí", kind="image", score=0.3, parent_url="https://x/a", via="doc"),
             Source(url="i2", title="Khác", kind="image", score=0.3, parent_url="https://x/z", via="clip")]
    route = router.Route(media="required", outputs=["text", "image"])
    # (a) sufficient True → giữ nguyên, không note
    _, m, note, r = _agent.validate("học phí?", text, list(media), "ctx", {"sufficient": True}, route)
    assert [x.url for x in m] == ["i1", "i2"] and note is None and r.outputs == ["text", "image"]
    # (b) False + media gắn → chỉ giữ media gắn, chưa note (còn media)
    _, m, note, r = _agent.validate("học phí?", text, list(media), "ctx", {"sufficient": False}, route)
    assert [x.url for x in m] == ["i1"] and note is None
    # (c) False + required + rỗng sau C → note AC-R9 + bỏ "image" khỏi outputs
    _, m, note, r = _agent.validate("cho xem ảnh tháp Eiffel?", text, [media[1]], "ctx",
                                    {"sufficient": False}, route)
    assert m == [] and note and "ảnh/video" in note and "image" not in r.outputs
    # (d) False + optional + rỗng → không note
    opt = router.Route(media="optional", outputs=["text"])
    _, m, note, r = _agent.validate("học phí?", text, [media[1]], "ctx", {"sufficient": False}, opt)
    assert m == [] and note is None


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
                  test_run_insufficient_when_question_terms_missing,
                  test_run_insufficient_keeps_only_coupled_media):
            t(mp)
            mp.undo()
            print("OK —", t.__name__)
    finally:
        mp.undo()
        os.environ.pop("LLM_API_KEY", None)
        config._settings = None
