"""Graph khái niệm (graph.mode = concepts): khoá khái niệm, chọn theo số bài, khớp danh bạ trên đoạn,
cấu hình, khớp câu hỏi.

Run: pytest tests/test_graph_concepts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.core.config import GraphConfig, _build
from sict_crawler.service.graph import concepts as C
from sict_crawler.service.graph import graph as G


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


def test_graph_config_from_yaml():
    import yaml
    data = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    assert _build(data).graph.concept_df_max_ratio == GraphConfig().concept_df_max_ratio == 0.0557
    data["graph"] = {"concept_df_max_ratio": 0.1}
    assert _build(data).graph.concept_df_max_ratio == 0.1


def test_gazetteer_query_match_folds_accents():
    g = G.Gazetteer([G.Entity(kind="concept", key="hoc phi", name="học phí", doc_count=5)])
    assert [e.key for e in g.match("Học phí năm 2025 bao nhiêu?")] == ["hoc phi"]
    assert [e.key for e in g.match("hoc phi nam 2025")] == ["hoc phi"]      # câu không dấu
    G.set_current(g)
    try:
        assert G.ekeys(G.match_question("học phí", "Học phí ngành CNTT")) == ["concept:hoc phi"]
    finally:
        G.set_current(None)


def test_incremental_graph_stage_rebuilds_only_when_corpus_changed(monkeypatch):
    """Dựng lại sau lượt ingest khi chữ ký corpus đổi; không đổi → bỏ qua; lượt dựng lỗi không ghi chữ ký."""
    import asyncio
    import contextlib

    from sict_crawler.pipeline import incremental as inc
    from sict_crawler.service.graph import graphsync

    store, sig, calls = {}, ["s1"], []

    class FakeRedis:
        def get(self, k):
            return store.get(k)

        def set(self, k, v):
            store[k] = v

    async def fake_sig():
        return sig[0]

    async def fake_rebuild(session, sync=True):
        calls.append(sig[0])
        if sig[0] == "boom":
            raise RuntimeError("hỏng")
        return {"entities": 1}

    @contextlib.asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(inc, "_redis", lambda: FakeRedis())
    monkeypatch.setattr(inc, "corpus_signature", fake_sig)
    monkeypatch.setattr(inc.vector_store, "has_sparse", lambda: True)
    monkeypatch.setattr(inc.db, "get_session", fake_session)
    monkeypatch.setattr(graphsync, "rebuild", fake_rebuild)
    assert "rebuild" in asyncio.run(inc._graph_stage())          # chưa dựng lần nào
    assert "skipped" in asyncio.run(inc._graph_stage())          # corpus không đổi
    sig[0] = "boom"
    with pytest.raises(RuntimeError):
        asyncio.run(inc._graph_stage())
    assert store[inc.GRAPH_SIG_KEY] == "s1"                      # lỗi → giữ chữ ký cũ, lượt sau dựng lại
    sig[0] = "s2"
    assert "rebuild" in asyncio.run(inc._graph_stage())
    assert calls == ["s1", "boom", "s2"]
