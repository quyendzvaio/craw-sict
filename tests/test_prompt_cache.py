"""Tests prompt caching hai tầng (T1 route, T2 answer) — spec docs/prompt_caching_multi_tier.md.

Không cần Redis thật: mock repository.cache.get/put. Trọng tâm:
  - key uniqueness (đổi câu hỏi / ảnh / model / template / index / ngữ cảnh → key khác);
  - tắt (enabled=False) → mọi get trả None, put không chạm Redis;
  - miss → hit qua mock;
  - stream_answer_cached: hit stream lại nguyên câu trả lời, miss gọi LLM rồi lưu.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sict_crawler.service.retrieval import prompt_cache as pc


# ── key uniqueness ─────────────────────────────────────────────────────────────

def test_route_key_differs_by_question():
    assert pc.route_cache_key("câu A", "img", "q1") != pc.route_cache_key("câu B", "img", "q1")


def test_route_key_differs_by_image():
    assert pc.route_cache_key("q", "img1", "q1") != pc.route_cache_key("q", "img2", "q1")


def test_route_key_differs_by_query_key():
    assert pc.route_cache_key("q", "img", "q1") != pc.route_cache_key("q", "img", "q2")


def test_route_key_differs_by_router_model(monkeypatch):
    from sict_crawler.core.config import get_settings
    llm = get_settings().llm
    k1 = pc.route_cache_key("q", "img", "q1")
    monkeypatch.setattr(llm, "router_model", "other-router")
    k2 = pc.route_cache_key("q", "img", "q1")
    assert k1 != k2
    # router_model rỗng → fallback sang cfg.model (vẫn tạo key hợp lệ, đúng prefix)
    monkeypatch.setattr(llm, "router_model", "")
    assert pc.route_cache_key("q", "img", "q1").startswith("pc:v1:route:")


def test_route_key_keeps_diacritics():
    """"tiếng Anh" và "ảnh" cùng fold thành "anh" — key phải phân biệt (không fold, xem hybrid_router rr:v3)."""
    assert pc.route_cache_key("tiếng Anh", "", "q") != pc.route_cache_key("ảnh", "", "q")


def test_prompt_key_differs_by_model(monkeypatch):
    from sict_crawler.core.config import get_settings
    llm = get_settings().llm
    k1 = pc.prompt_cache_key("q", "img", "ctx", "idx")
    monkeypatch.setattr(llm, "model", "other-model")
    assert pc.prompt_cache_key("q", "img", "ctx", "idx") != k1


def test_prompt_key_differs_by_max_tokens(monkeypatch):
    from sict_crawler.core.config import get_settings
    llm = get_settings().llm
    k1 = pc.prompt_cache_key("q", "img", "ctx", "idx")
    monkeypatch.setattr(llm, "max_new_tokens", 512)
    assert pc.prompt_cache_key("q", "img", "ctx", "idx") != k1


def test_prompt_key_differs_by_template_version(monkeypatch):
    from sict_crawler.core.config import get_settings
    llm = get_settings().llm
    k1 = pc.prompt_cache_key("q", "img", "ctx", "idx")
    monkeypatch.setattr(llm, "prompt_cache_template_version", "v2")
    assert pc.prompt_cache_key("q", "img", "ctx", "idx") != k1


def test_prompt_key_differs_by_index():
    assert pc.prompt_cache_key("q", "img", "ctx", "sict_text_v2") != \
        pc.prompt_cache_key("q", "img", "ctx", "sict_text_v3")


def test_prompt_key_differs_by_context():
    """Cùng câu hỏi, ngữ cảnh khác (filters khác) → key khác — không phục vụ answer cũ."""
    assert pc.prompt_cache_key("q", "img", "ngữ cảnh A", "idx") != \
        pc.prompt_cache_key("q", "img", "ngữ cảnh B", "idx")


def test_key_format_prefixes():
    assert pc.route_cache_key("q", "", "k").startswith("pc:v1:route:")
    assert pc.prompt_cache_key("q", "", "ctx", "idx").startswith("pc:v1:prompt:")


# ── tắt mặc định ───────────────────────────────────────────────────────────────

def test_disabled_route_get_returns_none_and_skips_redis(monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_enabled", False)
    with patch("sict_crawler.repository.cache.get") as mock_get:
        assert pc.get_route("q", "img", "k") is None
    mock_get.assert_not_called()


def test_disabled_answer_get_returns_none_and_skips_redis(monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_enabled", False)
    with patch("sict_crawler.repository.cache.get") as mock_get:
        assert pc.get_answer("q", "img", "ctx", "idx") is None
    mock_get.assert_not_called()


def test_disabled_route_put_skips_redis(monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_enabled", False)
    with patch("sict_crawler.repository.cache.put") as mock_put:
        pc.put_route("q", "img", "k", {"media": "none", "wants_file": False, "oos": False})
    mock_put.assert_not_called()


def test_disabled_answer_put_skips_redis(monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_enabled", False)
    with patch("sict_crawler.repository.cache.put") as mock_put:
        pc.put_answer("q", "img", "ctx", "idx", "trả lời")
    mock_put.assert_not_called()


# ── hit / miss (mock Redis) ────────────────────────────────────────────────────

@pytest.fixture()
def cache_on(monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_enabled", True)


def test_route_hit_roundtrip(cache_on):
    payload = {"media": "optional", "wants_file": True, "oos": False, "confidence": 0.9}
    with patch("sict_crawler.repository.cache.get",
               return_value=json.dumps(payload, ensure_ascii=False).encode()):
        assert pc.get_route("q", "img", "k") == payload


def test_route_miss_returns_none(cache_on):
    with patch("sict_crawler.repository.cache.get", return_value=None):
        assert pc.get_route("q", "img", "k") is None


def test_route_corrupt_payload_returns_none(cache_on):
    with patch("sict_crawler.repository.cache.get", return_value=b"{khong phai json"):
        assert pc.get_route("q", "img", "k") is None


def test_route_put_uses_configured_ttl(cache_on, monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_route_ttl", 7200)
    calls = []
    with patch("sict_crawler.repository.cache.put", side_effect=lambda k, v, t: calls.append(t)):
        pc.put_route("q", "img", "k", {"media": "none", "wants_file": False, "oos": False})
    assert calls == [7200]


def test_answer_hit_roundtrip(cache_on):
    with patch("sict_crawler.repository.cache.get", return_value="Trả lời từ cache".encode()):
        assert pc.get_answer("q", "img", "ctx", "idx") == "Trả lời từ cache"


def test_answer_miss_returns_none(cache_on):
    with patch("sict_crawler.repository.cache.get", return_value=None):
        assert pc.get_answer("q", "img", "ctx", "idx") is None


def test_answer_put_refuses_empty(cache_on):
    with patch("sict_crawler.repository.cache.put") as mock_put:
        pc.put_answer("q", "img", "ctx", "idx", "   ")
        pc.put_answer("q", "img", "ctx", "idx", "")
    mock_put.assert_not_called()


def test_answer_put_uses_configured_ttl(cache_on, monkeypatch):
    from sict_crawler.core.config import get_settings
    monkeypatch.setattr(get_settings().llm, "prompt_cache_prompt_ttl", 43200)
    calls = []
    with patch("sict_crawler.repository.cache.put", side_effect=lambda k, v, t: calls.append(t)):
        pc.put_answer("q", "img", "ctx", "idx", "Trả lời")
    assert calls == [43200]


# ── stream_answer_cached ───────────────────────────────────────────────────────

def test_stream_hit_replays_cache_without_llm(cache_on):
    cached = "Trả lời đầy đủ từ cache, dài hơn 80 ký tự để test chia khúc stream mượt cho UI."
    with patch.object(pc, "get_answer", return_value=cached), \
         patch("sict_crawler.service.retrieval.vlm.stream_answer") as mock_llm:
        got = list(pc.stream_answer_cached("q", "ctx"))
    assert "".join(got) == cached
    mock_llm.assert_not_called()


def test_stream_miss_calls_llm_and_stores(cache_on):
    with patch.object(pc, "get_answer", return_value=None), \
         patch("sict_crawler.service.retrieval.vlm.stream_answer",
               return_value=iter(["Đáp án ", "từ LLM"])) as mock_llm, \
         patch.object(pc, "put_answer") as mock_put:
        got = list(pc.stream_answer_cached("q", "ctx"))
    assert "".join(got) == "Đáp án từ LLM"
    mock_llm.assert_called_once()
    mock_put.assert_called_once()
    # tham số lưu phải là tổng câu trả lời
    args = mock_put.call_args[0]
    assert args[-1] == "Đáp án từ LLM"


def test_stream_llm_error_does_not_store(cache_on):
    def boom(*a, **k):
        raise RuntimeError("LLM hỏng")
        yield  # pragma: no cover — generator có raise ở body
    with patch.object(pc, "get_answer", return_value=None), \
         patch("sict_crawler.service.retrieval.vlm.stream_answer", boom), \
         patch.object(pc, "put_answer") as mock_put:
        with pytest.raises(RuntimeError):
            list(pc.stream_answer_cached("q", "ctx"))
    mock_put.assert_not_called()
