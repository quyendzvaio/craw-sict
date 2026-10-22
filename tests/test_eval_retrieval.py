"""Hàm thuần của scripts/eval_retrieval.py: BM25 không dấu, RRF, gộp mức tài liệu, expect_any.

Run: python -m tests.test_eval_retrieval  |  pytest tests/test_eval_retrieval.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("eval_retrieval", ROOT / "scripts" / "eval_retrieval.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def test_bm25_accent_insensitive():
    bm = ev.BM25(["Thông báo tuyển sinh đại học năm 2026", "Lịch thực hành khoa CNTT", "Học bổng"])
    s = bm.scores("tuyen sinh 2026")
    assert int(np.argmax(s)) == 0 and s[1] == 0


def test_rrf_and_doc_rank_attachment_counts_as_parent():
    chunks = [{"url": "https://x/vn/a"},
              {"url": "https://x/media/1.pdf", "parent_url": "http://x/vn/html/quy-che/"},
              {"url": "https://x/vn/a"}]
    fused = ev.rrf([1, 0, 2], [1])
    order = sorted(fused, key=lambda i: -fused[i])
    docs = ev.rank_docs(order, chunks, np.ones(3, dtype=bool))
    assert docs[0] == "x/vn/html/quy-che"          # file đính kèm → trang cha, chuẩn hoá scheme + "/"
    assert docs == ["x/vn/html/quy-che", "x/vn/a"]  # trùng tài liệu chỉ tính 1 lần


def test_expect_any():
    meta = {"x/vn/thong-bao/1": {"section": "thong-bao", "published_at": "2026-09-01T08:00:00+07:00"},
            "x/vn/tin-tuc/2": {"section": "tin-tuc", "published_at": "2024-01-01T08:00:00+07:00"}}
    assert ev.expect_any_ok(["x/vn/tin-tuc/2", "x/vn/thong-bao/1"], meta,
                            {"section": "thong-bao", "published_after": "2026-08-01"})
    assert not ev.expect_any_ok(["x/vn/tin-tuc/2"], meta,
                                {"section": "tin-tuc", "published_after": "2025-01-01"})


if __name__ == "__main__":
    test_bm25_accent_insensitive()
    test_rrf_and_doc_rank_attachment_counts_as_parent()
    test_expect_any()
    print("OK — eval_retrieval helpers")
