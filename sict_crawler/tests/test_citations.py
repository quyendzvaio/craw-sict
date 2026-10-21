"""D24: trích dẫn bắt buộc — nhãn (file → trang), enforce, SSE /chat, ngữ cảnh gần ảnh theo block."""
import json

import pytest

from sict_crawler import api, citations as C, vector_store
from sict_crawler.api import Source


def _src(**k):
    base = dict(url="https://sict.haui.edu.vn/vn/a/1", title="Bài A", kind="text", score=1.0, excerpt="x")
    base.update(k)
    base["cite"] = base.get("cite") or C.make_cite(base["title"], base["url"], base["kind"], base.get("page"),
                                                   base.get("heading_path"))
    return Source(**base)


def test_make_cite_web_file_and_page():
    assert C.make_cite("Bài A", "u", "text", None, ["Mục 1", "Mục 1.1"]) == "Bài A › Mục 1 › Mục 1.1"
    assert C.make_cite("Quy chế", "https://x/media/q.pdf", "file", 7) == "Quy chế — trang 7"
    assert C.make_cite("", "https://x/media/Quy%20che.pdf", "file", None) == "Quy che.pdf"      # thiếu tên → tên file
    [s] = api.text_sources([{"url": "https://x/media/q.pdf", "title": "Quy chế", "content": "n", "score": 1,
                             "doc_type": "attachment", "page": 7}])
    assert s.kind == "file" and s.page == 7 and s.cite == "Quy chế — trang 7"


def test_used_ignores_out_of_range_and_duplicates():
    assert C.used("a [2] b [1] c [2] d [9] e [0]", 3) == [2, 1]


def test_enforce_keeps_valid_citations_and_drops_phantoms():
    srcs = [_src(), _src(url="https://x/media/q.pdf", title="Quy chế", kind="file", page=4)]
    ans, cites = C.enforce("Học phí là 500k [1]. Xem thêm [2][7].", srcs)
    assert ans == "Học phí là 500k [1]. Xem thêm [2]." and [c["n"] for c in cites] == [1, 2]
    assert cites[1]["page"] == 4 and cites[1]["cite"] == "Quy chế — trang 4"


def test_enforce_appends_sources_when_llm_did_not_cite():
    srcs = [_src(), _src(url="https://x/media/q.pdf", title="Quy chế", kind="file", page=4), _src(title="C"), _src(title="D")]
    ans, cites = C.enforce("Học phí là 500k.", srcs)
    assert ans.startswith("Học phí là 500k.") and "\n\nNguồn:\n[1] Bài A — https://sict.haui.edu.vn/vn/a/1" in ans
    assert "[2] Quy chế — trang 4 — https://x/media/q.pdf" in ans
    assert [c["n"] for c in cites] == [1, 2, 3]                       # tối đa FOOTER_SOURCES nguồn
    ans2, cites2 = C.enforce("Học phí [9].", srcs[:1])                  # chỉ có [9] ảo → coi như chưa trích dẫn
    assert "[9]" not in ans2 and "[1] Bài A" in ans2 and [c["n"] for c in cites2] == [1]


def test_enforce_no_sources_no_requirement():
    assert C.enforce("Không tìm thấy thông tin.", []) == ("Không tìm thấy thông tin.", [])


def test_index_matches_context_numbering():
    t, m = [_src(), _src(title="B")], [_src(title="Ảnh", kind="image")]
    ctx = api.build_context(t, m)
    idx = C.index(t + m)
    assert [e["n"] for e in idx] == [1, 2, 3] and "[3] (image) Ảnh" in ctx and idx[2]["title"] == "Ảnh"


def test_chat_stream_emits_citations_and_footer(monkeypatch):
    from fastapi.testclient import TestClient
    from sict_crawler import agent, vlm
    src = _src(url="https://x/media/q.pdf", title="Quy chế", kind="file", page=4)
    monkeypatch.setattr(agent, "run", lambda *a, **k: {
        "text": [src], "media": [], "context": "[1] (file) Quy chế — trang 4\nx", "attempts": 1, "sufficient": True})
    monkeypatch.setattr(vlm, "stream_answer", lambda *a, **k: iter(["Học phí ", "500k."]))
    with TestClient(api.app) as c:
        body = c.post("/chat", json={"question": "học phí?"}).text
    events = [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ") and x != "data: [DONE]"]
    said = "".join(e.get("delta", "") for e in events)
    assert said.startswith("Học phí 500k.") and "Nguồn:\n[1] Quy chế — trang 4" in said     # LLM không trích dẫn → server ghép
    cit = next(e for e in events if "citations" in e)["citations"]
    assert cit[0]["page"] == 4 and cit[0]["n"] == 1
    assert body.rstrip().endswith("data: [DONE]")


def test_chat_stream_keeps_llm_citation(monkeypatch):
    from fastapi.testclient import TestClient
    from sict_crawler import agent, vlm
    monkeypatch.setattr(agent, "run", lambda *a, **k: {
        "text": [_src()], "media": [], "context": "[1] (text) Bài A\nx", "attempts": 1, "sufficient": True})
    monkeypatch.setattr(vlm, "stream_answer", lambda *a, **k: iter(["Đúng [1]."]))
    with TestClient(api.app) as c:
        body = c.post("/chat", json={"question": "q"}).text
    assert "Nguồn:" not in body and '"n": 1' in body


def test_chunks_around_block_prefers_containing_then_neighbours(monkeypatch):
    chunks = [{"chunk_id": "a", "block_range": [0, 3], "kind": "text"}, {"chunk_id": "b", "block_range": [6, 9], "kind": "text"},
              {"chunk_id": "c", "block_range": [10, 12], "kind": "table"}, {"chunk_id": "cap", "kind": "media_caption"}]
    monkeypatch.setattr(vector_store, "scroll_text", lambda *a, **k: [dict(c) for c in chunks])
    assert [c["chunk_id"] for c in api._chunks_around(1, 7)] == ["b"]                 # block nằm trong chunk b
    assert [c["chunk_id"] for c in api._chunks_around(1, 5)] == ["a", "b"]            # ảnh ở khe giữa a và b → liền trước + liền sau
    assert [c["chunk_id"] for c in api._chunks_around(1, 20)] == ["c"]                # sau chunk cuối: chỉ chunk liền trước


def test_image_block_from_caption_payload(monkeypatch):
    seen = {}
    def scroll(flt, limit=200, collection=None):
        seen["keys"] = sorted(c.key for c in flt.must)
        return [{"block_idx": None}, {"block_idx": 5}]
    monkeypatch.setattr(vector_store, "scroll_text", scroll)
    assert api._image_block("sha", 3) == 5 and seen["keys"] == ["document_id", "kind", "media_key"]
    assert api._image_block(None, 3) is None


def test_linked_hits_use_block_neighbours_before_similarity(monkeypatch):
    monkeypatch.setattr(api, "_image_block", lambda mk, d: 7 if mk == "sim1" else None)
    monkeypatch.setattr(api, "_chunks_around", lambda d, b, limit=2: [{"chunk_id": f"near{d}-{b}", "url": "u", "score": 0.0}])
    monkeypatch.setattr(vector_store, "search_text", lambda v, limit=2, filter_document_id=None, query_filter=None, collection=None: [
        {"chunk_id": f"sim{filter_document_id}", "url": "u2", "score": 0.7, "kind": "text"}])
    kept = [{"modality": "i2i", "media_key": "sim1", "document_id": [5]},
            {"modality": "i2i", "media_key": "old", "document_id": [6]}, {"modality": "clip", "media_key": "z", "document_id": [8]}]
    out = api._linked_doc_hits([0.0, 1.0], kept, None)
    assert [h["chunk_id"] for h in out] == ["near5-7", "sim6"] and all(h["via"] == "i2i-doc" for h in out)
