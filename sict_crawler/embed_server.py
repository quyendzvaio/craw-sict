"""Dịch vụ embedding tự host jina-clip-v2 — ONNX Runtime int8, CPU, không torch (D22 "các lần sau tự host").

Cùng giao diện Jina `/v1/embeddings` nên `embedder` backend `api` chỉ cần đổi EMBED_API_BASE (đổi config, không
đổi code):
  request  {model, input:[{"text"}|{"image": <base64>}], normalized, dimensions}
  response {model, object, data:[{object, index, embedding}], usage:{total_tokens, texts, images, seconds}}
`GET /` (và `/healthz`) → trạng thái. Vector luôn chuẩn hoá L2; `dimensions` < 1024 = cắt Matryoshka rồi chuẩn hoá lại.

Model (build_towers, 1 lần, cache trong EMBED_BUILD_DIR): ONNX fp32 chính thức của repo HF `jinaai/jina-clip-v2`
(`onnx/model.onnx` + `model.onnx_data` 3,5 GB, ghim revision; 1 graph cho cả 2 tháp: vào input_ids + pixel_values) →
tách thành tháp chữ / tháp ảnh (ORT không tỉa tháp không dùng: câu ngắn tốn ngang 1 ảnh, RAM phình tới OOM) → mỗi
tháp lượng tử hoá int8 CHỈ TRỌNG SỐ (MatMulNBits 8-bit, block 32; ACC_LEVEL theo tháp) + bảng từ vựng int8 theo
hàng (quantize_vocab_table). Đo 2026-10-20 (cosine với vector cloud / fp32 của index v3):
  - `onnx/model_int8.onnx` chính thức: chữ 0,54–0,61 (hỏng), ảnh 0,98;
  - tự quantize_dynamic (MatMul int8, per-channel, activation int8): chữ 0,36 — activation của tháp chữ có giá trị
    ngoại lai lớn, lượng tử activation phá vector;
  - int8 chỉ trọng số: chữ 0,9990 (min 0,996), câu hỏi 0,9995, ảnh 1,0000 (min 0,9989) → dùng cách này.
`EMBED_QUANT=fp32` = giữ fp32 (không lượng tử). tokenizer.json + preprocessor_config.json lấy cùng revision.

Khớp với vector cloud / fp32 (index v3 được nhúng bằng 2 nguồn đó):
  - chữ: cắt EMBED_TEXT_MAX_LEN token (mặc định 2048 cho máy 8 GB — xem TEXT_MAX_LEN; cloud nhận 8192, HF mặc
    định chỉ 512 → chunk dài lệch, đã gặp khi dựng bản Kaggle);
  - ảnh: y hệt JinaCLIPImageProcessor (resize cạnh ngắn = 512 bicubic → cắt giữa 512 → /255 → mean/std CLIP);
  - int8 không cho vector TRÙNG fp32 (≈ 0,999): trước khi trỏ API/incremental sang dịch vụ này phải chạy
    `scripts/embed_parity.py` (cosine với vector cloud) + eval golden trên index v3 — xem docstring script đó.

Chạy (cần group `embed`; đã thử local 2026-10-20 trên giới hạn 2 vCPU / 8 GB):
    uv run --group embed python -m sict_crawler.embed_server build      # 1 lần: tải fp32 + tách + lượng tử (~2 phút, RAM ~6 GB)
    uv run --group embed python -m sict_crawler.embed_server --port 8090   # chưa build thì tự build lúc khởi động
    docker compose -f docker-compose.embed.yml up -d --build            # Dockerfile.embed
Env: EMBED_ONNX_REPO, EMBED_ONNX_REVISION, EMBED_ONNX_DIR (thư mục có sẵn file fp32, bỏ qua tải), EMBED_BUILD_DIR
(nơi ghi 2 tháp đã lượng tử, mặc định $HF_HOME/sict-embed/<revision>-<quant>), EMBED_QUANT (w8 | fp32),
EMBED_THREADS (intra-op, mặc định số CPU), EMBED_TEXT_MAX_LEN (2048), EMBED_TOKEN_BUDGET (token / 1 lượt chạy),
EMBED_CONCURRENCY (1),
EMBED_IMAGE_BATCH, EMBED_MEM_ARENA=1 (bật arena ORT: nhanh hơn chút, RAM giữ đỉnh).
"""
import argparse
import base64
import io
import json
import os
import threading
import time
from pathlib import Path

import numpy as np

REPO = os.environ.get("EMBED_ONNX_REPO", "jinaai/jina-clip-v2")
REVISION = os.environ.get("EMBED_ONNX_REVISION", "e10d47f5691d0454a0fb5d13f46f2199b74cb436")  # main 2026-10-20
SRC_FILE = "onnx/model.onnx"   # fp32 + model.onnx_data; nguồn để tách + lượng tử (build_towers)
QUANT = os.environ.get("EMBED_QUANT", "w8")   # w8 = int8 chỉ trọng số (MatMulNBits 8-bit block 32) | fp32
QUANT_BLOCK = 32
# accuracy_level của MatMulNBits theo tháp. Câu hỏi (~28 token, 2 vCPU, đo 2026-10-20): level 0 (giải nén trọng số về
# fp32 mỗi lần nhân) 724 ms; level 4 (activation int8 theo block 32) 290 ms, cosine 0,999 — fp32 gốc 248 ms (tháp chữ
# ~560M tham số: ~31 GFLOP/câu, gần giới hạn tính toán của 2 vCPU; int8 ở đây tiết kiệm RAM chứ không nhanh hơn fp32).
# Ảnh giữ level 0 (cosine 1,0000 đã đo).
ACC_LEVEL = {"text": 4, "image": 0}
RECIPE = "w8a4e8"   # đổi khi đổi công thức → build_dir mới, không dùng lại bản build cũ
# RAM đỉnh (đo 2026-10-20, 2 vCPU, cgroup memory.peak, model đã nạp ~1,2 GB): 1024 token 2,6 GB · 2048 → 3,5 GB ·
# 3072 → 5,0 GB · 4096 → 6,9 GB (attention đầy đủ ~ len², không flash-attn) → máy 8 GB cắt 2048. Chunk v3: p50 864,
# p99 1429; chỉ 77/16.794 (0,5%, bảng dài) > 2048 — và cũng chính 77 chunk đó > 4096, nên 2048 không mất thêm gì so
# với 4096. Cloud nhận 8192: vector của 77 chunk đó (nếu nhúng lại bằng dịch vụ này) sẽ lệch cloud.
TEXT_MAX_LEN = int(os.environ.get("EMBED_TEXT_MAX_LEN", "2048"))
TOKEN_BUDGET = int(os.environ.get("EMBED_TOKEN_BUDGET", "2048"))   # n đoạn × độ dài (đã pad) mỗi lượt → chặn RAM
CONCURRENCY = int(os.environ.get("EMBED_CONCURRENCY", "1"))        # lượt chạy đồng thời (2 vCPU: song song không nhanh hơn)
IMAGE_BATCH = int(os.environ.get("EMBED_IMAGE_BATCH", "8"))
FULL_DIM = 1024
MODEL_NAME = "jina-clip-v2"


# ---------------------------------------------------------------- tiền xử lý (thuần numpy/PIL, test được không cần model)
def preprocess_image(data: bytes, cfg: dict) -> np.ndarray:
    """bytes ảnh → mảng (3, S, S) float32 như JinaCLIPImageProcessor (resize_mode `shortest`): torchvision
    Resize(S) trên cạnh ngắn (cạnh dài = int(S·dài/ngắn)), CenterCrop(S) (lề trên/trái = round((x − S)/2)),
    ToTensor, Normalize(mean, std)."""
    from PIL import Image
    size = int(cfg.get("size", 512))
    resample = Image.BILINEAR if cfg.get("interpolation") == "bilinear" else Image.BICUBIC
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    if w <= h:
        nw, nh = size, int(size * h / w)
    else:
        nw, nh = int(size * w / h), size
    if (nw, nh) != (w, h):
        im = im.resize((nw, nh), resample)
    top, left = int(round((nh - size) / 2.0)), int(round((nw - size) / 2.0))
    im = im.crop((left, top, left + size, top + size))
    a = np.asarray(im, dtype=np.float32) / 255.0
    mean = np.asarray(cfg.get("mean", (0.48145466, 0.4578275, 0.40821073)), dtype=np.float32)
    std = np.asarray(cfg.get("std", (0.26862954, 0.26130258, 0.27577711)), dtype=np.float32)
    return ((a - mean) / std).transpose(2, 0, 1)


def token_batches(lengths: list[int], budget: int, same_length: bool) -> list[list[int]]:
    """Chỉ số chữ → các lượt chạy. Xếp theo độ dài để ít padding; mỗi lượt ≤ budget token (đã pad: n × dài nhất),
    tối thiểu 1 đoạn. same_length: graph không có attention_mask → chỉ gộp đoạn cùng số token (không pad)."""
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    out: list[list[int]] = []
    cur: list[int] = []
    for i in order:
        longest = max(lengths[i], max((lengths[j] for j in cur), default=0))
        if cur and ((len(cur) + 1) * longest > budget or (same_length and lengths[i] != lengths[cur[0]])):
            out.append(cur)
            cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def load_tokenizer(path: Path):
    """tokenizer.json của repo đặt sẵn padding Fixed 77 + truncation 77 (kiểu CLIP) → phải tắt padding và cắt ở
    TEXT_MAX_LEN, nếu không mọi chunk bị cụt còn 77 token (vector lệch hẳn cloud)."""
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(str(path))
    tok.no_padding()
    tok.enable_truncation(TEXT_MAX_LEN)
    return tok


def pick_output(names: list[str], kind: str) -> str:
    """Tên đầu ra embedding `kind` (text|image); ưu tiên bản đã chuẩn hoá `l2norm_*` (ta vẫn chuẩn hoá lại)."""
    cands = [n for n in names if kind in n.lower()]
    if not cands:
        raise RuntimeError(f"graph không có đầu ra {kind}: {names}")
    return next((n for n in cands if "norm" in n.lower()), cands[0])


def finish(vec: np.ndarray, dim: int | None) -> list[float]:
    """Cắt Matryoshka (dim < 1024) rồi chuẩn hoá L2."""
    v = np.asarray(vec, dtype=np.float32).reshape(-1)[: (dim or FULL_DIM)]
    return (v / max(float(np.linalg.norm(v)), 1e-12)).tolist()


def parse_items(body: dict) -> list[dict]:
    items = body.get("input")
    if isinstance(items, str):
        items = [{"text": items}]
    if not isinstance(items, list) or not items:
        raise ValueError("input phải là danh sách {text}|{image}")
    out = []
    for it in items:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict) or ("text" in it) == ("image" in it):
            raise ValueError("mỗi phần tử input có đúng 1 khoá text hoặc image")
        out.append(it)
    return out


# ---------------------------------------------------------------- model
def model_dir() -> Path:
    """Thư mục chứa file ONNX + tokenizer + preprocessor (EMBED_ONNX_DIR, hoặc tải từ HF vào HF_HOME)."""
    if os.environ.get("EMBED_ONNX_DIR"):
        return Path(os.environ["EMBED_ONNX_DIR"])
    from huggingface_hub import snapshot_download
    return Path(snapshot_download(REPO, revision=REVISION, allow_patterns=[
        SRC_FILE, f"{SRC_FILE}_data", "tokenizer.json", "preprocessor_config.json", "config.json"]))


def build_dir() -> Path:
    if os.environ.get("EMBED_BUILD_DIR"):
        return Path(os.environ["EMBED_BUILD_DIR"])
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    return home / "sict-embed" / f"{REVISION[:12]}-{RECIPE if QUANT == 'w8' else QUANT}"


def quantize_vocab_table(model, min_rows: int = 10000) -> int:
    """Gather trên bảng embedding fp32 (từ vựng 250k × 1024 ≈ 1 GB — phần lớn RAM của tháp chữ sau MatMulNBits) →
    int8 theo hàng + scale fp32: Gather(W_int8) → Cast → Mul(Gather(scale)). MatMulNBitsQuantizer có lượng tử Gather
    nhưng đổi opset làm graph lỗi (ReduceMean axes). Đo: RAM tháp chữ 1.567 → 874 MB, độ trễ / cosine không đổi.
    Trả số bảng đã đổi."""
    import onnx
    from onnx import helper, numpy_helper
    g = model.graph
    inits = {i.name: i for i in g.initializer}
    done = 0
    for node in list(g.node):
        if node.op_type != "Gather" or node.input[0] not in inits:
            continue
        W = numpy_helper.to_array(inits[node.input[0]])
        if W.ndim != 2 or W.shape[0] < min_rows or W.dtype != np.float32:
            continue
        scale = (np.maximum(np.abs(W).max(axis=1), 1e-12) / 127.0).astype(np.float32)
        Wq = np.clip(np.round(W / scale[:, None]), -127, 127).astype(np.int8)
        base, ids, o = node.input[0], node.input[1], node.output[0]
        g.initializer.remove(inits[base])
        g.initializer.extend([numpy_helper.from_array(Wq, base + "_q8"), numpy_helper.from_array(scale, base + "_scale"),
                              numpy_helper.from_array(np.array([-1], dtype=np.int64), base + "_ax")])
        new = [helper.make_node("Gather", [base + "_q8", ids], [o + "_q"], axis=0),
               helper.make_node("Cast", [o + "_q"], [o + "_f"], to=onnx.TensorProto.FLOAT),
               helper.make_node("Gather", [base + "_scale", ids], [o + "_s"], axis=0),
               helper.make_node("Unsqueeze", [o + "_s", base + "_ax"], [o + "_s2"]),
               helper.make_node("Mul", [o + "_f", o + "_s2"], [o])]
        at = list(g.node).index(node)
        g.node.remove(node)
        for k, n in enumerate(new):
            g.node.insert(at + k, n)
        done += 1
    return done


def build_towers(src: Path, out_dir: Path) -> dict[str, tuple[Path, str]]:
    """fp32 gộp (src = thư mục model) → {text: (file, đầu ra), image: (...)} trong out_dir; đã có thì dùng lại.

    onnx từ chối đọc dữ liệu ngoài qua symlink (cache HF) lẫn hardlink → chép thật model.onnx(+_data) vào out_dir/_src
    (3,5 GB, xoá sau khi xong). Tách graph KHÔNG nạp trọng số (tháp chữ fp32 ~2,2 GB vượt giới hạn 2 GB của protobuf;
    graph con vẫn trỏ vào model.onnx_data), rồi lượng tử hoá từng tháp (int8 ~1,4 GB + 0,35 GB, dưới 2 GB)."""
    import shutil

    import onnx
    from onnx.utils import Extractor
    meta = out_dir / "towers.json"
    if meta.exists():
        names = json.loads(meta.read_text())
        plan = {k: out_dir / f"{k}.onnx" for k in names}
        if all(p.exists() for p in plan.values()):
            return {k: (plan[k], names[k]) for k in plan}
    t0 = time.time()
    work = out_dir / "_src"
    work.mkdir(parents=True, exist_ok=True)
    for f in (SRC_FILE, f"{SRC_FILE}_data"):
        shutil.copyfile(src / f, work / Path(f).name)        # theo symlink → file thật
    m = onnx.load(str(work / "model.onnx"), load_external_data=False)
    ins = [i.name for i in m.graph.input]
    names = {"text": pick_output([o.name for o in m.graph.output], "text"),
             "image": pick_output([o.name for o in m.graph.output], "image")}
    want_in = {"text": [n for n in ins if n in ("input_ids", "attention_mask")], "image": ["pixel_values"]}
    ex = Extractor(m)
    for k in names:
        sub_path = work / f"{k}_fp32.onnx"
        onnx.save(ex.extract_model(want_in[k], [names[k]]), str(sub_path))
        sub = onnx.load(str(sub_path))                        # nạp trọng số của riêng tháp này
        tmp = out_dir / f"{k}.onnx.tmp"
        if QUANT == "fp32":
            onnx.save(sub, str(tmp), save_as_external_data=True, all_tensors_to_one_file=True,
                      location=f"{k}.onnx_data")
        else:
            from onnxruntime.quantization.matmul_nbits_quantizer import (
                DefaultWeightOnlyQuantConfig, MatMulNBitsQuantizer)
            qz = MatMulNBitsQuantizer(sub, algo_config=DefaultWeightOnlyQuantConfig(
                block_size=QUANT_BLOCK, is_symmetric=True, accuracy_level=ACC_LEVEL[k], bits=8))
            qz.process()
            qm = qz.model.model
            if k == "text":
                quantize_vocab_table(qm)
            onnx.save(qm, str(tmp))
            del qz, qm
        del sub
        tmp.replace(out_dir / f"{k}.onnx")
        print(f"[embed_server] build {k} ({QUANT}): {time.time() - t0:.0f}s", flush=True)
    del ex, m
    shutil.rmtree(work)
    meta.write_text(json.dumps(names))
    return {k: (out_dir / f"{k}.onnx", names[k]) for k in names}


class Encoder:
    """2 InferenceSession (tháp chữ / tháp ảnh — build_towers), dùng chung giữa các luồng."""

    def __init__(self, path: Path | None = None, threads: int | None = None):
        import onnxruntime as ort
        d = path or model_dir()
        out = build_dir()
        towers = build_towers(d, out)
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or int(os.environ.get("EMBED_THREADS", "0")) or (os.cpu_count() or 1)
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # độ dài chữ đổi mỗi lượt → arena giữ đỉnh cũ và mem pattern vô dụng; tắt để RAM bám theo lượt đang chạy
        so.enable_cpu_mem_arena = os.environ.get("EMBED_MEM_ARENA", "0") == "1"
        so.enable_mem_pattern = False
        self.slots = threading.Semaphore(CONCURRENCY)   # RAM đỉnh cộng dồn theo số lượt chạy cùng lúc
        self.sess = {k: ort.InferenceSession(str(f), so, providers=["CPUExecutionProvider"])
                     for k, (f, _) in towers.items()}
        self.out = {k: name for k, (_, name) in towers.items()}
        self.inputs = {k: {i.name for i in s.get_inputs()} for k, s in self.sess.items()}
        self.tok = load_tokenizer(d / "tokenizer.json")
        self.pre = json.loads((d / "preprocessor_config.json").read_text())
        # không có attention_mask → pad sẽ lọt vào mean pooling → chỉ gộp các đoạn cùng số token (token_batches)
        self.has_mask = "attention_mask" in self.inputs["text"]
        print(f"[embed_server] {REPO}@{REVISION[:8]} {QUANT} {out} inputs={self.inputs} out={self.out} "
              f"threads={so.intra_op_num_threads} arena={so.enable_cpu_mem_arena}", flush=True)

    def _run(self, kind: str, feed: dict) -> np.ndarray:
        feed = {k: v for k, v in feed.items() if k in self.inputs[kind]}
        with self.slots:
            return np.asarray(self.sess[kind].run([self.out[kind]], feed)[0], dtype=np.float32)

    def texts(self, texts: list[str]) -> list[np.ndarray]:
        enc = self.tok.encode_batch(texts)
        ids = [e.ids for e in enc]
        out: list[np.ndarray | None] = [None] * len(texts)
        for b in token_batches([len(x) for x in ids], TOKEN_BUDGET, same_length=not self.has_mask):
            n = max(len(ids[i]) for i in b)
            arr = np.ones((len(b), n), dtype=np.int64)   # pad id XLM-R = 1
            mask = np.zeros((len(b), n), dtype=np.int64)
            for r, i in enumerate(b):
                arr[r, :len(ids[i])] = ids[i]
                mask[r, :len(ids[i])] = 1
            feed = {"input_ids": arr, **({"attention_mask": mask} if self.has_mask else {})}
            for i, v in zip(b, self._run("text", feed)):
                out[i] = v
        return out  # type: ignore[return-value]

    def images(self, blobs: list[bytes]) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for i in range(0, len(blobs), IMAGE_BATCH):
            px = np.stack([preprocess_image(b, self.pre) for b in blobs[i:i + IMAGE_BATCH]])
            out += list(self._run("image", {"pixel_values": px}))
        return out


def embed(enc, items: list[dict], dim: int | None) -> tuple[list[list[float]], int, int]:
    """items (đã parse_items) → vector theo đúng thứ tự input."""
    t_idx = [k for k, it in enumerate(items) if "text" in it]
    i_idx = [k for k, it in enumerate(items) if "image" in it]
    vecs: list = [None] * len(items)
    if t_idx:
        for k, v in zip(t_idx, enc.texts([items[k]["text"] for k in t_idx])):
            vecs[k] = v
    if i_idx:
        for k, v in zip(i_idx, enc.images([base64.b64decode(items[k]["image"]) for k in i_idx])):
            vecs[k] = v
    return [finish(v, dim) for v in vecs], len(t_idx), len(i_idx)


# ---------------------------------------------------------------- HTTP
def create_app(enc=None):
    """FastAPI app; enc=None → nạp model lúc khởi động (test truyền encoder giả)."""
    from fastapi import FastAPI, HTTPException, Request

    from prometheus_client import (
        CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, disable_created_metrics, generate_latest,
    )
    disable_created_metrics()   # bỏ chuỗi *_created (hạn mức chuỗi Grafana Cloud)
    from starlette.responses import Response

    app = FastAPI(title="sict embed (jina-clip-v2 onnx int8)")
    state = {"enc": enc, "requests": 0, "items": 0, "lock": threading.Lock()}
    # D13: Alloy (VM2) scrape /metrics — độ trễ theo loại lượt (câu hỏi ngắn / chữ dài / ảnh) + số mục
    reg = CollectorRegistry()
    latency = Histogram("sict_embed_request_duration_seconds", "Thời gian 1 request /v1/embeddings", ["kind"],
                        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120), registry=reg)
    items_total = Counter("sict_embed_items_total", "Số mục đã nhúng", ["kind"], registry=reg)
    errors_total = Counter("sict_embed_errors_total", "Request lỗi (4xx/5xx)", ["code"], registry=reg)

    def get_enc():
        if state["enc"] is None:
            with state["lock"]:
                if state["enc"] is None:
                    state["enc"] = Encoder()
        return state["enc"]

    @app.on_event("startup")
    def _load() -> None:
        get_enc()

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    @app.get("/")
    @app.get("/healthz")
    def health() -> dict:
        return {"ok": state["enc"] is not None, "model": MODEL_NAME, "quant": QUANT,
                "requests": state["requests"], "items": state["items"]}

    def _embed(body: dict) -> dict:
        try:
            items = parse_items(body)
            dim = int(body.get("dimensions") or FULL_DIM)
            if not 1 <= dim <= FULL_DIM:
                raise ValueError(f"dimensions phải trong 1..{FULL_DIM}")
        except (ValueError, TypeError) as e:
            errors_total.labels("400").inc()
            raise HTTPException(400, str(e))
        t0 = time.time()
        try:
            vecs, nt, ni = embed(get_enc(), items, dim)
        except Exception as e:   # ảnh hỏng, lỗi ORT → 500 có lý do; embedder phía client thử lại / báo EmbedError
            errors_total.labels("500").inc()
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        kind = "image" if ni else ("query" if nt == 1 and len(items[0]["text"]) < 1000 else "text")
        latency.labels(kind).observe(time.time() - t0)
        items_total.labels("image").inc(ni)
        items_total.labels("text").inc(nt)
        state["requests"] += 1
        state["items"] += len(vecs)
        return {"model": MODEL_NAME, "object": "list",
                "data": [{"object": "embedding", "index": k, "embedding": v} for k, v in enumerate(vecs)],
                "usage": {"total_tokens": 0, "texts": nt, "images": ni, "seconds": round(time.time() - t0, 3)}}

    @app.post("/v1/embeddings")
    @app.post("/embeddings")
    async def embeddings(request: Request) -> dict:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_embed, await request.json())

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="jina-clip-v2 ONNX int8 — API /v1/embeddings kiểu Jina")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "build"],
                    help="build = chỉ tải + tách + lượng tử rồi thoát (chạy trước, RAM ~6 GB)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    if a.cmd == "build":
        print(json.dumps({k: str(f) for k, (f, _) in build_towers(model_dir(), build_dir()).items()}))
        return
    import uvicorn
    uvicorn.run(create_app(), host=a.host, port=a.port, workers=1)


if __name__ == "__main__":
    main()
