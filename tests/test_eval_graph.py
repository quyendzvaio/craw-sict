"""Cổng hồi quy trích xuất graph (D20, giai đoạn B): luật hiện tại trên bộ nhỏ gán tay
(tests/fixtures/graph_gold_mini.jsonl, 6 bài). Ngưỡng = số đo lúc tạo bộ (2026-10-14) trừ biên nhỏ:
đổi luật / prompt / model làm tụt precision người hoặc precision chung → CI đỏ. Recall sự kiện / ảnh
thấp là đã biết (giai đoạn C nhắm vào đó) nên chỉ chặn không cho tụt thêm.

Bộ đầy đủ: eval/graph_gold_v1.jsonl (150 bài, cần người duyệt) — scripts/eval_graph.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import eval_graph as EG  # noqa: E402

GOLD = Path(__file__).parent / "fixtures" / "graph_gold_mini.jsonl"


def _items():
    return [json.loads(x) for x in GOLD.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_matching_rules():
    assert EG.same("person", EG.norm("person", "Lê Thị Thủy"), EG.norm("person", "Lê Thị Thuỷ"))
    assert not EG.same("person", EG.norm("person", "Nguyễn Văn Hùng"), EG.norm("person", "Nguyễn Văn Hưng"))
    assert EG.same("event", EG.norm("event", "IT Festival 2023"), EG.norm("event", "IT Festival"))
    assert EG.same("org", EG.norm("org", "IT Supporter"), EG.norm("org", "CLB IT Supporter"))
    gold = [{"type": "person", "name": "A Bc"}]
    assert EG.evaluate([{"url": "u", "status": "reviewed", "entities": gold, "media": []}],
                       {"u": {"entities": [], "media": []}})["person"]["recall"] == 0.0


def test_rules_regression_on_hand_labeled_mini_set():
    items = _items()
    assert all(it["status"] == "reviewed" for it in items) and len(items) == 6
    res = EG.evaluate(items, EG.predictions(items, "rules", gaz_source="items"))
    assert res["person"]["precision"] >= 0.9 and res["person"]["recall"] >= 0.85, res["person"]
    assert res["all_entities"]["precision"] >= 0.8, res["all_entities"]
    assert res["event"]["recall"] >= 0.25, res["event"]
    # 2026-10-18 (danh mục chuẩn CLB/sự kiện): 0,82 → 0,64. Các cặp "thừa" mới đã soát tay là ĐÚNG nhưng bộ
    # nhỏ không gán (logo "Đội hỗ trợ kỹ thuật" trong OCR → IT Supporter; alt "học bổng tài năng STP" → Samsung
    # STP) hoặc khác độ mịn (chuỗi "ICPC" vs nhãn "ICPC Asia Hochiminh City 2025"); recall 0,18 → 0,64.
    assert res["media"]["precision"] >= 0.6 and res["media"]["recall"] >= 0.5, res["media"]
