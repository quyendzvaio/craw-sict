"""D24: trích dẫn bắt buộc — nhãn (file → trang), enforce, SSE /chat, ngữ cảnh gần ảnh theo block."""
import json

import pytest

from sict_crawler.api import routes as api
from sict_crawler.model.schemas import Source
from sict_crawler.repository import vector_store
from sict_crawler.service.retrieval import citations as C
from sict_crawler.service.retrieval import retriever, sources


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
    [s] = sources.text_sources([{"url": "https://x/media/q.pdf", "title": "Quy chế", "content": "n", "score": 1,
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
    ctx = sources.build_context(t, m)
    idx = C.index(t + m)
    assert [e["n"] for e in idx] == [1, 2, 3] and "Bài: Ảnh — 1 ảnh\n[3] (image)" in ctx and idx[2]["title"] == "Ảnh"


def test_chat_stream_emits_citations_and_footer(monkeypatch):
    from fastapi.testclient import TestClient

    from sict_crawler.service.retrieval import agent, vlm
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

    from sict_crawler.service.retrieval import agent, vlm
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
    assert [c["chunk_id"] for c in retriever._chunks_around(1, 7)] == ["b"]                 # block nằm trong chunk b
    assert [c["chunk_id"] for c in retriever._chunks_around(1, 5)] == ["a", "b"]            # ảnh ở khe giữa a và b → liền trước + liền sau
    assert [c["chunk_id"] for c in retriever._chunks_around(1, 20)] == ["c"]                # sau chunk cuối: chỉ chunk liền trước


def test_image_block_from_caption_payload(monkeypatch):
    seen = {}
    def scroll(flt, limit=200, collection=None):
        seen["keys"] = sorted(c.key for c in flt.must)
        return [{"block_idx": None}, {"block_idx": 5}]
    monkeypatch.setattr(vector_store, "scroll_text", scroll)
    assert retriever._image_block("sha", 3) == 5 and seen["keys"] == ["document_id", "kind", "media_key"]
    assert retriever._image_block(None, 3) is None


def test_linked_hits_use_block_neighbours_before_similarity(monkeypatch):
    monkeypatch.setattr(retriever, "_image_block", lambda mk, d: 7 if mk == "sim1" else None)
    monkeypatch.setattr(retriever, "_chunks_around", lambda d, b, limit=2: [{"chunk_id": f"near{d}-{b}", "url": "u", "score": 0.0}])
    monkeypatch.setattr(vector_store, "search_text", lambda v, limit=2, filter_document_id=None, query_filter=None, collection=None: [
        {"chunk_id": f"sim{filter_document_id}", "url": "u2", "score": 0.7, "kind": "text"}])
    kept = [{"modality": "i2i", "media_key": "sim1", "document_id": [5]},
            {"modality": "i2i", "media_key": "old", "document_id": [6]}, {"modality": "clip", "media_key": "z", "document_id": [8]}]
    out = retriever._linked_doc_hits([0.0, 1.0], kept, None)
    assert [h["chunk_id"] for h in out] == ["near5-7", "sim6"] and all(h["via"] == "i2i-doc" for h in out)


def test_enforce_accepts_fullwidth_brackets():
    """gpt-oss trích dẫn bằng 【3】 / ［3］ (ngoặc toàn khổ): đổi về [3], không ghép thêm dòng "Nguồn:" (golden_v2
    2026-11-01: 23/88 câu trả lời dùng 【n】 và đều bị ghép footer dù đã trích dẫn)."""
    srcs = [_src(), _src(title="B"), _src(title="C")]
    ans, cites = C.enforce("Quyết định 2536/QĐ-TTg【3】, xem thêm ［1］ và 【9】.", srcs)
    assert ans == "Quyết định 2536/QĐ-TTg[3], xem thêm [1] và ." and "Nguồn:" not in ans
    assert [c["n"] for c in cites] == [3, 1]
    assert C.used("a【2】 b［1］", 3) == [2, 1]
    # gpt-oss còn kèm số dòng: 【2†L1-L4】 (golden_v2 g032 2026-11-04 bị ghép footer dù đã trích dẫn)
    ans, cites = C.enforce("Có 3 đội【2†L1-L4】【1†L1-L3】.", srcs)
    assert ans == "Có 3 đội[2][1]." and "Nguồn:" not in ans


def test_enforce_accepts_nguon_bracket():
    """gpt-oss còn viết "[Nguồn: 6, 7]" thay cho [6][7] (g061/g065 2026-11-04) → bị coi là không trích dẫn, ghép footer."""
    srcs = [_src(title=str(i)) for i in range(8)]
    ans, cites = C.enforce("Ảnh lễ. [Nguồn: 6,\u202f7] và [nguồn 3].", srcs)
    assert ans == "Ảnh lễ. [6][7] và [3]." and [c["n"] for c in cites] == [6, 7, 3] and "Nguồn:" not in ans


def test_enforce_drops_placeholder_n():
    """Model chép nguyên chỗ giữ chỗ "[n]" của prompt ("Video recap: [6]   [n]", golden_v2 g065 2026-11-04)."""
    ans, _ = C.enforce("Video recap: [1]   [n]", [_src()])
    assert ans == "Video recap: [1]"


def test_enforce_normalizes_fullwidth_punctuation():
    """gpt-oss-120b-sale chấm câu bằng 。 (2026-11-04: "… gần 6.000 người [4]。 (Các số khác …"): đổi về dấu ASCII,
    thêm khoảng trắng khi dấu dính liền chữ sau; không đụng số thập phân."""
    srcs = [_src(), _src(title="B"), _src(title="C"), _src(title="D")]
    ans, cites = C.enforce("Khoảng 6.000 người [4]。 （Ước tính）：25.22 điểm【3】。Hết！", srcs)
    assert ans == "Khoảng 6.000 người [4]. (Ước tính): 25.22 điểm[3]. Hết!"
    assert [c["n"] for c in cites] == [4, 3]


def _cap(title, ocr, n=0, parent="https://sict.haui.edu.vn/vn/tin-tuc/le/1", kind="image"):
    cap = f"{title} | Tin tức | 26/08/2024. {title}." + (f" {ocr}" if ocr else "")
    return _src(url=f"https://sict.haui.edu.vn/media/{n}.jpg", title=title, kind=kind, content=cap, excerpt=cap,
                parent_url=parent, published_at="2024-08-26T08:00:00+07:00")


def test_media_context_grouped_by_post_with_ocr_label():
    """2026-11-04: mỗi ảnh lặp tiêu đề bài 3 lần, OCR không gắn nhãn → LLM mô tả 6 ảnh một bài thành 6 ý và chép
    link OCR sai. Giờ: một khối mỗi bài, mỗi ảnh một dòng [n] chỉ với chữ OCR, gắn nhãn có thể sai."""
    t = [_src()]
    m = [_cap("Lễ Tốt nghiệp 2024", "", 1), _cap("Hướng dẫn dự lễ", "https://tracuu.euni.hqui.edu.vn", 2,
                                                parent="https://sict.haui.edu.vn/vn/thong-bao/hd/2"),
         _cap("Lễ Tốt nghiệp 2024", "Hà Nội, ngày 24 tháng 8", 3)]
    ctx = sources.build_context(t, m)
    parts = ctx.split("\n\n---\n\n")
    assert len(parts) == 3 and parts[0].startswith("[1] (text) Bài A")
    assert parts[1].splitlines() == [
        "Bài: Lễ Tốt nghiệp 2024 — đăng ngày 26/08/2024 — 2 ảnh",
        "[2] (image) không có chữ đọc được",
        "[4] (image) chữ đọc từ ảnh (OCR, có thể sai): Hà Nội, ngày 24 tháng 8"]
    assert parts[2].splitlines()[1] == "[3] (image) chữ đọc từ ảnh (OCR, có thể sai): https://tracuu.euni.hqui.edu.vn"
    assert ctx.count("Lễ Tốt nghiệp 2024") == 1
    assert [e["n"] for e in C.index(t + m)] == [1, 2, 3, 4]       # số [n] không đổi


def test_media_header_counts_kinds_and_uses_post_title():
    """Header dùng tên bài (không kèm mục) và ghi rõ "đăng ngày": g064 2026-11-04 header "… › Các cuộc thi công
    nghệ: (30/03/2025)" → model nói video "không có ngày đăng"."""
    v = _src(url="https://youtu.be/x", title="IT FESTIVAL 2025", kind="video", content="Video: Bản tin. a b",
             parent_url="https://sict.haui.edu.vn/vn/tin-tuc/it/3", published_at="2025-03-30T08:00:00+07:00")
    v.cite = "IT FESTIVAL 2025 › 2. Chuỗi sự kiện › Các cuộc thi:"
    i = _cap("IT FESTIVAL 2025", "TRƯỜNG ĐẠI HỌC", 1, parent="https://sict.haui.edu.vn/vn/tin-tuc/it/3")
    ctx = sources.build_context([], [v, i])
    assert ctx.splitlines()[0] == "Bài: IT FESTIVAL 2025 — đăng ngày 30/03/2025 — 1 video, 1 ảnh"


def test_media_text_keeps_caption_without_header():
    s = _src(title="Bài X", kind="video", content="Video: Giới thiệu. phụ đề a b")
    assert sources.media_text(s) == "Video: Giới thiệu. phụ đề a b"
    assert sources.media_text(_cap("A. B", "chữ", 1)) == "chữ"


def test_unverified_links_lose_link_form():
    t = [_src(content="Tra cứu tại https://tracuu.euni.haui.edu.vn/tracuu hoặc [web](https://sict.haui.edu.vn/)")]
    m = [_cap("Hướng dẫn", "https://tracuu.euni.hqui.edu.vn/tracuu", 1)]
    ans, _ = C.enforce("Vào https://tracuu.euni.hqui.edu.vn/tracuu [1], [trang tra cứu](https://tracuu.euni.hqui.edu.vn/x) "
                       "hoặc https://tracuu.euni.haui.edu.vn/tracuu/ và [SICT](https://sict.haui.edu.vn) [2]. "
                       "Ảnh: https://sict.haui.edu.vn/media/1.jpg.", t + m)
    assert ans == ("Vào `https://tracuu.euni.hqui.edu.vn/tracuu` [1], trang tra cứu "
                   "hoặc https://tracuu.euni.haui.edu.vn/tracuu/ và [SICT](https://sict.haui.edu.vn) [2]. "
                   "Ảnh: https://sict.haui.edu.vn/media/1.jpg.")


def test_answer_rules_on_media_and_ocr():
    from sict_crawler.service.retrieval import vlm
    r = vlm.ANSWER_RULES
    assert "gom theo bài" in r and "không mô tả lặp lại từng ảnh" in r and "không chép đường link" in r


def test_citation_runs_deduped():
    assert C.dedupe_runs("Ảnh kèm: [9] , [10] [9][10]\n\nB [3], [4]. C [5] và [5]") == \
        "Ảnh kèm: [9][10]\n\nB [3][4]. C [5] và [5]"
