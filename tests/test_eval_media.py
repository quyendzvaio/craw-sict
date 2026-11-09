"""Hàm chấm thuần của scripts/eval_media.py (không mạng): score_row, summarize, failures.

Run: pytest tests/test_eval_media.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("eval_media", ROOT / "scripts" / "eval_media.py")
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)

POST = "https://sict.haui.edu.vn/vn/tin-tuc/le-tot-nghiep/71428"
OTHER = "https://sict.haui.edu.vn/vn/tin-tuc/bai-khac/70001"
FILE = "https://sict.haui.edu.vn/media/29/uffile-upload-no-title29968.xls"
PARENT = "https://sict.haui.edu.vn/vn/ke-hoach/thong-bao-ke-hoach/71241"


def img(parent: str, kind: str = "image") -> dict:
    return {"url": "https://sict.haui.edu.vn/media/81/t1.jpg", "parent_url": parent, "kind": kind, "media_key": "ab"}


def txt(url: str, kind: str = "text", parent: str | None = None) -> dict:
    return {"url": url, "parent_url": parent, "kind": kind}


def test_required_image_on_target_and_precision():
    row = {"id": "m1", "case": "image", "style": "question", "media_mode": "required", "wants_file": False,
           "min_media": 2, "expected_urls": [POST], "expected_media_docs": [POST]}
    res = {"media_mode": "required", "wants_file": False, "sufficient": True,
           "text": [txt(OTHER), txt(POST)], "media": [img(POST), img(POST), img(OTHER)]}
    r = em.score_row(row, res)
    assert r["mode_ok"] and r["url_hit"] and r["text_rank"] == 2
    assert r["required_has_media"] is True and r["media_on_target"] is True
    assert (r["media_good"], r["media_total"]) == (2, 3)
    assert r["none_clean"] is None and r["file_hit"] is None and r["video_hit"] is None


def test_required_without_media_fails_and_trailing_slash_http_are_equal():
    row = {"id": "m2", "case": "image", "style": "phrase", "media_mode": "required", "wants_file": False,
           "min_media": 1, "expected_urls": [POST + "/"], "expected_media_docs": [POST]}
    res = {"media_mode": "none", "wants_file": False, "sufficient": True,
           "text": [txt(POST.replace("https://", "http://"))], "media": []}
    r = em.score_row(row, res)
    assert r["mode_ok"] is False and r["url_hit"] is True
    assert r["required_has_media"] is False and r["media_on_target"] is False


def test_optional_without_media_is_not_scored_but_wrong_media_is():
    row = {"id": "o1", "case": "optional", "style": "question", "media_mode": "optional", "wants_file": False,
           "min_media": 0, "expected_urls": [POST], "expected_media_docs": [POST]}
    r = em.score_row(row, {"media_mode": "optional", "text": [txt(POST)], "media": [], "sufficient": True})
    assert r["media_on_target"] is None and r["required_has_media"] is None
    r = em.score_row(row, {"media_mode": "optional", "text": [txt(POST)], "media": [img(OTHER)], "sufficient": True})
    assert r["media_on_target"] is False and (r["media_good"], r["media_total"]) == (0, 1)


def test_none_clean_and_collision_wants_file():
    row = {"id": "n1", "case": "none", "style": "question", "media_mode": "none", "wants_file": False,
           "min_media": 0, "expected_urls": [POST], "expected_media_docs": []}
    r = em.score_row(row, {"media_mode": "none", "wants_file": True, "text": [txt(POST)], "media": [], "sufficient": True})
    assert r["none_clean"] is True and r["wants_file_ok"] is False and r["media_on_target"] is None
    r = em.score_row(row, {"media_mode": "none", "wants_file": False, "text": [txt(POST)], "media": [img(POST)],
                           "sufficient": True})
    assert r["none_clean"] is False


def test_file_hit_uses_kind_file_and_url_hit_accepts_parent():
    row = {"id": "f1", "case": "file", "style": "question", "media_mode": "none", "wants_file": True,
           "min_media": 0, "expected_urls": [FILE, PARENT], "expected_media_docs": []}
    res = {"media_mode": "none", "wants_file": True, "sufficient": True,
           "text": [txt(OTHER), txt(FILE, kind="file", parent=PARENT)], "media": []}
    r = em.score_row(row, res)
    assert r["file_hit"] is True and r["file_rank"] == 2 and r["url_hit"] is True and r["text_rank"] == 2
    res["text"] = [txt(FILE, kind="text", parent=PARENT)]   # đoạn chữ của bài cha, không phải nguồn file
    r = em.score_row(row, res)
    assert r["file_hit"] is False and r["url_hit"] is True


def test_audited_url_labels_separate_file_and_page_and_preserve_old_metric():
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [FILE, PARENT]}
    labels = {"file_urls": [FILE], "page_urls": [PARENT]}
    res = {"media_mode": "none", "wants_file": True,
           "text": [txt(FILE, kind="file", parent=OTHER), txt(OTHER, kind="file", parent=PARENT)]}
    r = em.score_row(row, res, labels)
    assert r['file_hit'] is True
    assert r['attachment_url_hit'] is True
    assert r['attachment_parent_hit'] is True
    assert r['resource_page_hit'] is True


def test_audited_url_labels_mark_unknown_file_as_not_applicable():
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [PARENT]}
    labels = {"file_urls": [], "page_urls": [PARENT]}
    r = em.score_row(row, {"media_mode": "none", "wants_file": True, "text": [txt(PARENT)]}, labels)
    assert r['file_hit'] is False and r['attachment_url_hit'] is None
    assert r['attachment_parent_hit'] is False and r['resource_page_hit'] is True
    s = em.summarize([r])
    assert s['counts']['attachment_url_hit'] == '0/0'
    assert s['counts']['attachment_parent_hit'] == '0/1'


def test_unreviewed_urls_and_irrelevant_files_are_not_counted():
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [PARENT]}
    r = em.score_row(row, {"media_mode": "none", "text": [txt(FILE, 'file', OTHER)]})
    assert r['attachment_url_hit'] is None and r['resource_page_hit'] is None
    r = em.score_row(row, {"media_mode": "none", "text": [txt(FILE, 'file', OTHER)]},
                     {"file_urls": [], "page_urls": [PARENT]})
    assert r['attachment_parent_hit'] is False and r['resource_page_hit'] is False


def test_audit_cannot_change_expected_urls_or_claim_same_url_has_both_types():
    import pytest
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [FILE, PARENT]}
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"file_urls": [OTHER], "page_urls": []})
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"file_urls": [FILE], "page_urls": [FILE]})


def test_cli_reports_reviewed_url_metrics(tmp_path, monkeypatch, capsys):
    import sys
    row = {"id": "f", "question": "Biểu mẫu", "media_mode": "none", "wants_file": True,
           "expected_urls": [PARENT]}
    source = tmp_path / "rows.jsonl"
    source.write_text(json.dumps(row), encoding="utf-8")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"f": {"page_urls": [PARENT]}}), encoding="utf-8")
    output = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["eval_media", "--set", str(source), "--url-labels", str(labels),
                                      "--out", str(output)])
    monkeypatch.setattr(em, "ask", lambda *a: {"media_mode": "none", "wants_file": True, "text": [txt(PARENT)]})
    em.main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["rows"][0]["resource_page_hit"] is True
    assert result["summary"]["counts"]["attachment_url_hit"] == "0/0"
    assert "resource_page=OK" in capsys.readouterr().out


def test_cli_rejects_invalid_sidecar_before_calling_api(tmp_path, monkeypatch):
    import pytest
    import sys
    source = tmp_path / "rows.jsonl"
    source.write_text(json.dumps({"id": "f", "question": "Biểu mẫu", "media_mode": "none",
                                  "wants_file": True, "expected_urls": [PARENT]}), encoding="utf-8")
    labels = tmp_path / "labels.json"
    monkeypatch.setattr(sys, "argv", ["eval_media", "--set", str(source), "--url-labels", str(labels)])
    calls = []
    monkeypatch.setattr(em, "ask", lambda *a: calls.append(a))
    for bad in ({"typo": {"page_urls": [PARENT]}}, {"f": None}, {"f": {"pages_urls": [PARENT]}}):
        labels.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            em.main()
    assert not calls


def test_source_link_audit_is_separate_from_golden_and_parent_association():
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [PARENT]}
    labels = {"page_urls": [PARENT], "source_file_urls": [FILE]}
    other = em.score_row(row, {"media_mode": "none", "text": [txt(OTHER, "file", PARENT)]}, labels)
    assert other["file_hit"] is False and other["attachment_parent_hit"] is True
    assert other["source_file_hit"] is False
    exact = em.score_row(row, {"media_mode": "none", "text": [txt(FILE, "file", OTHER)]}, labels)
    assert exact["file_hit"] is False and exact["source_file_hit"] is True
    assert em.summarize([other, exact])["counts"]["source_file_hit"] == "1/2"
    assert em.score_row(row, {}, {"page_urls": [PARENT]})["source_file_hit"] is None


def test_source_link_audit_rejects_invalid_values_and_page_as_file():
    import pytest
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [PARENT]}
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"source_file_urls": "not a list"})
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"source_file_urls": ["not-a-url"]})
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"page_urls": [PARENT], "source_file_urls": [PARENT]})


def test_source_link_audit_keeps_path_and_query_case():
    row = {"id": "f", "media_mode": "none", "wants_file": True, "expected_urls": [PARENT]}
    url = "https://example.org/Dir/FILE.pdf?download=YES"
    labels = {"source_file_urls": [url]}
    assert em.score_row(row, {"text": [txt(url.replace("Dir", "dir"), "file")]}, labels)["source_file_hit"] is False
    assert em.score_row(row, {"text": [txt(url.replace("YES", "yes"), "file")]}, labels)["source_file_hit"] is False
    assert em.score_row(row, {"text": [txt(url.replace("example.org", "EXAMPLE.ORG"), "file")]}, labels)["source_file_hit"] is True
    page_row = {**row, "expected_urls": [url]}
    different_file = url.replace("Dir", "dir")
    assert em.score_row(page_row, {"text": [txt(different_file, "file")]},
                        {"page_urls": [url], "source_file_urls": [different_file]})["source_file_hit"] is True


def test_file_audit_rejects_nonempty_labels_for_non_file_question():
    import pytest
    row = {"id": "f", "media_mode": "none", "wants_file": False, "expected_urls": [PARENT]}
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"page_urls": [PARENT]})
    with pytest.raises(ValueError):
        em.score_row(row, {}, {"source_file_urls": [FILE]})


def test_video_hit_needs_video_frame_of_right_post():
    row = {"id": "v1", "case": "video", "style": "question", "media_mode": "required", "wants_file": False,
           "min_media": 1, "expected_urls": [POST], "expected_media_docs": [POST]}
    r = em.score_row(row, {"media_mode": "required", "text": [], "media": [img(POST)], "sufficient": True})
    assert r["video_hit"] is False and r["media_on_target"] is True
    r = em.score_row(row, {"media_mode": "required", "text": [], "media": [img(POST, "video_frame")], "sufficient": True})
    assert r["video_hit"] is True
    r = em.score_row(row, {"media_mode": "required", "text": [], "media": [img(OTHER, "video"), img(POST, "video")],
                           "sufficient": True})
    assert r["video_hit"] is True and r["media_kinds"] == ["video", "video"]


def test_out_of_scope_abstain_and_no_media():
    row = {"id": "x1", "case": "out_of_scope", "style": "phrase", "media_mode": "required", "wants_file": False,
           "min_media": 0, "answerable": False, "expected_urls": [], "expected_media_docs": []}
    r = em.score_row(row, {"media_mode": "required", "text": [txt(OTHER)], "media": [img(OTHER)], "sufficient": True})
    assert r["abstain_ok"] is False and r["none_clean"] is False and r["url_hit"] is None
    assert r["required_has_media"] is None and r["false_abstain"] is None
    r = em.score_row(row, {"media_mode": "required", "text": [], "media": [], "sufficient": False})
    assert r["abstain_ok"] is True and r["none_clean"] is True


def test_summarize_rates_and_tables():
    rows = [
        {"id": "m1", "case": "image", "style": "question", "media_mode": "required", "wants_file": False,
         "min_media": 1, "expected_urls": [POST], "expected_media_docs": [POST]},
        {"id": "n1", "case": "none", "style": "no_accent", "media_mode": "none", "wants_file": False,
         "min_media": 0, "expected_urls": [POST], "expected_media_docs": []},
    ]
    scored = [em.score_row(rows[0], {"media_mode": "required", "text": [txt(POST)], "media": [img(POST), img(OTHER)],
                                     "sufficient": True}),
              em.score_row(rows[1], {"media_mode": "none", "text": [txt(OTHER), txt(POST)], "media": [], "sufficient": False})]
    s = em.summarize(scored)
    assert s["n"] == 2 and s["media_mode_acc"] == 1.0 and s["url_hit"] == 1.0
    assert s["text_mrr@10"] == 0.75 and s["media_precision"] == 0.5 and s["none_clean"] == 1.0
    assert s["required_has_media"] == 1.0 and s["false_abstain"] == 0.5
    assert set(s["by_case"]) == {"image", "none"} and s["by_style"]["no_accent"]["n"] == 1
    assert s["counts"]["none_clean"] == "1/1"
    assert em.failures(scored) == [{"id": "n1", "fail": ["false_abstain"]}]
    json.dumps(s)   # tóm tắt phải tuần tự hoá được


def test_media_v1_set_is_well_formed():
    rows = [json.loads(x) for x in open(ROOT / "eval" / "media_v1.jsonl", encoding="utf-8") if x.strip()]
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) >= 70
    for r in rows:
        assert r["media_mode"] in ("none", "optional", "required"), r["id"]
        assert r["case"] in ("image", "video", "file", "optional", "none", "out_of_scope"), r["id"]
        assert r["style"] in ("question", "phrase", "no_accent", "typo", "chatty", "abbrev", "english"), r["id"]
        if r["case"] == "out_of_scope":
            assert r["answerable"] is False and not r["expected_urls"] and not r["expected_media_docs"]
        else:
            assert r["expected_urls"] and r["reference"], r["id"]
        if r["media_mode"] == "required" and r["case"] != "out_of_scope":
            assert r["min_media"] >= 1 and r["expected_media_docs"], r["id"]
        if r["media_mode"] == "none":
            assert r["min_media"] == 0 and not r["expected_media_docs"], r["id"]
        if r["case"] == "file":
            assert r["wants_file"] and any("/media/" in u for u in r["expected_urls"]), r["id"]
            assert r["media_mode"] != "required", r["id"]
        if r.get("variant_of"):
            assert r["variant_of"] in ids, r["id"]
    cases = {c: sum(r["case"] == c for r in rows) for c in ("image", "video", "file", "optional", "none")}
    assert cases["image"] >= 25 and cases["video"] >= 8 and cases["file"] >= 8
    assert cases["optional"] >= 10 and cases["none"] >= 10
