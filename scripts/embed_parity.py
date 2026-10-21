"""Kiểm một endpoint embedding (VD dịch vụ ONNX int8 tự host) có dùng chung được index v3 hay không — CHẠY TRƯỚC
khi trỏ API / incremental sang endpoint đó.

Index v3 nhúng bằng jina-clip-v2 cloud (chữ) + fp32 tự host (ảnh, khớp cloud ~0,9996). Endpoint mới phải cho
vector đủ gần để (a) câu hỏi nhúng bằng nó vẫn tìm đúng trong index cũ, (b) bài / ảnh mới nhúng bằng nó nằm
cùng không gian với điểm cũ. Đo 3 thứ, so với artifact v3 (không gọi cloud, không ghi gì):
  1. chữ: cosine endpoint ↔ chunks.npy trên --n-text chunk ngẫu nhiên (chia theo độ dài, chunk dài lộ lỗi cắt token);
  2. ảnh: cosine endpoint ↔ images.npy trên --n-image ảnh (cùng tiền xử lý 512 px JPEG như pipeline);
  3. truy xuất: câu hỏi golden nhúng bằng endpoint vs bằng vector cache (cloud/fp32) → độ trùng top-10 chunk trên
     toàn bộ chunks.npy.
Ngưỡng mặc định (int8): cosine trung bình ≥ 0,98, thấp nhất ≥ 0,95, trùng top-10 ≥ 0,8 → PASS (exit 0), không thì
FAIL (exit 1). Đây là điều kiện cần, chưa đủ: sau đó vẫn chạy eval golden trên Qdrant v3.

    uv run python scripts/embed_parity.py --base http://localhost:8090/v1
    uv run python scripts/embed_parity.py --base http://localhost:8090/v1 --n-text 200 --n-image 100 --out eval/results/parity_int8.json
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sqlite3
import sys
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sict_crawler import embedder  # noqa: E402
from sict_crawler.config import get_settings  # noqa: E402

ART = "_snapshot/2026-10-12/artifacts/v3-jina-clip-v2"


def post(base: str, items: list[dict], batch: int = 16) -> np.ndarray:
    out = []
    for i in range(0, len(items), batch):
        r = httpx.post(base.rstrip("/") + "/embeddings", timeout=600, json={
            "model": "jina-clip-v2", "input": items[i:i + batch], "normalized": True, "dimensions": 1024})
        r.raise_for_status()
        out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    return np.asarray(out, dtype=np.float32)


def stats(x: np.ndarray) -> dict:
    return {"n": int(len(x)), "mean": round(float(x.mean()), 4), "min": round(float(x.min()), 4),
            "p05": round(float(np.percentile(x, 5)), 4)} if len(x) else {"n": 0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="VD http://localhost:8090/v1")
    ap.add_argument("--artifacts", default=ART)
    ap.add_argument("--snapshot", default=None, help="mặc định lấy từ manifest.json")
    ap.add_argument("--golden", default="eval/golden_v1.jsonl")
    ap.add_argument("--cache", default="/DATA/craw-sict-data/embed_cache/jina-clip-v2.sqlite")
    ap.add_argument("--n-text", type=int, default=60)
    ap.add_argument("--n-image", type=int, default=40)
    ap.add_argument("--min-mean", type=float, default=0.98)
    ap.add_argument("--min-cos", type=float, default=0.95)
    ap.add_argument("--min-overlap", type=float, default=0.8)
    ap.add_argument("--out", default=None, help="ghi kết quả JSON")
    a = ap.parse_args()
    art = Path(a.artifacts)
    man = json.loads((art / "manifest.json").read_text())
    snap = Path(a.snapshot or man.get("snapshot", "_snapshot/2026-10-12"))
    e = get_settings().embedding
    e.model, e.dim = man.get("text_model", "jina-clip-v2"), int(man.get("dim", 1024))
    prefix = man.get("query_prefix", e.query_prefix) or ""
    rnd = random.Random(0)
    res: dict = {"base": a.base, "artifacts": str(art)}

    chunks = [json.loads(line) for line in open(art / "chunks.jsonl", encoding="utf-8")]
    V = np.load(art / "chunks.npy")
    idx = rnd.sample(range(len(chunks)), min(a.n_text, len(chunks)))
    K = post(a.base, [{"text": chunks[i]["text"]} for i in idx])
    cos_t = (K * V[idx]).sum(1)
    L = np.array([len(chunks[i]["text"]) for i in idx])
    res["text"] = stats(cos_t)
    res["text_by_len"] = {f"{lo}-{hi}": stats(cos_t[(L >= lo) & (L < hi)])
                          for lo, hi in ((0, 900), (900, 1600), (1600, 10 ** 9))}

    images = [json.loads(line) for line in open(art / "images.jsonl", encoding="utf-8")]
    VI = np.load(art / "images.npy")
    items, ref = [], []
    for i in rnd.sample(range(len(images)), min(a.n_image, len(images))):
        p = snap / images[i]["path"]
        b = embedder.prepare_image(p.read_bytes()) if p.exists() else None
        if b:
            items.append({"image": base64.b64encode(b).decode()})
            ref.append(VI[i])
    cos_i = (post(a.base, items, batch=8) * np.stack(ref)).sum(1) if items else np.zeros(0)
    res["image"] = stats(cos_i)

    # truy xuất: câu hỏi golden đã có vector (cloud / fp32) trong cache → so top-10 trên toàn bộ chunk
    db = sqlite3.connect(f"file:{a.cache}?mode=ro", uri=True)
    qs, ref_q = [], []
    for line in open(a.golden, encoding="utf-8"):
        q = (json.loads(line).get("question") or "").strip() if line.strip() else ""
        row = db.execute("select v from vec where k=?", (embedder._key("t", (prefix + q).encode()),)).fetchone() if q else None
        if row:
            qs.append(prefix + q)
            ref_q.append(np.frombuffer(row[0], dtype=np.float32))
    if qs:
        QN = post(a.base, [{"text": q} for q in qs])
        top = lambda M: np.argsort(-(V @ M.T), axis=0)[:10].T   # noqa: E731
        ov = np.array([len(set(x) & set(y)) / 10 for x, y in zip(top(QN), top(np.stack(ref_q)))])
        res["query_cos"] = stats((QN * np.stack(ref_q)).sum(1))
        res["top10_overlap"] = stats(ov)
    else:
        res["top10_overlap"] = {"n": 0, "note": "cache không có vector câu hỏi golden — bỏ qua"}

    checks = [res["text"].get("mean", 0) >= a.min_mean, res["text"].get("min", 0) >= a.min_cos]
    if res["image"]["n"]:
        checks += [res["image"]["mean"] >= a.min_mean, res["image"]["min"] >= a.min_cos]
    if res["top10_overlap"]["n"]:
        checks.append(res["top10_overlap"]["mean"] >= a.min_overlap)
    res["pass"] = all(checks)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print("PASS" if res["pass"] else "FAIL")
    sys.exit(0 if res["pass"] else 1)


if __name__ == "__main__":
    main()
