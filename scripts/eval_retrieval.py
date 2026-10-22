"""Đánh giá retrieval offline trên artifact (compute.py) với bộ câu hỏi vàng (AC-G3).

Không cần Qdrant/LLM (không rewrite câu hỏi):
  dense         cosine brute-force trên chunks.npy (E5, query: prefix)
  bm25          BM25 trên chunk text đã bỏ dấu (xấp xỉ nhánh keyword `text_folded`, D8)
  hybrid        RRF(dense, bm25)
  hybrid+boost  hybrid rồi nhân ưu tiên mềm metadata (filters.apply_boost, D18)
Lọc cứng = trường `filters` của câu hỏi vàng (filters.matches). Metric mức TÀI LIỆU: url hoặc
parent_url của chunk (đính kèm tính về trang chứa nó), chuẩn hoá http/https và "/" cuối.
Câu hỏi thời gian không có URL cố định dùng `expect_any` (mục + khoảng ngày) — đạt nếu ≥1 tài liệu
top-5 khớp.

Usage (cần embedder → chạy trong image worker/api, hoặc venv có group ml):
  python scripts/eval_retrieval.py --artifacts _snapshot/2026-10-12/artifacts/v2-multilingual-e5-small \\
      --golden eval/golden_v1.jsonl --out eval/results
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sict_crawler.service.retrieval import filters as F  # noqa: E402

MODES = ("dense", "bm25", "hybrid", "hybrid+boost")
POOL = 200          # số chunk mỗi nhánh đưa vào fusion
RRF_K = 60
TODAY = date(2026, 10, 12)  # ngày snapshot — "năm nay"/"mới nhất" tính theo ngày này


def norm_url(u: str | None) -> str:
    return re.sub(r"^https?://", "", u or "").rstrip("/").lower()


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", F.fold(text)) if len(t) >= 2]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: list[Counter] = [Counter(tokens(d)) for d in docs]
        self.len = np.array([sum(c.values()) for c in self.tf], dtype=np.float32)
        self.avg = float(self.len.mean()) if len(self.len) else 1.0
        df = Counter()
        self.post: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.tf):
            for t in c:
                df[t] += 1
                self.post[t].append(i)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> np.ndarray:
        s = np.zeros(len(self.tf), dtype=np.float32)
        for t in set(tokens(query)):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.post[t]:
                f = self.tf[i][t]
                s[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        return s


def rrf(*rankings: list[int]) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    for r in rankings:
        for pos, i in enumerate(r):
            out[i] += 1.0 / (RRF_K + pos + 1)
    return out


def doc_key(c: dict) -> str:
    return norm_url(c.get("parent_url") or c.get("url"))


def rank_docs(order: list[int], chunks: list[dict], allowed: np.ndarray, top: int = 10) -> list[str]:
    seen, out = set(), []
    for i in order:
        if not allowed[i]:
            continue
        k = doc_key(chunks[i])
        if k not in seen:
            seen.add(k)
            out.append(k)
            if len(out) >= top:
                break
    return out


def expect_any_ok(docs: list[str], meta_by_doc: dict[str, dict], cond: dict) -> bool:
    for d in docs[:5]:
        m = meta_by_doc.get(d, {})
        if cond.get("section") and m.get("section") != cond["section"]:
            continue
        pub = (m.get("published_at") or "")[:10]
        if cond.get("published_after") and (not pub or pub < cond["published_after"]):
            continue
        if cond.get("published_before") and (not pub or pub >= cond["published_before"]):
            continue
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--golden", default="eval/golden_v1.jsonl")
    ap.add_argument("--out", default=None, help="thư mục ghi <model>.json (chi tiết từng câu)")
    args = ap.parse_args()

    art = Path(args.artifacts)
    manifest = json.loads((art / "manifest.json").read_text())
    chunks = [json.loads(x) for x in (art / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    vecs = np.load(art / "chunks.npy").astype(np.float32)
    assert len(chunks) == vecs.shape[0], "chunks.jsonl và chunks.npy lệch nhau"
    golden = [json.loads(x) for x in Path(args.golden).read_text(encoding="utf-8").splitlines() if x.strip()]

    from sict_crawler.core.config import get_settings
    from sict_crawler.service.embedding import embedder
    get_settings().embedding.text_model = manifest["text_model"]
    print(f"model={manifest['text_model']} chunks={len(chunks)} device={embedder.get_device()}", flush=True)

    bm25 = BM25([c["text"] for c in chunks])
    meta_by_doc = {}
    for c in chunks:
        meta_by_doc.setdefault(doc_key(c), c)
    known_docs = set(meta_by_doc)

    per_mode: dict[str, list[dict]] = {m: [] for m in MODES}
    for g in golden:
        qv = np.asarray(embedder.embed_query(g["question"]), dtype=np.float32)
        dense = vecs @ qv
        sparse = bm25.scores(g["question"])
        hard = F.from_request(g.get("filters"))
        allowed = np.array([F.matches(c, hard) for c in chunks]) if hard.has_hard() \
            else np.ones(len(chunks), dtype=bool)
        d_order = list(np.argsort(-dense)[:POOL])
        s_order = [i for i in np.argsort(-sparse)[:POOL] if sparse[i] > 0]
        fused = rrf(d_order, s_order)
        h_order = sorted(fused, key=lambda i: -fused[i])
        soft = F.infer(g["question"], TODAY)
        boosted = F.apply_boost([{**chunks[i], "_i": i, "score": fused[i]} for i in h_order], soft,
                                today=TODAY)
        orders = {"dense": d_order, "bm25": s_order, "hybrid": h_order,
                  "hybrid+boost": [h["_i"] for h in boosted]}
        expected = {norm_url(u) for u in g["expected_urls"]}
        for mode, order in orders.items():
            docs = rank_docs(order, chunks, allowed)
            rank = next((k + 1 for k, d in enumerate(docs) if d in expected), None)
            ok_any = expect_any_ok(docs, meta_by_doc, g["expect_any"]) if g.get("expect_any") else None
            per_mode[mode].append({"id": g["id"], "intent": g["intent"], "rank": rank,
                                   "expect_any_ok": ok_any, "top": docs[:5],
                                   "expected_indexed": bool(expected & known_docs) if expected else None})

    def summarize(rows: list[dict]) -> dict:
        url_rows = [r for r in rows if r["expected_indexed"]]
        n = len(url_rows) or 1
        r5 = sum(1 for r in url_rows if r["rank"] and r["rank"] <= 5) / n
        r10 = sum(1 for r in url_rows if r["rank"] and r["rank"] <= 10) / n
        mrr = sum(1 / r["rank"] for r in url_rows if r["rank"]) / n
        anys = [r for r in rows if r["expect_any_ok"] is not None]
        return {"n_url": len(url_rows), "recall@5": round(r5, 3), "recall@10": round(r10, 3),
                "mrr@10": round(mrr, 3),
                "expect_any": f"{sum(r['expect_any_ok'] for r in anys)}/{len(anys)}" if anys else "-"}

    report = {"model": manifest["text_model"], "chunks": len(chunks),
              "not_indexed": [r["id"] for r in per_mode["dense"] if r["expected_indexed"] is False],
              "modes": {m: summarize(rows) for m, rows in per_mode.items()},
              "by_intent": {m: {it: summarize([r for r in rows if r["intent"] == it])
                                for it in sorted({r["intent"] for r in rows})}
                            for m, rows in per_mode.items()}}
    print(f"\n| mode | recall@5 | recall@10 | MRR@10 | expect_any |\n|---|---|---|---|---|")
    for m, s in report["modes"].items():
        print(f"| {m} | {s['recall@5']} | {s['recall@10']} | {s['mrr@10']} | {s['expect_any']} |")
    print(f"\nby intent (hybrid+boost, recall@5): " + ", ".join(
        f"{it}={s['recall@5']}" for it, s in report["by_intent"]["hybrid+boost"].items()))
    if report["not_indexed"]:
        print(f"URL đáp án không có trong index: {report['not_indexed']}")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        name = manifest["text_model"].rsplit("/", 1)[-1]
        (out / f"{name}.json").write_text(json.dumps({**report, "per_question": per_mode},
                                                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
