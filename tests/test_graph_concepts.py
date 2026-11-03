"""Graph khái niệm (graph.mode = concepts): khoá khái niệm, chọn theo số bài, khớp danh bạ trên đoạn,
cấu hình và đường dựng trong graphsync.

Run: pytest tests/test_graph_concepts.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.core.config import GraphConfig, _build, get_settings
from sict_crawler.service.graph import concepts as C
from sict_crawler.service.graph import graph as G
from sict_crawler.service.graph import graphsync


def test_concept_key_normalises_and_rejects():
    assert C.concept_key("Hội thảo Khoa học 2024") == "hoi thao khoa hoc"
    assert C.concept_key("Thị giác máy tính") == "thi giac may tinh"
    assert C.concept_key("sinh viên") == "sinh vien"
    assert C.concept_key("Trường") is None          # 1 từ
    assert C.concept_key("2024 2025") is None       # toàn số / năm
    assert C.concept_key("x " * 200) is None        # quá cột entities.key


def test_select_concepts_by_document_frequency():
    pairs = [
        (1, ["Thị giác máy tính", "sinh viên"]),
        (2, ["thị giác máy tính", "sinh viên"]),
        (3, ["sinh viên", "Hội thảo riêng"]),       # cụm chỉ 1 bài → bỏ
        (4, ["sinh viên"]),
        (5, ["sinh viên"]),
    ]
    # 5 bài, tỷ lệ 0,6 → tối đa 3 bài: "sinh viên" (5 bài) quá phổ biến
    ents = {e.key: e for e in C.select_concepts(pairs, n_docs=5, df_max_ratio=0.6)}
    assert set(ents) == {"thi giac may tinh"}
    e = ents["thi giac may tinh"]
    assert (e.kind, e.name, e.doc_count) == ("concept", "Thị giác máy tính", 2)   # tên = cách viết gặp đầu


def test_select_concepts_floor_two_docs():
    # corpus nhỏ: trần không dưới 2 bài
    ents = C.select_concepts([(1, ["máy học"]), (2, ["máy học"])], n_docs=3, df_max_ratio=0.01)
    assert [e.key for e in ents] == ["may hoc"]


def test_link_rows_longest_match_and_captions():
    ents = [G.Entity(kind="concept", key="thi giac may tinh", name="Thị giác máy tính", doc_count=3, id=7),
            G.Entity(kind="concept", key="may tinh", name="máy tính", doc_count=9, id=8),
            G.Entity(kind="concept", key="le tot nghiep", name="lễ tốt nghiệp", doc_count=4, id=9)]
    eids = {(e.kind, e.key): e.id for e in ents}
    chunks = [C.ChunkText("c1", 11, "text", "Hội thảo Thị giác máy tính và ứng dụng"),
              C.ChunkText("c2", 11, "table", "Phòng máy tính tầng 3"),
              C.ChunkText("p3", 12, "media_caption", "Lễ tốt nghiệp 2025. Ảnh sinh viên")]
    rows = C.link_rows(G.Gazetteer(ents), chunks, eids)
    got = {(r["chunk_id"], r["entity_id"]) for r in rows}
    # c1: cụm dài nhất "thị giác máy tính" tại vị trí đó; "máy tính" vẫn khớp ở vị trí sau (chồng lấn)
    assert ("c1", 7) in got
    assert ("c2", 8) in got and ("p3", 9) in got
    assert all(r["rel"] == "mentions" and r["media_id"] is None for r in rows)
    assert {r["document_id"] for r in rows if r["chunk_id"] == "p3"} == {12}


def test_phrase_source_only_text_chunks():
    assert C.ChunkText("a", 1, "text", "x").phrase_source
    assert not C.ChunkText("b", 1, "table", "x").phrase_source
    assert not C.ChunkText("c", 1, "media_caption", "x").phrase_source


def test_noun_phrases_underthesea():
    pytest.importorskip("underthesea")
    nps = C.noun_phrases("Trường Công nghệ Thông tin tổ chức hội thảo thị giác máy tính.")
    assert nps and all(isinstance(p, str) for p in nps)


def test_graph_config_default_and_env(monkeypatch):
    import yaml
    assert GraphConfig().mode == "concepts"
    data = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    monkeypatch.delenv("GRAPH_MODE", raising=False)
    assert _build(data).graph.mode == "concepts"
    data["graph"] = {"mode": "concepts", "concept_df_max_ratio": 0.1}
    assert _build(data).graph.concept_df_max_ratio == 0.1
    monkeypatch.setenv("GRAPH_MODE", "rules")
    assert _build(data).graph.mode == "rules"


def test_rebuild_dispatches_by_mode(monkeypatch):
    calls = []

    async def fake_concepts(session, sync=True):
        calls.append(("concepts", sync))
        return {"mode": "concepts"}

    async def fake_update(session, doc_ids):
        calls.append(("update", tuple(doc_ids)))
        return {"mode": "concepts"}

    monkeypatch.setattr(graphsync, "rebuild_concepts", fake_concepts)
    monkeypatch.setattr(graphsync, "update_concept_documents", fake_update)
    monkeypatch.setattr(get_settings().graph, "mode", "concepts")
    assert asyncio.run(graphsync.rebuild(None, sync=False)) == {"mode": "concepts"}
    assert asyncio.run(graphsync.update_documents(None, [3, 4])) == {"mode": "concepts"}
    assert calls == [("concepts", False), ("update", (3, 4))]


def test_update_concepts_empty_doc_list_does_nothing(monkeypatch):
    # [] không được hiểu là "mọi bài" (scroll không lọc) → không đọc Qdrant, không ghi PG
    monkeypatch.setattr(graphsync, "_scroll_concept_chunks", lambda *a: pytest.fail("không được scroll"))
    assert asyncio.run(graphsync.update_concept_documents(None, []))["documents"] == 0
