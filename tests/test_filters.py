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


def test_infer_latest_only_when_question_is_section_plus_recency():
    for q in ["Thông báo mới nhất của trường là gì?", "thong bao moi nhat", "Lịch thi mới nhất",
              "Tin tức gần đây nhất của SICT"]:
        assert F.infer(q, TODAY).latest, q
    # còn nội dung riêng (học bổng, năm, học phí) → chỉ ưu tiên mềm như cũ, tìm theo nghĩa
    for q in ["Thông báo mới nhất về học bổng", "Thông báo mới nhất năm 2026", "Học phí mới nhất bao nhiêu?",
              "Thông báo tuyển sinh", "Quy chế đào tạo hiện hành"]:
        assert not F.infer(q, TODAY).latest, q
    assert F.merge(F.from_request(None), F.infer("thông báo mới nhất", TODAY)).latest


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


def test_infer_scope_section_when_question_names_published_works():
    """Câu nêu đúng loại công bố ("bài báo khoa học", "báo cáo khoa học", "công trình đã công bố") → mục
    danh-muc-cong-trinh-da-cong-bo là phạm vi tìm thêm; đề tài NCKH sinh viên thì không (golden_v2 g053/g054)."""
    for q in ["Năm 2021 giảng viên của trường công bố những bài báo khoa học nào?",
              "Báo cáo khoa học năm 2021 có những báo cáo nào?", "cac cong trinh da cong bo nam 2020"]:
        f = F.infer(q, TODAY)
        assert f.scope_sections == ["danh-muc-cong-trinh-da-cong-bo"], q
        assert "sinh-vien-nckh" not in f.prefer_sections, q       # không cộng điểm cho đề tài sinh viên
    for q in ["Tổng hợp đề tài sinh viên nghiên cứu khoa học năm 2021", "Ngành khoa học máy tính học gì?",
              "báo cáo thực tập tốt nghiệp nộp khi nào"]:
        assert F.infer(q, TODAY).scope_sections == [], q
    assert "scope_sections" in F.infer("bài báo khoa học 2021", TODAY).cache_key()


def test_year_boost_uses_table_label_year_of_the_chunk():
    """Một trang gom bảng nhiều năm (Bài báo khoa học: NĂM 2021 … NĂM 2013) có year = năm đăng trang. Mảnh bảng
    mang nhãn năm thì so năm theo nhãn, không theo năm đăng."""
    f = F.QueryFilters(prefer_years=[2021])
    head = "[Bài báo khoa học | Công trình đã công bố | 10/12/2021] "
    assert F.boost({"year": 2021, "content": head + "[Bảng: NĂM 2021] | STT | Tên bài báo |"}, f) == F.YEAR_BOOST
    assert F.boost({"year": 2021, "content": head + "[Bảng: NĂM 2014] | STT | Tên bài báo |"}, f) == 1.0
    assert F.boost({"year": 2019, "content": head + "[Bảng: Danh sách năm học 2020-2021] | STT |"}, f) == F.YEAR_BOOST
    assert F.boost({"year": 2021, "content": head + "[Bảng] | STT |"}, f) == F.YEAR_BOOST    # không nhãn năm
    assert F.boost({"year": 2021, "content": head + "Bài báo khoa học"}, f) == F.YEAR_BOOST


def test_title_boost_when_question_names_the_page():
    """Tiêu đề trang (≥ 3 từ) nằm nguyên văn trong câu hỏi → nhân điểm: "bài báo khoa học" chọn trang "Bài báo khoa
    học", không phải "Báo cáo khoa học" (cùng mục, cùng năm, bảng giống nhau)."""
    hits = [{"title": "Báo cáo khoa học", "score": 1.0}, {"title": "Bài báo khoa học", "score": 0.9},
            {"title": "Tin tức", "score": 0.95}]
    out = F.apply_title_boost([dict(h) for h in hits], "Năm 2021 giảng viên công bố những bài báo khoa học nào?")
    assert [h["title"] for h in out] == ["Bài báo khoa học", "Báo cáo khoa học", "Tin tức"]
    out = F.apply_title_boost([dict(h) for h in hits], "tin tức mới")        # tiêu đề 2 từ: không tính
    assert [h["title"] for h in out] == ["Báo cáo khoa học", "Bài báo khoa học", "Tin tức"]   # không khớp: giữ thứ tự
    out = F.apply_title_boost([dict(h) for h in hits], "bao cao khoa hoc nam 2021")   # không dấu vẫn khớp
    assert out[0]["title"] == "Báo cáo khoa học" and out[0]["score"] == F.TITLE_BOOST
    assert F.apply_title_boost([{"title": "Bài báo khoa học", "score": 1.0}], "bài báo khoa họcxyz")[0]["score"] == 1.0
