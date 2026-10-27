"""Guardrail truy xuất không phụ thuộc LLM (D27): đa dạng tài liệu, câu hỏi liệt kê, câu gốc luôn được tìm,
ngữ cảnh đủ nội dung trong ngân sách, tín hiệu "đủ bằng chứng" tính bằng luật. Fake Qdrant/embedder, không cần dịch vụ."""
import pytest

from sict_crawler.core.config import get_settings
from sict_crawler.model.schemas import Source
from sict_crawler.repository import cache, vector_store
from sict_crawler.service.embedding import embedder
from sict_crawler.service.retrieval import agent, query_rewriter, retriever, router, sources


def _hit(doc, n, via=None):
    h = {"document_id": doc, "chunk_id": f"{doc}-{n}", "url": f"https://x/{doc}"}
    if via:
        h["via"] = via
    return h


# --- đa dạng tài liệu ---------------------------------------------------------------------------------------------

def test_diversify_caps_chunks_per_doc_and_keeps_order():
    hits = [_hit(1, 1), _hit(1, 2), _hit(1, 3), _hit(2, 1), _hit(2, 2), _hit(3, 1)]
    out = retriever.diversify(hits, top_k=4, per_doc=1)
    assert [h["chunk_id"] for h in out] == ["1-1", "2-1", "3-1", "1-2", "1-3", "2-2"]   # 3 bài khác nhau lên trước
    assert len(out) == len(hits)                                   # không mất hit: phần bị đẩy xếp sau


def test_diversify_fills_top_k_when_few_docs():
    hits = [_hit(1, 1), _hit(1, 2), _hit(1, 3)]
    out = retriever.diversify(hits, top_k=3, per_doc=2)
    assert [h["chunk_id"] for h in out] == ["1-1", "1-2", "1-3"]   # thiếu bài khác → đoạn thừa lấp chỗ, giữ thứ tự


def test_diversify_does_not_cap_image_linked_chunks():
    hits = [_hit(5, 1, "i2i-doc"), _hit(5, 2, "i2i-doc"), _hit(5, 3, "i2i-doc"), _hit(6, 1)]
    out = retriever.diversify(hits, top_k=4, per_doc=1)
    assert [h["chunk_id"] for h in out] == ["5-1", "5-2", "5-3", "6-1"]


def test_dominant_doc_needs_majority_of_top_hits():
    hits = [_hit(7, i) for i in range(6)] + [_hit(i, 1) for i in range(1, 5)]
    assert retriever.dominant_doc(hits, min_hits=6) == 7
    assert retriever.dominant_doc(hits, min_hits=7) is None
    assert retriever.dominant_doc(hits, min_hits=0) is None                        # 0 = tắt
    assert retriever.dominant_doc([_hit(1, 1)] + hits, min_hits=6) is None          # đứng đầu phải là bài áp đảo


def test_diversify_lets_dominant_doc_take_more_chunks():
    hits = [_hit(7, i) for i in range(1, 7)] + [_hit(1, 1), _hit(2, 1), _hit(3, 1)]
    out = retriever.diversify(hits, top_k=5, per_doc=2, dominant=7, dominant_max=4)
    assert [h["chunk_id"] for h in out[:5]] == ["7-1", "7-2", "7-3", "7-4", "1-1"]
    listq = retriever.diversify(hits, top_k=5, per_doc=1, dominant=7, dominant_max=4)    # câu liệt kê: các bài khác 1 đoạn
    assert [h["chunk_id"] for h in listq[:5]] == ["7-1", "7-2", "7-3", "7-4", "1-1"]
    assert [h["chunk_id"] for h in retriever.diversify(hits, 5, 2)[:5]] == ["7-1", "7-2", "1-1", "2-1", "3-1"]


def test_diversify_media_points_with_doc_lists():
    hits = [{"document_id": [1, 2], "chunk_id": "a"}, {"document_id": [1, 2], "chunk_id": "b"}, _hit(3, 1)]
    out = retriever.diversify(hits, top_k=2, per_doc=1)
    assert [h["chunk_id"] for h in out[:2]] == ["a", "3-1"]


# --- câu hỏi liệt kê ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "Trường có những ngành đào tạo nào?", "SICT đào tạo các chương trình đại học nào?",
    "Danh sách các phó giáo sư của trường", "Ban giám hiệu trường gồm những ai?",
    "Liệt kê các câu lạc bộ sinh viên", "Trường có bao nhiêu khoa?", "truong co nhung nganh nao",
    "Năm 2025 trường xét tuyển đại học theo những phương thức nào?",
])
def test_list_question_positive(q):
    assert router.is_list_question(q)


@pytest.mark.parametrize("q", [
    "Học phí năm 2025 bao nhiêu?", "Thầy Phạm Văn Bổng là ai?", "CLB HIT là gì?", "Lịch thi học kỳ 1 năm 2025-2026?",
    "Ngành nào có điểm chuẩn cao nhất?", "Cho tôi xem ảnh lễ tốt nghiệp", "Những điều cần biết khi nhập học", "",
])
def test_list_question_negative(q):
    assert not router.is_list_question(q)


# --- ngữ cảnh: nội dung đầy đủ trong ngân sách --------------------------------------------------------------------

def _src(i, content, kind="text"):
    return Source(url=f"https://x/{i}", title=f"Bài {i}", kind=kind, score=0.5, excerpt=content[:300],
                  content=content, cite=f"Bài {i}")


def test_context_uses_full_content_not_excerpt():
    ctx = sources.build_context([_src(1, "a" * 280 + " ĐUÔI-QUAN-TRỌNG")], [], source_chars=1500, context_chars=9000)
    assert "ĐUÔI-QUAN-TRỌNG" in ctx                                # trước đây LLM chỉ thấy 300 ký tự đầu


def test_context_budget_keeps_every_source():
    srcs = [_src(i, f"nội dung {i} " + "x" * 3000) for i in range(1, 11)]
    ctx = sources.build_context(srcs, [], source_chars=1500, context_chars=9000)
    assert all(f"[{i}] (text) Bài {i}" in ctx for i in range(1, 11))   # đủ 10 nguồn, không cắt mất nguồn cuối
    assert len(ctx) < 9000 + 10 * 120                              # tổng ≈ ngân sách (+ dòng nhãn)
    one = sources.build_context(srcs[:1], [], source_chars=1500, context_chars=9000)
    assert 1500 <= len(one) < 1700                                 # 1 nguồn: trần mỗi nguồn


def test_list_question_gets_full_budget_per_source(monkeypatch):
    # 10 nguồn, mục cần liệt kê nằm ở ký tự 1000–1400 của mỗi đoạn: ngân sách thường (9000 / 10 = 900) cắt mất
    srcs = [_src(i, "x" * 1000 + f" MỤC-{i} " + "y" * 2000) for i in range(1, 11)]
    monkeypatch.setattr(get_settings().llm, "source_chars", 1500)
    monkeypatch.setattr(get_settings().llm, "context_chars", 9000)
    monkeypatch.setattr(get_settings().llm, "list_context_chars", 15000)

    def run(list_question):
        route = router.Route(list_question=list_question)
        monkeypatch.setattr(retriever, "retrieve", lambda *a, **k: (srcs, [], route))
        return agent.run("Trường có những ngành nào?", 5)["context"]
    assert all(f"MỤC-{i}" in run(True) for i in range(1, 11))
    assert not any(f"MỤC-{i}" in run(False) for i in range(1, 11))


def test_context_falls_back_to_excerpt_for_old_cache_entries():
    s = Source(url="u", title="t", kind="text", score=0.1, excerpt="trích ngắn", cite="t")   # bản cache cũ: không content
    assert "trích ngắn" in sources.build_context([s], [])


def test_http_responses_do_not_expose_full_content(monkeypatch):
    import asyncio

    import httpx

    from sict_crawler.api import routes as api

    big = _src(1, "BÍ-MẬT-NỘI-DUNG " * 50)
    monkeypatch.setattr(agent, "run", lambda *a, **k: {"context": "ctx", "text": [big], "media": [], "attempts": 1,
                                                       "sufficient": True})

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://t") as c:
            return (await c.post("/query", json={"question": "q"})).json()
    body = asyncio.run(go())
    assert "content" not in body["text"][0] and body["text"][0]["excerpt"]


# --- tín hiệu đủ bằng chứng (thay LLM chấm) ----------------------------------------------------------------------

def test_evidence_requires_question_terms_in_context(monkeypatch):
    monkeypatch.setattr(get_settings().retrieval, "min_term_cover", 0.6)
    monkeypatch.setattr(get_settings().retrieval, "min_cosine", 0.3)
    s = [_src(1, "Thông báo thu học phí học kỳ phụ năm 2025")]
    s[0].cosine = 0.6
    ok = agent.evidence("Học phí năm 2025 bao nhiêu?", s, [], sources.build_context(s, []))
    assert ok["sufficient"] and ok["cover"] == 1.0
    off = agent.evidence("Giá vé xem phim CGV hôm nay", s, [], sources.build_context(s, []))
    assert not off["sufficient"] and off["cover"] < 0.6


def test_evidence_low_cosine_is_insufficient(monkeypatch):
    monkeypatch.setattr(get_settings().retrieval, "min_cosine", 0.3)
    s = [_src(1, "học phí năm 2025")]
    s[0].cosine = 0.1
    assert not agent.evidence("học phí năm 2025", s, [], sources.build_context(s, []))["sufficient"]


def test_source_cosine_from_dense_score_when_not_reranked():
    s = sources.text_sources([{"url": "u", "title": "t", "score": 0.02, "content": "c", "dense_score": 0.61}])[0]
    assert s.cosine == 0.61                                        # v2/v3: rerank_text chỉ chấm caption
    b = sources.text_sources([{"url": "u", "title": "t", "score": 0.02, "content": "c"}])[0]
    assert b.cosine is None                                        # hit chỉ từ BM25


def test_evidence_without_sources_or_image_only():
    assert not agent.evidence("học phí?", [], [], "")["sufficient"]
    img = [Source(url="u", title="t", kind="image", score=0.9)]
    assert agent.evidence("", [], img, "")["sufficient"]                # chỉ ảnh, có ảnh giống → đủ


# --- câu gốc luôn được tìm, câu LLM viết lại chỉ bổ sung ------------------------------------------------------------

@pytest.fixture
def fake_search(monkeypatch):
    calls = {"dense": [], "sparse": []}
    monkeypatch.setattr(get_settings().embedding, "backend", "api")
    monkeypatch.setattr(cache, "get", lambda k: None)
    monkeypatch.setattr(cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(embedder, "embed_query", lambda q: [float(len(q)), 1.0])
    monkeypatch.setattr(vector_store, "has_sparse", lambda *a, **k: True)

    def dense(vec, limit=8, filter_document_id=None, query_filter=None, collection=None):
        calls["dense"].append(vec)
        doc = int(vec[0])                                         # mỗi câu hỏi (độ dài khác nhau) ra một bài khác
        return [{"url": f"https://x/{doc}", "title": "t", "content": "c", "score": 0.8, "document_id": doc,
                 "chunk_id": f"d{doc}", "kind": "text"}]

    def sparse(text, terms, limit=8, query_filter=None, collection=None):
        calls["sparse"].append(text)
        return []
    monkeypatch.setattr(vector_store, "search_text", dense)
    monkeypatch.setattr(vector_store, "keyword_search", sparse)
    monkeypatch.setattr(vector_store, "point_vectors", lambda need: {})
    return calls


def test_retrieve_searches_original_and_rewritten(monkeypatch, fake_search):
    q, rw = "nganh dao tao?", "Các ngành đào tạo đại học của Trường Công nghệ thông tin và Truyền thông"
    monkeypatch.setattr(query_rewriter, "rewrite", lambda x: {"rewritten": rw, "terms": rw.split(), "route_llm": None})
    t, _, _ = retriever.retrieve(q, 5)
    assert sorted(fake_search["sparse"]) == sorted([q, rw])        # BM25 cho cả câu gốc lẫn câu viết lại (song song)
    assert {int(v[0]) for v in fake_search["dense"]} == {len(q), len(rw)}
    assert {s.url for s in t} == {f"https://x/{len(q)}", f"https://x/{len(rw)}"}


def test_retrieve_single_pass_when_rewrite_equals_question(monkeypatch, fake_search):
    q = "Học phí năm 2025"
    monkeypatch.setattr(query_rewriter, "rewrite", lambda x: {"rewritten": "hoc phi nam 2025", "terms": [],
                                                              "route_llm": None})
    retriever.retrieve(q, 5)
    assert fake_search["sparse"] == [q] and len(fake_search["dense"]) == 1   # giống nhau sau khi bỏ dấu → 1 lượt


def test_retrieve_puts_latest_docs_first_for_section_plus_recency(monkeypatch, fake_search):
    monkeypatch.setattr(query_rewriter, "rewrite", lambda x: {"rewritten": x, "terms": [], "route_llm": None})
    seen = {}

    def latest(sections, limit=10, since_year=None):
        seen.update(sections=sections, limit=limit)
        return [{"url": f"https://x/new{i}", "title": f"Mới {i}", "content": "c", "score": 0.0, "document_id": 900 + i,
                 "chunk_id": f"n{i}", "kind": "text", "published_at": f"2026-10-0{9 - i}"} for i in range(3)]
    monkeypatch.setattr(vector_store, "latest_text", latest)
    t, _, _ = retriever.retrieve("Thông báo mới nhất của trường là gì?", 5)
    assert [s.url for s in t[:3]] == ["https://x/new0", "https://x/new1", "https://x/new2"]
    assert seen["sections"] == ["thong-bao"] and t[0].via == "latest"
    seen.clear()
    retriever.retrieve("Học phí năm 2025", 5)
    assert not seen                                                # câu có nội dung riêng: không đụng tới theo ngày


def test_retrieve_widens_list_questions(monkeypatch, fake_search):
    monkeypatch.setattr(query_rewriter, "rewrite", lambda x: {"rewritten": x, "terms": [], "route_llm": None})
    monkeypatch.setattr(get_settings().retrieval, "list_top_k", 10)
    _, _, route = retriever.retrieve("Trường có những ngành đào tạo nào?", 5)
    assert route.list_question
    _, _, route2 = retriever.retrieve("Học phí năm 2025", 5)
    assert not route2.list_question


# --- model suy luận: hết max_tokens trong phần suy nghĩ ----------------------------------------------------------

def _fake_stream(monkeypatch, lines):
    from sict_crawler.service.retrieval import vlm

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter(lines)

    class C(R):
        def stream(self, *a, **k):
            return R()
    monkeypatch.setattr(get_settings().llm, "api_key", "k")
    monkeypatch.setattr(vlm, "_client", lambda *a, **k: C())
    return vlm


def test_stream_answer_reasoning_only_yields_nothing_and_says_why(monkeypatch, capsys):
    import json
    think = 'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "đang nghĩ…"}}]})
    vlm = _fake_stream(monkeypatch, [think, think, "data: [DONE]"])
    assert list(vlm.stream_answer("q", "ctx")) == []                    # caller echo ngữ cảnh như khi lỗi
    assert "max_new_tokens" in capsys.readouterr().out


def test_stream_answer_skips_reasoning_and_yields_content(monkeypatch, capsys):
    import json
    vlm = _fake_stream(monkeypatch, [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "nghĩ"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Trả lời [1]."}}]}), "data: [DONE]"])
    assert list(vlm.stream_answer("q", "ctx")) == ["Trả lời [1]."]
    assert "max_new_tokens" not in capsys.readouterr().out
