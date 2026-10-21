"""Router D17: ý định (luật/LLM) + chặn theo bằng chứng (gate) + nối vào api.retrieve.

Run: python -m sict_crawler.tests.test_router  |  pytest sict_crawler/tests/test_router.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler import router as R
from sict_crawler.vlm import parse_route_json

GOLDEN = Path(__file__).resolve().parent.parent.parent / "eval" / "golden_v1.jsonl"


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


def _img(mid, doc, score, modality="clip", kind="image", url=None):
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
    assert 6 not in ids and 5 in ids and ids[:3] == [1, 2, 4]  # bài đứng đầu trước, ảnh lạc đề bị loại
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


def test_retrieve_none_route_skips_media_leg(monkeypatch=None):
    from sict_crawler import api, embedder, query_rewriter, vector_store
    saved = (query_rewriter.rewrite, query_rewriter.hybrid_search_text, query_rewriter.rerank_text,
             embedder.embed_query, embedder.embed_text_clip, vector_store.search_images,
             api._cache_get, api._cache_set, vector_store.keyword_search, vector_store.has_sparse)
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
        api._cache_get = lambda k: None
        api._cache_set = lambda *a, **k: None
        t, m, route = api.retrieve("Học phí ngành CNTT bao nhiêu?", 5)
        assert route.media == "none" and m == [] and calls["clip"] == 0
        assert all(s.kind == "text" for s in t)  # caption ảnh không lọt vào nguồn chữ
        t, m, route = api.retrieve("Cho xem ảnh học phí", 5)
        assert route.media == "required" and calls["clip"] == 1
        assert [s.media_id for s in m] == [7]   # caption cosine 0.95 ≥ ngưỡng, cùng bài đứng đầu
    finally:
        (query_rewriter.rewrite, query_rewriter.hybrid_search_text, query_rewriter.rerank_text,
         embedder.embed_query, embedder.embed_text_clip, vector_store.search_images,
         api._cache_get, api._cache_set, vector_store.keyword_search, vector_store.has_sparse) = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK — router")
