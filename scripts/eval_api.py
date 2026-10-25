"""Bộ câu hỏi vàng qua HTTP `POST /query` (stdlib, chạy được ở bất kỳ đâu gọi tới API).

Kiểm tra sau deploy/cutover: recall@5/10 + MRR@10 mức tài liệu (url hoặc parent_url), độ chính xác
mức media (`media_mode`) của router, và với câu `required` có url đúng: media trả về thuộc bài đúng.
Có LLM key trên API thì câu hỏi được LLM viết lại — số liệu khác chạy offline (scripts/eval_retrieval.py).

Câu liệt kê (`--list eval/list_v1.jsonl`): độ phủ = tỷ lệ mục `expected_items` (mỗi mục là danh sách tên tương đương,
so khớp không dấu) có mặt trong `context` gửi LLM; kèm độ chính xác cờ `sufficient` (câu liệt kê phải true, câu ngoài
phạm vi phải false). Chấm trên ngữ cảnh chứ không trên câu trả lời, nên không phụ thuộc LLM trả lời.

Usage: python3 scripts/eval_api.py --api http://localhost:8003 --golden eval/golden_v1.jsonl
       python3 scripts/eval_api.py --api http://localhost:8003 --list eval/list_v1.jsonl [--top-k 8]
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
import urllib.request


def norm(u: str | None) -> str:
    return re.sub(r"^https?://", "", u or "").rstrip("/").lower()


def fold(text: str) -> str:
    """Như sict_crawler.core.text.fold (script chạy độc lập, không import gói)."""
    s = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d")


def ask(api: str, q: str, filters: dict | None, timeout: int, top_k: int = 10) -> dict:
    body = json.dumps({"question": q, "top_k": top_k, "filters": filters}).encode()
    req = urllib.request.Request(f"{api}/query", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8003")
    ap.add_argument("--golden", default="eval/golden_v1.jsonl")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--list", help="bộ câu liệt kê (eval/list_v1.jsonl): chấm độ phủ thay cho recall")
    ap.add_argument("--top-k", type=int, default=8, help="top_k cho --list (UI gửi 8)")
    args = ap.parse_args()
    if args.list:
        return eval_list(args)
    gold = [json.loads(x) for x in open(args.golden, encoding="utf-8") if x.strip()]
    ranks, mode_ok, media_hit, media_n = [], 0, 0, 0
    for g in gold:
        res = ask(args.api, g["question"], g.get("filters"), args.timeout)
        mode_ok += res.get("media_mode") == g["media_mode"]
        if not g.get("expected_urls"):
            continue
        want = {norm(u) for u in g["expected_urls"]}
        docs = list(dict.fromkeys(norm(s.get("parent_url") or s["url"]) for s in res["text"]))
        r = next((i for i, d in enumerate(docs[:10]) if d in want), None)
        ranks.append(r)
        if g["media_mode"] == "required":
            media_n += 1
            media_hit += bool({norm(m.get("parent_url")) for m in res["media"]} & want)
        print(f"{g['id']} rank={r} media_mode={res.get('media_mode')} n_media={len(res['media'])} "
              f"entities={res.get('entities')}", flush=True)
    n = len(ranks) or 1
    print(json.dumps({
        "n": len(ranks), "recall@5": round(sum(r is not None and r < 5 for r in ranks) / n, 3),
        "recall@10": round(sum(r is not None for r in ranks) / n, 3),
        "mrr@10": round(sum(1 / (r + 1) for r in ranks if r is not None) / n, 3),
        "media_mode_acc": round(mode_ok / len(gold), 3),
        "required_media_on_target": f"{media_hit}/{media_n}"}, ensure_ascii=False))


def eval_list(args) -> None:
    rows = [json.loads(x) for x in open(args.list, encoding="utf-8") if x.strip()]
    covs, suf_ok, out = [], 0, []
    for g in rows:
        res = ask(args.api, g["question"], g.get("filters"), args.timeout, args.top_k)
        ctx = fold(res.get("context", ""))
        hit = [any(fold(a) in ctx for a in alts) for alts in g["expected_items"]]
        want_suf = g["kind"] != "out_of_scope"
        suf_ok += bool(res.get("sufficient")) == want_suf
        cov = sum(hit) / len(hit) if hit else None
        if cov is not None:
            covs.append(cov)
        docs = len({norm(s.get("parent_url") or s["url"]) for s in res["text"]})
        missing = [alts[0] for alts, h in zip(g["expected_items"], hit) if not h]
        out.append({"id": g["id"], "coverage": cov, "sufficient": res.get("sufficient"), "docs": docs, "missing": missing})
        print(f"{g['id']} cover={'-' if cov is None else f'{sum(hit)}/{len(hit)}'} sufficient={res.get('sufficient')} "
              f"docs={docs} ctx={len(ctx)} thiếu={missing}", flush=True)
    print(json.dumps({"n_list": len(covs), "coverage": round(sum(covs) / (len(covs) or 1), 3),
                      "full_coverage": sum(c == 1 for c in covs), "sufficient_acc": round(suf_ok / len(rows), 3)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
