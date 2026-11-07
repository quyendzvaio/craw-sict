"""Router D17: ý định (luật/LLM) + chặn theo bằng chứng (gate) + nối vào retriever.retrieve.

Run: python -m tests.test_router  |  pytest tests/test_router.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.service.retrieval import router as R
from sict_crawler.service.retrieval.vlm import parse_route_json

GOLDEN = Path(__file__).resolve().parent.parent / "eval" / "golden_v1.jsonl"


def test_rules_examples():
    assert R.classify_rules("Học phí ngành CNTT bao nhiêu?").media == "none"
    assert R.classify_rules("Cho xem ảnh lễ khai giảng 2025").media == "required"
    assert R.classify_rules("Video giới thiệu trường").media == "required"
    assert R.classify_rules("Ảnh các sản phẩm báo tường IT Festival").media == "required"
    assert R.classify_rules("Hội thảo Deep Learning tổ chức khi nào?").media == "optional"
    # "tiếng Anh" không phải "ảnh"; "tại" không phải "tải"
    assert R.classify_rules("Chương trình dạy bằng tiếng Anh của trường?").media == "none"
    assert not R.classify_rules("Trụ sở trường tại đâu?").wants_file
    assert R.classify_rules("TS. Lê Nguyên Khôi tốt nghiệp ở đâu?").media == "none"  # họ Lê ≠ lễ
    assert R.classify_rules("Lễ tốt nghiệp năm 2025").media == "optional"
    assert R.classify_rules("Tải quy chế đào tạo tín chỉ").wants_file


def test_wants_file_expanded_phrases():
    """Task 2: 9 câu t4 + t1-18 Phase 0 trượt vì thiếu mẫu từ — mở rộng _FILE (giữ trần
    quy định/quyết định vì golden cần; miss còn lại t4-05 + t5-09 chấp nhận, xem ledger)."""
    for q in ["Tải kế hoạch học kỳ hè 2026 ở đâu?",
              "kế hoạch học kỳ 2 năm học 2024-2025",
              "Danh sách doanh nghiệp tham gia Ngày hội việc làm 2024",
              "tai danh sach mon hoc hoc phan thay the",
              "Thông báo tuyển sinh đào tạo trình độ thạc sĩ năm 2022 chi tiết",
              "đơn xin thực tập tại doanh nghiệp tải ở đâu",
              "phiếu đăng ký sinh viên nghiên cứu khoa học",
              "đơn đăng ký xin học bổng Digiworld",
              "hướng dẫn sử dụng module thông tin thực tập",
              "Mẫu sơ yếu lý lịch học bổng Brother 2024",
              "so do hoi truong le tot nghiep 2024",
              "Quyết định 721/QĐ-ĐHCN về đánh giá kết quả học tập",
              "quyết định trưởng chương trình đào tạo trình độ đại học"]:
        assert R.classify_rules(q).wants_file, q


def test_wants_file_no_false_positive():
    # câu hỏi nội dung/tồn tại không đòi file; collision "tại"→"tải" trong câu chatty (t5-05).
    # LƯU Ý: "quy định/quy chế" trần VẪN match (golden q17–q20 cần trần để qua) — t5-09/g052/q69
    # là miss chấp nhận (mở rộng ngữ cảnh làm golden gãy), nên KHÔNG đưa trần vào neg list.
    for q in ["Trường Đại học Công nghiệp Hà Nội chuyển thành Đại học Công nghiệp Hà Nội",
              "Trung tâm Nghiên cứu và Ứng dụng Trí tuệ nhân tạo thành lập",
              "đề cương môn học tại khoa CNTT tải ở đâu vậy ad",
              "Trụ sở trường tại đâu?"]:
        assert not R.classify_rules(q).wants_file, q
    # các mẫu cũ giữ nguyên
    for q in ["tải về biểu mẫu đánh giá", "quy chế tuyển sinh", "thời khoá biểu lớp CNTT1",
              "download quy dinh chuan dau ra ngoai ngu",
              "tai quy che dao tao dai hoc tin chi o dau",
              "quyết định trưởng chương trình đào tạo trình độ đại học",
              "Quyết định 721/QĐ-ĐHCN về đánh giá kết quả học tập"]:
        assert R.classify_rules(q).wants_file, q


def test_image_input_not_media_request():
    r = R.classify_rules("Sự kiện trong ảnh này trao giải gì?", has_image=True)
    assert r.media == "optional"
    assert "image" in r.inputs
    assert R.classify_rules("ảnh lễ tốt nghiệp").media == "required"  # hành vi cũ
    r2 = R.classify_rules("cho xem thêm ảnh lễ tốt nghiệp", has_image=True)
    assert r2.media == "required"  # đòi XEM dù có ảnh kèm → vẫn required
    r3 = R.classify_rules("Đây là ảnh của trường nào?", has_image=True)
    assert r3.media == "optional" and "image" in r3.inputs  # "ảnh của" = ảnh đầu vào
    r4 = R.classify_rules("xem thêm ảnh của ngày hội này", has_image=True)
    assert r4.media == "required"  # "xem thêm ảnh" vẫn là đòi xem


def test_image_input_with_explicit_request_for_more_images():
    for q in ("Tìm ảnh của lễ tốt nghiệp này", "Gửi thêm ảnh của sự kiện trong ảnh này",
              "Cho tôi ảnh của ngày hội này", "Tìm thêm ảnh khác của sự kiện trong ảnh này"):
        assert R.classify_rules(q, has_image=True).media == "required", q
    for q in ("Tìm ảnh này được chụp tại đâu?", "Ảnh này thuộc sự kiện nào?"):
        assert R.classify_rules(q, has_image=True).media == "optional", q


def test_rules_on_golden_set():
    """Luật dự phòng phải bắt chắc 2 đầu: câu `required` và câu muốn file. Câu `optional` khó
    bằng luật (LLM lo) nên chỉ đo, không ép."""
    rows = [json.loads(x) for x in GOLDEN.read_text(encoding="utf-8").splitlines() if x.strip()]
    req = [g for g in rows if g["media_mode"] == "required"]
    assert all(R.classify_rules(g["question"]).media == "required" for g in req), \
        [g["id"] for g in req if R.classify_rules(g["question"]).media != "required"]
    none_rows = [g for g in rows if g["media_mode"] == "none"]
    false_required = [g["id"] for g in none_rows if R.classify_rules(g["question"]).media == "required"]
    assert not false_required, false_required  # không bao giờ ép ảnh cho câu thông tin
    files = [g for g in rows if g["wants_file"]]
    assert all(R.classify_rules(g["question"]).wants_file for g in files), \
        [g["id"] for g in files if not R.classify_rules(g["question"]).wants_file]


def test_decide_merges_llm_and_rules():
    assert R.decide("Học phí bao nhiêu?", {"media": "optional", "wants_file": False}).media == "optional"
    # luật thấy yêu cầu xem ảnh rõ ràng → LLM không hạ được
    assert R.decide("Cho xem ảnh hội chợ", {"media": "none", "wants_file": False}).media == "required"
    assert R.decide("Tải quy chế", {"media": "none", "wants_file": False}).wants_file
    assert R.decide("Học phí?", {"media": "weird"}).source == "rules"   # JSON sai → luật
    assert R.decide("Học phí?", None).source == "rules"


def test_parse_route_json():
    raw = 'Kết quả: {"rewritten": "học phí ngành công nghệ thông tin", "media": "None", "wants_file": false} xong'
    d = parse_route_json(raw)
    assert d == {"rewritten": "học phí ngành công nghệ thông tin", "media": "none", "wants_file": False}
    assert parse_route_json("học phí ngành công nghệ thông tin") is None
    assert parse_route_json('{"media": "none"}') is None  # thiếu rewritten


def _img(mid, doc, score, modality="clip", kind="image", url=None):   # doc: id bài hoặc danh sách (point ảnh v2)
    return {"media_id": mid, "document_id": doc, "raw_score": score, "modality": modality,
            "kind": kind, "url": url or f"img{mid}", "score": score or 0.0}


def test_gate_user_image_match_beats_weak_coupled_captions():
    """Truy vấn chỉ có ảnh (đo trên v3 2026-10-20): top bài chữ do caption i2t yếu (~0,38) quyết định; ảnh trùng
    (i2i 0,99) không thuộc bài nào trong đó vẫn phải đứng đầu, không bị 5 caption đẩy khỏi top_k."""
    top = [{"document_id": d} for d in (1316, 1627, 1581)]
    c = [{"modality": "i2t", "raw_score": 0.39 - i / 100, "media_key": f"cap{i}", "kind": "media_caption",
          "media_kind": "image", "document_id": 1316 + i} for i in range(5)]
    c += [{"modality": "i2i", "raw_score": 0.99, "media_key": "same", "kind": "image", "document_id": [1032]},
          {"modality": "i2i", "raw_score": 0.97, "media_key": "alike", "kind": "image", "document_id": [1036]}]
    out = R.gate(R.Route("required"), top, c, 5, clip_min=0.25, caption_min=0.5, i2i_min=0.6, i2t_min=0.3)
    assert [h["media_key"] for h in out][:2] == ["same", "alike"]


def test_gate_none_optional_required():
    text = [{"document_id": 10}, {"document_id": 11}]
    cands = [_img(1, 10, 0.30), _img(2, 10, 0.29), _img(3, 11, 0.27), _img(4, 10, 0.26),
             _img(5, 99, 0.33), _img(6, 10, 0.10)]
    kw = dict(top_k=8, clip_min=0.24, caption_min=0.82, optional_max=3)
    assert R.gate(R.Route("none"), text, cands, **kw) == []
    opt = R.gate(R.Route("optional"), text, cands, **kw)
    assert [h["media_id"] for h in opt] == [1, 2, 4]          # chỉ ảnh của bài đứng đầu, ≤3, đủ ngưỡng
    req = R.gate(R.Route("required"), text, cands, **kw)
    ids = [h["media_id"] for h in req]
    assert 6 not in ids and ids == [1, 2, 4, 3, 5]            # bài đứng đầu trước, ảnh không gắn sau, dưới ngưỡng bị loại
    weak = [_img(7, 10, 0.12), _img(8, 11, 0.15)]
    assert R.gate(R.Route("required"), text, weak, **kw) == []  # không đạt → rỗng, không ảnh sai


def test_gate_video_one_slot_and_caption_threshold():
    text = [{"doc_id": "d1"}]
    frames = [{"doc_id": "d1", "kind": "video_frame", "url": "yt/abc", "media_id": i,
               "modality": "clip", "raw_score": 0.25 + i / 100} for i in range(5)]
    caps = [{"doc_id": "d1", "kind": "media_caption", "media_kind": "image", "media_id": 50,
             "modality": "caption", "raw_score": 0.79},
            {"doc_id": "d1", "kind": "media_caption", "media_kind": "image", "media_id": 51,
             "modality": "caption", "raw_score": 0.86}]
    out = R.gate(R.Route("required"), text, frames + caps, top_k=8, clip_min=0.24, caption_min=0.82)
    assert sum(h.get("kind") == "video_frame" for h in out) == 1     # 1 video = 1 vị trí
    assert [h["media_id"] for h in out if h.get("kind") == "media_caption"] == [51]


def test_gate_caption_kw_needs_coupling():
    """Ngoài phạm vi "cho xem ảnh tháp Eiffel" (media_v1 x002): 8 caption_kw ngẫu nhiên từng qua gate vì caption_kw
    không có ngưỡng. Giờ caption_kw chỉ là bằng chứng khi ảnh thuộc bài chữ đứng đầu."""
    text = [{"document_id": 10, "url": "https://x/a"}]
    kw = [{"modality": "caption_kw", "raw_score": None, "media_key": f"k{i}", "kind": "media_caption",
           "media_kind": "image", "document_id": 50 + i} for i in range(8)]
    kw.append({"modality": "caption_kw", "raw_score": None, "media_key": "mine", "kind": "media_caption",
               "media_kind": "image", "document_id": 10})
    out = R.gate(R.Route("required"), text, kw, 8, clip_min=0.25, caption_min=0.5)
    assert [h["media_key"] for h in out] == ["mine"]


def test_gate_doc_candidates_first_with_parent_url():
    """A: ảnh của bài chữ hạng 1 (modality doc) đứng trước ảnh CLIP cao điểm hơn của bài khác; ảnh dùng chung nhiều bài
    lấy url của bài chữ đã kéo nó lên làm bài cha."""
    text = [{"document_id": 10, "url": "https://x/a"}, {"document_id": 11, "url": "https://x/b"}]
    c = [_img(1, 99, 0.40), _img(2, [33, 11], 0.27, modality="doc"), _img(3, [10], 0.30, modality="doc"),
         _img(4, [10], 0.20, modality="doc")]
    out = R.gate(R.Route("required"), text, c, 8, clip_min=0.25, caption_min=0.5)
    assert [h["media_id"] for h in out] == [3, 2, 1]          # bài 10, rồi bài 11, rồi ảnh không gắn; 0,20 < ngưỡng
    assert out[0]["page_url"] == "https://x/a" and out[1]["page_url"] == "https://x/b" and "page_url" not in out[2]


def test_gate_media_cap_by_intent():
    text = [{"document_id": 10, "url": "https://x/a"}]
    c = [_img(i, 10, 0.30 + i / 100, modality="doc") for i in range(5)] + [_img(9, 99, 0.5)]
    kw = dict(top_k=8, clip_min=0.25, caption_min=0.5, optional_max=3)
    assert R.gate(R.Route("none"), text, c, **kw) == []                          # none_max mặc định 0
    two = R.gate(R.Route("none"), text, c, none_max=2, **kw)
    assert [h["media_id"] for h in two] == [4, 3]                                # chỉ ảnh gắn, cosine cao nhất trước
    assert len(R.gate(R.Route("optional"), text, c, **kw)) == 3 and 9 not in [
        h["media_id"] for h in R.gate(R.Route("optional"), text, c, **kw)]
    assert len(R.gate(R.Route("required"), text, c, **kw)) == 6
    assert R.media_cap(R.Route("required"), 8, 3, 2) == 8 and R.media_cap(R.Route("none"), 8, 3) == 0


def test_gate_video_request_prefers_video():
    """D: câu đòi video (v002 "xem video giới thiệu CTĐT CNTT" từng ra 8/8 ảnh): video đứng trước mọi ảnh, ảnh không
    gắn bài chữ bị loại."""
    route = R.classify_rules("xem video giới thiệu chương trình đào tạo Công nghệ thông tin")
    assert route.media == "required" and route.wants_video
    assert not R.classify_rules("Cho xem ảnh lễ khai giảng").wants_video
    text = [{"document_id": 10, "url": "https://x/a"}]
    c = [_img(1, 10, 0.40, modality="doc"), _img(2, 99, 0.45),
         {"modality": "caption", "raw_score": 0.6, "kind": "media_caption", "media_kind": "video", "video_key": "yt1",
          "url": "https://youtube.com/embed/yt1", "document_id": 77},
         {"modality": "clip", "raw_score": 0.30, "kind": "video_frame", "video_key": "yt1", "document_id": [77]}]
    out = R.gate(route, text, c, 8, clip_min=0.25, caption_min=0.5)
    assert [R.is_video(h) for h in out] == [True, False]           # 1 video (1 vị trí) rồi ảnh gắn; ảnh 99 bị loại
    assert out[1]["media_id"] == 1
    # bài chữ đứng đầu có video → video đó là câu trả lời, video không gắn bị bỏ
    own = {"modality": "doc", "raw_score": 0.26, "kind": "video_frame", "video_key": "yt2", "document_id": [10]}
    out = R.gate(route, text, c + [own], 8, clip_min=0.25, caption_min=0.5)
    assert [h.get("video_key") or h.get("media_id") for h in out] == ["yt2", 1]


def test_supported_media_when_evidence_is_weak():
    """C: sufficient False → chỉ media thuộc nguồn chữ đứng đầu (đúng loại được hỏi) + ảnh giống ảnh người dùng gửi."""
    from types import SimpleNamespace as S
    text = [S(url="https://x/a", parent_url=None), S(url="https://x/f.pdf", parent_url="https://x/b"),
            S(url="https://x/c", parent_url=None)]
    media = [S(kind="image", parent_url="https://x/a", via="doc"), S(kind="image", parent_url="https://x/z", via="clip"),
             S(kind="video", parent_url="https://x/b", via="caption"), S(kind="image", parent_url="https://x/q", via="i2i"),
             S(kind="image", parent_url="https://x/c", via="doc")]
    keep = R.supported_media(media, text, 2)
    assert [m.parent_url for m in keep] == ["https://x/a", "https://x/b", "https://x/q"]
    assert [m.kind for m in R.supported_media(media, text, 2, wants_video=True)] == ["video", "image"]  # video + i2i


def test_retrieve_none_route_skips_media_leg(monkeypatch=None):
    from sict_crawler.repository import cache, vector_store
    from sict_crawler.service.embedding import embedder
    from sict_crawler.service.retrieval import query_rewriter, retriever
    saved = (query_rewriter.rewrite, query_rewriter.hybrid_search_text, query_rewriter.rerank_text,
             embedder.embed_query, embedder.embed_text_clip, vector_store.search_images,
             cache.get, cache.put, vector_store.keyword_search, vector_store.has_sparse)
    calls = {"clip": 0}
    try:
        query_rewriter.rewrite = lambda q: {"rewritten": q, "terms": ["hoc", "phi"], "route_llm": None}
        hits = [{"url": "https://x/vn/a", "title": "A", "content": "học phí", "score": 0.9,
                 "document_id": 1},
                {"url": "img", "kind": "media_caption", "media_kind": "image", "media_id": 7,
                 "caption": "ảnh", "score": 0.95, "document_id": 1}]
        query_rewriter.hybrid_search_text = lambda *a, **k: [dict(h) for h in hits]
        query_rewriter.rerank_text = lambda h, q, top_n=5, **k: [
            dict(x, raw_score=x["score"]) for x in h]
        embedder.embed_query = lambda q: [0.0]
        embedder.embed_text_clip = lambda t: calls.__setitem__("clip", calls["clip"] + 1) or [[0.0]]
        vector_store.search_images = lambda *a, **k: []
        vector_store.keyword_search = lambda *a, **k: []   # không chạm Qdrant thật
        vector_store.has_sparse = lambda *a, **k: True
        cache.get = lambda k: None
        cache.put = lambda *a, **k: None
        t, m, route = retriever.retrieve("Học phí ngành CNTT bao nhiêu?", 5)
        assert route.media == "none" and m == [] and calls["clip"] == 0
        assert all(s.kind == "text" for s in t)  # caption ảnh không lọt vào nguồn chữ
        t, m, route = retriever.retrieve("Cho xem ảnh học phí", 5)
        assert route.media == "required" and calls["clip"] == 1
        assert [s.media_id for s in m] == [7]   # caption cosine 0.95 ≥ ngưỡng, cùng bài đứng đầu
    finally:
        (query_rewriter.rewrite, query_rewriter.hybrid_search_text, query_rewriter.rerank_text,
         embedder.embed_query, embedder.embed_text_clip, vector_store.search_images,
         cache.get, cache.put, vector_store.keyword_search, vector_store.has_sparse) = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK — router")


def test_router_accented_anh_is_image_intent():
    assert R.classify_rules("Ảnh PGS.TS Nguyễn Thị Kim Sơn").media == "required"
    assert R.classify_rules("Ảnh hưởng của AI tới việc làm?").media == "none"
    assert R.classify_rules("anh Nguyễn Việt Hoàng là ai").media == "none"
