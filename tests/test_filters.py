"""Metadata filtering: suy ưu tiên từ câu hỏi, lọc cứng từ API, boost, qdrant filter.

Run: python -m tests.test_filters  |  pytest tests/test_filters.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.service.retrieval import filters as F

TODAY = date(2026, 10, 12)


def test_infer_sections_years_recency():
    f = F.infer("Thông báo tuyển sinh năm 2025 mới nhất?", TODAY)
    assert "thong-bao" in f.prefer_sections and "tuyen-sinh-dai-hoc" in f.prefer_sections
    assert f.prefer_years == [2025] and f.recency
    assert F.infer("quy che dao tao tin chi", TODAY).prefer_sections == ["quy-che"]
    assert F.infer("Kế hoạch năm học 2025-2026", TODAY).prefer_years == [2025, 2026]
    assert F.infer("Sự kiện năm nay có gì?", TODAY).prefer_years == [2026]
    assert F.infer("Trường SICT ở đâu?", TODAY).prefer_years == []
    assert not F.infer("Trường SICT ở đâu?", TODAY).has_hard()  # suy luận không bao giờ lọc cứng
    # "tiếng Anh" không được kéo về thư viện ảnh
    assert "thu-vien-anh" not in F.infer("Khoa học máy tính dạy bằng tiếng Anh?", TODAY).prefer_sections
    assert "an-toan-thong-tin" in F.infer("Ngành an toàn thông tin học gì?", TODAY).prefer_sections


def test_from_request_validates():
    f = F.from_request({"sections": ["thong-bao", "DROP TABLE", 5], "doc_types": ["attachment", "x"],
                        "year_from": 2024, "year_to": "2026", "evil": 1})
    assert f.sections == ["thong-bao"] and f.doc_types == ["attachment"]
    assert f.year_from == 2024 and f.year_to is None
    assert F.from_request(None).has_hard() is False


def test_matches_hard_filter():
    f = F.QueryFilters(sections=["thong-bao"], year_from=2025)
    assert F.matches({"section": "thong-bao", "year": 2026}, f)
    assert not F.matches({"section": "tin-tuc", "year": 2026}, f)
    assert not F.matches({"section": "thong-bao", "year": 2024}, f)
    assert not F.matches({"section": "thong-bao"}, f)  # thiếu năm mà đòi lọc năm → loại


def test_boost_soft_and_neutral_when_missing():
    f = F.infer("thông báo mới nhất", TODAY)
    new = {"section": "thong-bao", "published_at": "2026-10-01T08:00:00+07:00", "year": 2026}
    old = {"section": "thong-bao", "published_at": "2022-01-01T08:00:00+07:00", "year": 2022}
    legacy = {"url": "x"}  # index cũ chưa có metadata
    assert F.boost(new, f, TODAY) > F.boost(old, f, TODAY) > 1.0
    assert F.boost(legacy, f, TODAY) == 1.0
    hits = [dict(old, score=1.0), dict(new, score=0.95)]
    assert F.apply_boost(hits, f, today=TODAY)[0]["year"] == 2026


def test_cache_key_differs_by_filters():
    assert F.QueryFilters().cache_key() == ""
    assert F.QueryFilters(sections=["thong-bao"]).cache_key() != F.QueryFilters(sections=["tin-tuc"]).cache_key()


def test_to_qdrant():
    assert F.to_qdrant(F.QueryFilters(prefer_sections=["x"])) is None  # chỉ mềm → không filter
    flt = F.to_qdrant(F.QueryFilters(sections=["thong-bao"], doc_types=["attachment"], year_from=2025))
    keys = [c.key for c in flt.must]
    assert keys == ["section", "doc_type", "year"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK — filters")
