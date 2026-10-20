"""So sánh truy xuất BM25 vs jina-clip-v2 vs BAAI/bge-m3 trên 12 câu hỏi kiểm thử (D24).

Ba bước, mỗi bước chạy riêng (embed rất nặng trên CPU, chạy nền; có checkpoint nên chạy lại tiếp tục):

  corpus  Dựng corpus THU GỌN: chunk của tài liệu đáp án (PDF chunk theo trang — số trang thật) + chunk gây
          nhiễu lấy ngẫu nhiên từ artifact v2 (+ vài PDF nhiều trang khác) + ảnh của câu hỏi thị giác + ảnh nhiễu.
          Cùng một corpus cho cả ba phương pháp.  (CPU nhẹ, không cần ML)
  embed   Nhúng chunk / câu hỏi / ảnh (CPU). jina-clip-v2 chạy bằng ONNX Runtime (đường tự host của D22): câu hỏi
          thêm tiền tố query, chunk/ảnh không tiền tố (cờ phía query/document); bge-m3 không cần tiền tố.
          Cần ML + onnxruntime (image worker).
  score   Tính Recall@1/5/10, MRR@10 (mức tài liệu), Recall mức TRANG (PDF), mức ảnh (câu thị giác); ghi
          compare_q12.json + COMPARE.md.

  python scripts/eval_compare.py corpus --out eval/results/compare
  python scripts/eval_compare.py embed  --out eval/results/compare --model jina
  python scripts/eval_compare.py embed  --out eval/results/compare --model bge-m3
  python scripts/eval_compare.py score  --out eval/results/compare

Không ghi PG / Qdrant / S3; không gọi API ngoài (chỉ tải model từ HF khi `embed`).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_retrieval import BM25, norm_url, rrf  # noqa: E402

SNAP = Path("_snapshot/2026-10-12")
ART = SNAP / "artifacts/v2-multilingual-e5-small"
KS = (1, 5, 10)


def _read(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write(p: Path, rows: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ corpus
def cmd_corpus(a) -> None:
    from sict_crawler import compute as C
    from sict_crawler import indexing as ix
    from sict_crawler.config import get_settings

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(a.seed)
    qs = _read(Path(a.questions))
    targets = {u for q in qs for u in q["expected_urls"]}
    docs = {d["url"]: d for d in _read(ART / "documents.jsonl")}
    ch = get_settings().chunking
    ch.structured, ch.semantic = True, False            # chunk file theo trang (D24)

    def paged(url: str) -> list[dict]:
        d = docs[url]
        pages, st = ix.extract_file_pages(SNAP / d["file_path"], ocr_scanned=False)
        if st != "ok":
            return []
        return [C.chunk_row(c) for c in C.doc_chunks(d, "\n\n".join(t for _, t in pages), False, pages=pages)]

    rows: list[dict] = []

    def add(r: dict, target: bool) -> None:
        rows.append({"cid": len(rows), "chunk_id": r["chunk_id"], "url": r["url"], "parent_url": r.get("parent_url"),
                     "page": r.get("page"), "title": r["title"], "kind": r["kind"], "text": r["text"],
                     "target": target})

    attach_targets = [u for u in targets if docs.get(u, {}).get("doc_type") == "attachment"]
    for u in attach_targets:
        for r in paged(u):
            add(r, True)
    web_targets = targets - set(attach_targets)
    chunks = _read(ART / "chunks.jsonl")
    for r in chunks:
        if r["url"] in web_targets:
            add(r, True)
    # PDF nhiều trang khác làm nhiễu (có số trang thật)
    pdfs = [u for u, d in docs.items() if d.get("doc_type") == "attachment" and u not in targets
            and ix.sniff_format(SNAP / d["file_path"]) == "pdf"]
    rnd.shuffle(pdfs)
    paged_extra = 0
    for u in pdfs:
        if paged_extra >= a.extra_pdfs:
            break
        rs = paged(u)
        if len({r.get("page") for r in rs}) >= 3:
            for r in rs:
                add(r, False)
            paged_extra += 1
    skip_urls = targets | {r["url"] for r in rows}
    pool = [r for r in chunks if r["url"] not in skip_urls and len(r["text"]) >= 80]
    for r in rnd.sample(pool, min(a.distractors, len(pool))):
        add(r, False)

    # ---- ảnh: ảnh trong tài liệu của câu thị giác + ảnh nhiễu (cùng ảnh có thể nằm trong nhiều bài)
    img_docs = {u for q in qs for u in q.get("expected_image_docs", [])}
    images = {r["sha256"]: r for r in _read(SNAP / "artifacts/v2-multilingual-e5-small/images.jsonl")}
    caps: dict[str, list[dict]] = defaultdict(list)
    for c in _read(ART / "captions_image.jsonl"):
        caps[c["sha256"]].append(c)
    want = [s for s, cs in caps.items() if s in images and any(c["url"] in img_docs for c in cs)]
    rest = [s for s in caps if s in images and s not in set(want)]
    keep = want + rnd.sample(rest, min(a.distractor_images, len(rest)))
    irows = []
    for i, s in enumerate(keep):
        cs = caps[s]
        irows.append({"iid": i, "sha256": s, "path": images[s]["path"], "docs": sorted({c["url"] for c in cs}),
                      "text": " ".join(dict.fromkeys(c["text"] for c in cs))[:900], "target": s in set(want)})
    _write(out / "corpus.jsonl", rows)
    _write(out / "images.jsonl", irows)
    (out / "manifest.json").write_text(json.dumps({
        "chunks": len(rows), "target_chunks": sum(r["target"] for r in rows), "pdf_chunks": sum(r["page"] is not None for r in rows),
        "images": len(irows), "target_images": len(want), "seed": a.seed, "questions": a.questions,
        "avg_chars": sum(len(r["text"]) for r in rows) / max(1, len(rows))}, indent=2), encoding="utf-8")
    print((out / "manifest.json").read_text())


class JinaOnnx:
    """jina-clip-v2 qua ONNX Runtime CPU. Graph gộp hai tháp (input_ids, pixel_values → 4 đầu ra); mỗi lượt chỉ
    yêu cầu đầu ra cần dùng nên ORT chỉ chạy tháp tương ứng, tháp kia nhận đầu vào giả."""

    def __init__(self, rel_path: str, max_length: int, threads: int):
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer
        root = Path(snapshot_download("jinaai/jina-clip-v2", allow_patterns=[rel_path, rel_path + "_data", "tokenizer.json",
                                                                              "preprocessor_config.json"]))
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(root / rel_path), so, providers=["CPUExecutionProvider"])
        outs = [o.name for o in self.sess.get_outputs()]
        self.out_text = next(n for n in outs if "text" in n and "l2norm" in n)
        self.out_image = next(n for n in outs if "image" in n and "l2norm" in n)
        self.tok = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tok.enable_truncation(max_length)
        self.tok.enable_padding(pad_id=1, pad_token="<pad>")
        pc = json.loads((root / "preprocessor_config.json").read_text())
        self.size = pc.get("size", {}).get("shortest_edge", 512) if isinstance(pc.get("size"), dict) else int(pc.get("size", 512))
        self.mean, self.std = np.array(pc["mean"], np.float32), np.array(pc["std"], np.float32)
        print("onnx IO:", [(i.name, i.shape) for i in self.sess.get_inputs()], outs, flush=True)

    def encode_text(self, texts: list[str], batch: int) -> np.ndarray:
        out = []
        for s in range(0, len(texts), batch):
            ids = np.array([e.ids for e in self.tok.encode_batch(texts[s:s + batch])], dtype=np.int64)
            dummy = np.zeros((len(ids), 3, self.size, self.size), np.float32)   # batch phải bằng nhau; tháp ảnh không chạy
            out.append(self.sess.run([self.out_text], {"input_ids": ids, "pixel_values": dummy})[0])
        return np.concatenate(out)

    def _prep(self, im) -> np.ndarray:
        from PIL import Image
        im = im.convert("RGB")
        w, h = im.size
        sc = self.size / min(w, h)
        im = im.resize((max(self.size, round(w * sc)), max(self.size, round(h * sc))), Image.BICUBIC)
        w, h = im.size
        l, t = (w - self.size) // 2, (h - self.size) // 2
        a = (np.asarray(im.crop((l, t, l + self.size, t + self.size)), np.float32) / 255.0 - self.mean) / self.std
        return a.transpose(2, 0, 1)

    def encode_image(self, ims: list, batch: int) -> np.ndarray:
        out = []
        for s in range(0, len(ims), batch):
            px = np.stack([self._prep(i) for i in ims[s:s + batch]])
            ids = np.ones((len(px), 4), dtype=np.int64)                          # tháp chữ không chạy
            out.append(self.sess.run([self.out_image], {"input_ids": ids, "pixel_values": px})[0])
        return np.concatenate(out)


# ------------------------------------------------------------------ embed
def cmd_embed(a) -> None:
    import torch
    out = Path(a.out)
    torch.set_num_threads(a.threads or torch.get_num_threads())
    qs = _read(Path(a.questions))
    corpus = _read(out / "corpus.jsonl")
    images = _read(out / "images.jsonl")
    chunk_texts = [r["text"] for r in corpus] + [r["text"] for r in images]      # chunk + caption ảnh (dạng chữ)
    stats = {"model": a.model, "max_length": a.max_length, "threads": torch.get_num_threads()}
    t0 = time.time()
    if a.model == "jina":
        # ONNX Runtime (đúng đường tự host dự định ở D22): query thêm tiền tố tay, chunk/ảnh không tiền tố
        m = JinaOnnx(a.onnx, a.max_length, a.threads or torch.get_num_threads())
        prefix = a.query_prefix
        encode_docs = lambda t: m.encode_text(t, a.batch)                          # noqa: E731  phía document
        encode_queries = lambda t: m.encode_text([prefix + x for x in t], a.batch)  # noqa: E731  phía query
    else:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("BAAI/bge-m3", device="cpu")
        m.max_seq_length = a.max_length
        encode_docs = encode_queries = lambda t: m.encode(t, batch_size=a.batch, normalize_embeddings=True)  # noqa: E731
    stats["load_s"] = round(time.time() - t0, 1)

    def run(name: str, texts: list[str], fn, step: int = 32):
        """Nhúng theo đoạn, sắp theo độ dài (ít padding), checkpoint sau mỗi đoạn → chạy lại tiếp tục."""
        path = out / f"vec_{a.model}_{name}.npy"
        if path.exists():
            print(f"[{a.model}] {name}: đã có"); return np.load(path)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        part = out / f"vec_{a.model}_{name}.partial.npz"
        done = {}
        if part.exists():
            z = np.load(part); done = dict(zip(z["idx"].tolist(), z["vec"]))
        t = time.time()
        for s in range(0, len(order), step):
            idx = [i for i in order[s:s + step] if i not in done]
            if not idx:
                continue
            v = np.asarray(fn([texts[i] for i in idx]), dtype=np.float32)
            v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
            done.update(zip(idx, v))
            np.savez(part, idx=np.array(list(done)), vec=np.stack(list(done.values())))
            el = time.time() - t
            print(f"[{a.model}] {name} {len(done)}/{len(texts)} {el:.0f}s ({el / max(1, len(done)):.2f}s/mục)", flush=True)
        arr = np.stack([done[i] for i in range(len(texts))])
        np.save(path, arr)
        part.unlink(missing_ok=True)
        stats[f"{name}_s"] = round(time.time() - t, 1)
        return arr

    run("queries", [q["question"] for q in qs], encode_queries)
    run("chunks", chunk_texts, encode_docs)
    if a.model == "jina":                         # nhánh ảnh: vector ảnh cùng không gian với chữ
        from PIL import Image
        def enc_img(paths):
            return m.encode_image([Image.open(SNAP / p) for p in paths], max(1, a.batch // 2))
        run("images", [r["path"] for r in images], enc_img, step=16)
    import resource
    stats["max_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    (out / f"embed_stats_{a.model}.json").write_text(json.dumps(stats, indent=2))
    print(stats)


# ------------------------------------------------------------------ score
def _ids(r: dict) -> set[str]:
    return {norm_url(r.get("url")), norm_url(r.get("parent_url"))} - {""}


def _first_rank(ranked: list[bool]) -> int | None:
    return next((i + 1 for i, ok in enumerate(ranked) if ok), None)


def _metrics(ranks: list[int | None]) -> dict:
    n = max(1, len(ranks))
    return {**{f"R@{k}": round(sum(1 for r in ranks if r and r <= k) / n, 3) for k in KS},
            "MRR@10": round(sum(1 / r for r in ranks if r and r <= 10) / n, 3), "n": len(ranks)}


def cmd_score(a) -> None:
    out = Path(a.out)
    qs = _read(Path(a.questions))
    corpus, images = _read(out / "corpus.jsonl"), _read(out / "images.jsonl")
    texts = [r["text"] for r in corpus]
    cap_texts = [r["text"] for r in images]
    n_c = len(corpus)
    bm_c, bm_i = BM25(texts), BM25(cap_texts)
    S: dict[str, dict[str, list[np.ndarray]]] = {"chunks": {}, "images": {}}     # method → [điểm / câu]
    S["chunks"]["bm25"] = [bm_c.scores(q["question"]) for q in qs]
    S["images"]["bm25 (caption)"] = [bm_i.scores(q["question"]) for q in qs]
    have = []
    for model, label in (("jina", "jina-clip-v2"), ("bge-m3", "bge-m3")):
        qp, cp = out / f"vec_{model}_queries.npy", out / f"vec_{model}_chunks.npy"
        if not (qp.exists() and cp.exists()):
            print(f"thiếu vector {model} — bỏ qua"); continue
        have.append((model, label))
        Q, Cv = np.load(qp), np.load(cp)
        S["chunks"][label] = [Cv[:n_c] @ q for q in Q]
        S["images"][f"{label} (caption)"] = [Cv[n_c:] @ q for q in Q]
        if model == "jina" and (out / "vec_jina_images.npy").exists():
            Iv = np.load(out / "vec_jina_images.npy")
            S["images"]["jina-clip-v2 (ảnh)"] = [Iv @ q for q in Q]
    for _, label in have:                         # hybrid tham khảo: BM25 + dense (RRF)
        S["chunks"][f"bm25+{label}"] = [None] * len(qs)
    methods = list(S["chunks"])

    def chunk_scores(method: str, i: int) -> np.ndarray:
        if method.startswith("bm25+"):
            lab = method[5:]
            a_, b_ = S["chunks"]["bm25"][i], S["chunks"][lab][i]
            r = rrf(list(np.argsort(-a_)[:200]), list(np.argsort(-b_)[:200]))
            s = np.zeros(n_c, dtype=np.float32)
            for k, v in r.items():
                s[k] = v
            return s
        return S["chunks"][method][i]

    res: dict = {"methods": {}, "per_question": {}}
    rows_doc: dict[str, list] = defaultdict(list)
    rows_page: dict[str, list] = defaultdict(list)
    rows_img: dict[str, list] = defaultdict(list)
    for m in methods:
        for i, q in enumerate(qs):
            sc = chunk_scores(m, i)
            order = np.argsort(-sc)
            exp = {norm_url(u) for u in q["expected_urls"]}
            seen, docs_ok = set(), []
            for j in order:                                     # xếp hạng mức tài liệu (dedupe)
                key = norm_url(corpus[j]["url"])
                if key in seen:
                    continue
                seen.add(key)
                docs_ok.append(bool(_ids(corpus[j]) & exp))
                if len(docs_ok) >= 10:
                    break
            rd = _first_rank(docs_ok)
            res["per_question"].setdefault(q["id"], {"question": q["question"], "type": q["type"]})[m] = {"doc_rank": rd}
            if q["type"] != "visual":
                rows_doc[m].append(rd)
            if q.get("expected_page"):                          # mức trang: chunk đầu tiên đúng (tài liệu, trang)
                pg = []
                seen_p = set()
                for j in order:
                    k = (norm_url(corpus[j]["url"]), corpus[j].get("page"))
                    if k in seen_p:
                        continue
                    seen_p.add(k)
                    pg.append(bool(_ids(corpus[j]) & exp) and corpus[j].get("page") == q["expected_page"])
                    if len(pg) >= 10:
                        break
                rp = _first_rank(pg)
                rows_page[m].append(rp)
                res["per_question"][q["id"]][m]["page_rank"] = rp
    for m in S["images"]:
        for i, q in enumerate(qs):
            if q["type"] != "visual":
                continue
            exp = set(q["expected_image_docs"])
            order = np.argsort(-S["images"][m][i])[:10]
            ri = _first_rank([bool(set(images[j]["docs"]) & exp) for j in order])
            rows_img[m].append(ri)
            res["per_question"][q["id"]][m] = {"image_rank": ri}
    res["methods"]["doc (10 câu chữ/PDF)"] = {m: _metrics(r) for m, r in rows_doc.items()}
    res["methods"]["page (4 câu PDF)"] = {m: _metrics(r) for m, r in rows_page.items()}
    res["methods"]["image (2 câu thị giác)"] = {m: _metrics(r) for m, r in rows_img.items()}
    man = json.loads((out / "manifest.json").read_text())
    res["corpus"] = man
    res["embed"] = {f.stem.replace("embed_stats_", ""): json.loads(f.read_text()) for f in out.glob("embed_stats_*.json")}
    (out.parent / "compare_q12.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    (out.parent / "COMPARE.md").write_text(_markdown(res, qs), encoding="utf-8")
    print((out.parent / "COMPARE.md").read_text())


def _markdown(res: dict, qs: list[dict]) -> str:
    L = ["# So sánh truy xuất: BM25 vs jina-clip-v2 vs bge-m3 (12 câu, corpus thu gọn)", "",
         f"Corpus: {res['corpus']['chunks']} chunk ({res['corpus']['target_chunks']} chunk của tài liệu đáp án, "
         f"{res['corpus']['pdf_chunks']} chunk PDF theo trang), {res['corpus']['images']} ảnh "
         f"({res['corpus']['target_images']} ảnh của câu thị giác). Câu hỏi: `eval/compare_q12.jsonl` (assistant soạn, draft). "
         "**12 câu là mẫu rất nhỏ — dùng làm chỉ báo, không kết luận chất lượng tổng thể.**", ""]
    for title, t in res["methods"].items():
        if not t:
            continue
        L += [f"## {title}", "", "| phương pháp | R@1 | R@5 | R@10 | MRR@10 | n |", "|---|---|---|---|---|---|"]
        L += [f"| {m} | {v['R@1']} | {v['R@5']} | {v['R@10']} | {v['MRR@10']} | {v['n']} |" for m, v in t.items()]
        L.append("")
    L += ["## Từng câu (hạng của kết quả đúng đầu tiên, `-` = ngoài top 10)", ""]
    ms = sorted({m for q in res["per_question"].values() for m in q if m not in ("question", "type")})
    L += ["| câu | loại | " + " | ".join(ms) + " |", "|---|---|" + "---|" * len(ms)]
    f = lambda v: "-" if v is None else str(v)      # noqa: E731
    for q in qs:
        pq = res["per_question"][q["id"]]
        cells = []
        for m in ms:
            v = pq.get(m, {})
            cells.append(f(v.get("page_rank", v.get("image_rank", v.get("doc_rank")))) if v else "")
        L.append(f"| {q['id']} {q['question'][:50]}… | {q['type']} | " + " | ".join(cells) + " |")
    for k, v in res.get("embed", {}).items():
        L += ["", f"Nhúng {k}: " + ", ".join(f"{a}={b}" for a, b in v.items())]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("corpus", "embed", "score"):
        p = sub.add_parser(name)
        p.add_argument("--out", default="eval/results/compare")
        p.add_argument("--questions", default="eval/compare_q12.jsonl")
        if name == "corpus":
            p.add_argument("--distractors", type=int, default=1200)
            p.add_argument("--extra-pdfs", type=int, default=6)
            p.add_argument("--distractor-images", type=int, default=300)
            p.add_argument("--seed", type=int, default=0)
        if name == "embed":
            p.add_argument("--model", choices=("jina", "bge-m3"), required=True)
            p.add_argument("--max-length", type=int, default=384)
            p.add_argument("--batch", type=int, default=8)
            p.add_argument("--threads", type=int, default=0)
            p.add_argument("--onnx", default="onnx/model.onnx", help="jina: đường dẫn graph ONNX (fp32 hoặc int8) trong HF cache")
            p.add_argument("--query-prefix", default="Represent the query for retrieving evidence documents: ",
                           help="jina: tiền tố phía query (ONNX không có LoRA task)")
    a = ap.parse_args()
    {"corpus": cmd_corpus, "embed": cmd_embed, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
