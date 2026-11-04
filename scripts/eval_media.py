"""Bộ đo media qua HTTP `POST /query` (stdlib): ảnh / video / file đính kèm / âm tính (eval/media_v1.jsonl).

Chấm trên nguồn trả về, không trên câu trả lời LLM, nên không phụ thuộc model. Tiêu chí từng loại xem
`eval/media_v1.md`. Mỗi dòng của bộ có `media_mode` mong đợi (none|optional|required), `wants_file`,
`expected_urls` (bài hoặc file đúng), `expected_media_docs` (bài mà ảnh/video trả về phải thuộc về), `min_media`.

Số đo (tổng và theo case / style):
  media_mode_acc      route.media_mode == mong đợi
  wants_file_acc      route.wants_file == mong đợi
  url_hit             expected_urls có trong nguồn chữ (url hoặc parent_url) hoặc media (parent_url)
  text_mrr@10         1/hạng của bài đúng đầu tiên trong nguồn chữ (mức tài liệu)
  required_has_media  câu required: số media trả về ≥ min_media
  media_on_target     câu có expected_media_docs: ≥ 1 media thuộc bài đúng (câu optional không có media = bỏ qua)
  media_precision     tỷ lệ media trả về thuộc bài đúng (micro: tổng media đúng / tổng media; chỉ câu có expected_media_docs)
  none_clean          câu none hoặc ngoài phạm vi: không có media
  file_hit            câu wants_file: có nguồn kind=file có url trong expected_urls
  video_hit           câu case=video: có media kind=video (1 slot/video, v2) hoặc video_frame (v1) thuộc bài đúng
  abstain_ok          câu ngoài phạm vi: sufficient == false;  false_abstain: câu trả lời được mà sufficient == false

Usage: python3 scripts/eval_media.py --api http://localhost:8003 [--set eval/media_v1.jsonl] [--top-k 8]
                                     [--out eval/results/media_v1_2026-11-05.json] [--only m001,v002]
Dòng cuối stdout là JSON tóm tắt (như scripts/eval_api.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import defaultdict


VIDEO_KINDS = ("video", "video_frame")


def norm(u: str | None) -> str:
    return re.sub(r"^https?://", "", u or "").replace("//", "/").rstrip("/").lower()


def doc_of(s: dict) -> str:
    """Bài chứa nguồn: file / ảnh / khung hình → parent_url; đoạn chữ → url."""
    return norm(s.get("parent_url") or s.get("url"))


def ask(api: str, q: str, timeout: int, top_k: int = 8) -> dict:
    body = json.dumps({"question": q, "top_k": top_k}).encode()
    req = urllib.request.Request(f"{api}/query", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def score_row(row: dict, res: dict) -> dict:
    """Chấm một câu (thuần, không mạng). res = JSON của /query. Trường None = tiêu chí không áp dụng cho câu này."""
    want = {norm(u) for u in row.get("expected_urls") or []}
    mdocs = {norm(u) for u in row.get("expected_media_docs") or []}
    text = res.get("text") or []
    media = res.get("media") or []
    oos = row.get("case") == "out_of_scope" or row.get("answerable") is False

    docs = list(dict.fromkeys(doc_of(s) for s in text))
    text_rank = next((i + 1 for i, d in enumerate(docs[:10]) if d in want), None)
    url_hit = None if not want else bool(
        text_rank or any(norm(s.get("url")) in want for s in text) or any(doc_of(m) in want for m in media))

    on_target = [m for m in media if doc_of(m) in mdocs]
    want_media = row["media_mode"] == "required" and not oos
    r = {
        "id": row["id"], "case": row.get("case"), "style": row.get("style"),
        "media_mode": res.get("media_mode"), "wants_file": bool(res.get("wants_file")),
        "n_text": len(text), "n_media": len(media), "n_media_on_target": len(on_target),
        "mode_ok": res.get("media_mode") == row["media_mode"],
        "wants_file_ok": bool(res.get("wants_file")) == bool(row.get("wants_file")),
        "text_rank": text_rank, "url_hit": url_hit,
        "required_has_media": (len(media) >= int(row.get("min_media") or 1)) if want_media else None,
        # optional: ảnh không bắt buộc, chỉ chấm khi có; required: không có media = sai
        "media_on_target": (bool(on_target) if (media or want_media) else None) if mdocs else None,
        "media_total": len(media) if mdocs else 0,
        "media_good": len(on_target) if mdocs else 0,
        "none_clean": (len(media) == 0) if (row["media_mode"] == "none" or oos) else None,
        "file_hit": None, "file_rank": None, "video_hit": None,
        "sufficient": res.get("sufficient"),
        "abstain_ok": (res.get("sufficient") is False) if oos else None,
        "false_abstain": (res.get("sufficient") is False) if (not oos and want) else None,
        "media_docs": list(dict.fromkeys(doc_of(m) for m in media))[:5],
        "top_docs": docs[:5],
    }
    if row.get("wants_file"):
        fr = next((i + 1 for i, s in enumerate(text) if s.get("kind") == "file" and norm(s.get("url")) in want), None)
        r["file_hit"], r["file_rank"] = fr is not None, fr
    if row.get("case") == "video" and not oos:
        # /query gộp mỗi video thành 1 nguồn kind=video (url YouTube, AC-R3); index v1 trả từng khung hình video_frame
        r["video_hit"] = any(m.get("kind") in VIDEO_KINDS and doc_of(m) in mdocs for m in media)
    r["media_kinds"] = [m.get("kind") for m in media]
    return r


def _rate(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(bool(v) for v in vals) / len(vals), 3) if vals else None


def _count(rows: list[dict], key: str) -> str:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return f"{sum(bool(v) for v in vals)}/{len(vals)}"


def summarize(scored: list[dict]) -> dict:
    """Tổng + bảng theo case và style. media_precision là micro (tổng media đúng / tổng media)."""
    def block(rows: list[dict]) -> dict:
        ranks = [r["text_rank"] for r in rows if r.get("url_hit") is not None]
        tot = sum(r["media_total"] for r in rows)
        return {
            "n": len(rows),
            "media_mode_acc": _rate(rows, "mode_ok"), "wants_file_acc": _rate(rows, "wants_file_ok"),
            "url_hit": _rate(rows, "url_hit"),
            "text_mrr@10": round(sum(1 / r for r in ranks if r) / len(ranks), 3) if ranks else None,
            "required_has_media": _rate(rows, "required_has_media"),
            "media_on_target": _rate(rows, "media_on_target"),
            "media_precision": round(sum(r["media_good"] for r in rows) / tot, 3) if tot else None,
            "none_clean": _rate(rows, "none_clean"), "file_hit": _rate(rows, "file_hit"),
            "video_hit": _rate(rows, "video_hit"),
            "abstain_ok": _rate(rows, "abstain_ok"), "false_abstain": _rate(rows, "false_abstain"),
        }

    out = block(scored)
    out["counts"] = {k: _count(scored, k) for k in ("mode_ok", "wants_file_ok", "url_hit", "required_has_media",
                                                     "media_on_target", "none_clean", "file_hit", "video_hit",
                                                     "abstain_ok", "false_abstain")}
    by_case: dict[str, list] = defaultdict(list)
    by_style: dict[str, list] = defaultdict(list)
    for r in scored:
        by_case[r.get("case") or "?"].append(r)
        by_style[r.get("style") or "?"].append(r)
    out["by_case"] = {k: block(v) for k, v in sorted(by_case.items())}
    out["by_style"] = {k: block(v) for k, v in sorted(by_style.items())}
    return out


def failures(scored: list[dict]) -> list[dict]:
    """Câu không đạt ít nhất một tiêu chí áp dụng cho nó."""
    keys = ("mode_ok", "wants_file_ok", "url_hit", "required_has_media", "media_on_target", "none_clean",
            "file_hit", "video_hit", "abstain_ok")
    out = []
    for r in scored:
        bad = [k for k in keys if r.get(k) is False] + (["false_abstain"] if r.get("false_abstain") else [])
        if bad:
            out.append({"id": r["id"], "fail": bad})
    return out


def _fmt(v) -> str:
    return "-" if v is None else ("OK" if v is True else ("X" if v is False else str(v)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8003")
    ap.add_argument("--set", default="eval/media_v1.jsonl")
    ap.add_argument("--top-k", type=int, default=8, help="top_k gửi /query (UI gửi 8)")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", help="ghi JSON chi tiết (từng câu + tóm tắt)")
    ap.add_argument("--only", help="chỉ chạy các id này, phẩy ngăn cách")
    args = ap.parse_args()
    rows = [json.loads(x) for x in open(args.set, encoding="utf-8") if x.strip()]
    if args.only:
        keep = set(args.only.split(","))
        rows = [r for r in rows if r["id"] in keep]
    scored = []
    for g in rows:
        try:
            res = ask(args.api, g["question"], args.timeout, args.top_k)
        except Exception as e:   # lỗi HTTP một câu không làm hỏng cả lượt chạy
            print(f"{g['id']} LỖI {e}", file=sys.stderr, flush=True)
            res = {"text": [], "media": [], "media_mode": None, "wants_file": None, "sufficient": None, "error": str(e)}
        r = score_row(g, res)
        r["question"] = g["question"]
        if res.get("error"):
            r["error"] = res["error"]
        scored.append(r)
        print(f"{r['id']} mode={r['media_mode']}{'' if r['mode_ok'] else '!'} file={int(r['wants_file'])}"
              f"{'' if r['wants_file_ok'] else '!'} rank={_fmt(r['text_rank'])} url={_fmt(r['url_hit'])} "
              f"media={r['n_media']}/{r['n_media_on_target']} tgt={_fmt(r['media_on_target'])} "
              f"clean={_fmt(r['none_clean'])} file_hit={_fmt(r['file_hit'])} video={_fmt(r['video_hit'])} "
              f"suff={r['sufficient']}", flush=True)
    summary = summarize(scored)
    summary["failures"] = failures(scored)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"api": args.api, "set": args.set, "top_k": args.top_k, "rows": scored, "summary": summary},
                      fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("by_case", "by_style", "failures")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
