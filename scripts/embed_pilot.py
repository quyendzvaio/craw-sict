"""Pilot chi phí embedding (D22): nhúng một mẫu nhỏ qua endpoint, đọc `usage`, ước tính toàn corpus rồi DỪNG.
Không ghi DB / Qdrant / S3; chỉ gọi endpoint embedding (EMBED_API_BASE/KEY, backend api).

  EMBED_BACKEND=api EMBED_API_BASE=... EMBED_API_KEY=... \
    python scripts/embed_pilot.py --artifacts _snapshot/2026-10-12/artifacts/v2-multilingual-e5-small \
        --snapshot _snapshot/2026-10-12 --n-text 50 --n-img 20

Chạy toàn bộ (compute) chỉ sau khi owner xem số ước tính và đồng ý.
"""
import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sict_crawler import embedder  # noqa: E402
from sict_crawler.compute import _jsonl_read as rows  # noqa: E402   (thiếu file → [])
from sict_crawler.config import get_settings  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, type=Path)
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--n-text", type=int, default=50)
    ap.add_argument("--n-img", type=int, default=20)
    a = ap.parse_args()
    e = get_settings().embedding
    if e.backend != "api" or not e.api_base:
        sys.exit("đặt EMBED_BACKEND=api và EMBED_API_BASE (+ EMBED_API_KEY) trước khi chạy pilot")
    random.seed(0)
    chunks, images, frames = (rows(a.artifacts / f"{n}.jsonl") for n in ("chunks", "images", "frames"))
    caps_i, caps_v = (rows(a.artifacts / f"captions_{n}.jsonl") for n in ("image", "video"))
    texts = [c["text"] for c in random.sample(chunks, min(a.n_text, len(chunks)))]
    imgs = [(a.snapshot / r["path"]).read_bytes() for r in random.sample(images, min(a.n_img, len(images)))]

    u = embedder.USAGE
    t0 = time.time()
    embedder.embed_texts(texts)
    t_tok = u["tokens"]
    vecs, ok = embedder.embed_image_bytes(imgs)
    i_tok = u["tokens"] - t_tok
    dt = time.time() - t0
    per_text, per_img = t_tok / max(1, len(texts)), i_tok / max(1, len(ok))
    print(f"mẫu: {len(texts)} chunk → {t_tok} token ({per_text:.0f}/chunk); "
          f"{len(ok)} ảnh → {i_tok} token ({per_img:.0f}/ảnh, image_tokens báo {u['image_tokens']}); "
          f"{dt:.1f}s, {u['requests']} lượt; dim={len(vecs[0]) if vecs else '?'}")
    n_t = len(chunks) + len(caps_i) + len(caps_v)
    n_i = len(images) + len(frames)
    est_t, est_i = per_text * n_t, per_img * n_i
    print(f"ước tính toàn corpus (cùng số lượng artifact cũ): chữ {n_t} mục ≈ {est_t / 1e6:.2f}M token; "
          f"ảnh/khung {n_i} ≈ {est_i / 1e6:.2f}M token; tổng ≈ {(est_t + est_i) / 1e6:.2f}M token")
    print("DỪNG: báo owner số trên (và hạng key/giá) trước khi chạy compute đầy đủ.")


if __name__ == "__main__":
    main()
