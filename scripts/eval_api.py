"""Bộ câu hỏi vàng qua HTTP `POST /query` (stdlib, chạy được ở bất kỳ đâu gọi tới API).

Kiểm tra sau deploy/cutover: recall@5/10 + MRR@10 mức tài liệu (url hoặc parent_url), độ chính xác
mức media (`media_mode`) của router, và với câu `required` có url đúng: media trả về thuộc bài đúng.
Có LLM key trên API thì câu hỏi được LLM viết lại — số liệu khác chạy offline (scripts/eval_retrieval.py).

Usage: python3 scripts/eval_api.py --api http://localhost:8003 --golden eval/golden_v1.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request


def norm(u: str | None) -> str:
    return re.sub(r"^https?://", "", u or "").rstrip("/").lower()


def ask(api: str, q: str, filters: dict | None, timeout: int) -> dict:
    body = json.dumps({"question": q, "top_k": 10, "filters": filters}).encode()
    req = urllib.request.Request(f"{api}/query", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8003")
    ap.add_argument("--golden", default="eval/golden_v1.jsonl")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
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


if __name__ == "__main__":
    main()
