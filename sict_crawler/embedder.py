"""Embedding gateway. Hai họ backend, cùng giao diện hàm (chọn bằng embedding.backend / EMBED_BACKEND):

- torch (index v2, mặc định tới khi cutover): E5 384-d cho chữ + CLIP B/32 512-d cho ảnh, hai không gian riêng.
- api (index v3, D22): MỘT model đa phương thức (jina-clip-v2, 1024-d) qua endpoint HTTP — chữ và ảnh chung
  không gian. Endpoint/key/model đặt bằng EMBED_API_BASE / EMBED_API_KEY / EMBED_MODEL, nên đổi nhà cung cấp
  hoặc đổi sang bản tự host (ONNX int8 sau cùng giao diện) chỉ là đổi cấu hình.

Callers: main.py, compute.py, indexer.py, api.py (query embed).
Giao diện: embed_texts(list[str], kind="passage"|"query") → list[vec]; embed_query(str) → vec;
embed_image_bytes(list[bytes]) → (vecs, ok_idx); embed_image_base64; embed_text_clip (=embed_query ở backend api).
"""
from __future__ import annotations

import base64
import hashlib
import io
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .config import get_settings

_text_model = None
_clip_model = None
_clip_preprocess = None
_device: str | None = None


def get_device() -> str:
    """Resolve embedding.device một lần rồi cache (không dò lại mỗi batch).

    auto → "cuda" nếu torch thấy GPU, ngược lại "cpu". Ép "cuda" mà máy không có
    GPU (hoặc torch bản CPU) → rơi về "cpu" kèm cảnh báo, không crash.
    """
    global _device
    if _device is None:
        want = get_settings().embedding.device.strip().lower() or "auto"
        has_cuda = False
        if want != "cpu":
            try:
                import torch
                has_cuda = bool(torch.cuda.is_available())
            except Exception:
                has_cuda = False
        if want == "auto":
            _device = "cuda" if has_cuda else "cpu"
        elif want.startswith("cuda") and not has_cuda:
            print(f"[embedder] device={want} nhưng không có GPU → dùng cpu")
            _device = "cpu"
        else:
            _device = want
        print(f"[embedder] device={_device}")
    return _device


def _get_text_model():
    global _text_model
    if _text_model is None:
        from sentence_transformers import SentenceTransformer
        cfg = get_settings()
        _text_model = SentenceTransformer(cfg.embedding.text_model, device=get_device())
    return _text_model


def _get_clip():
    global _clip_model, _clip_preprocess
    if _clip_model is None:
        from transformers import CLIPModel, CLIPProcessor
        cfg = get_settings()
        _clip_model = CLIPModel.from_pretrained(cfg.embedding.image_model).to(get_device()).eval()
        _clip_preprocess = CLIPProcessor.from_pretrained(cfg.embedding.image_model)
    return _clip_model, _clip_preprocess


def _torch_embed_texts(texts: list[str], kind: str = "passage") -> list[list[float]]:
    """E5 multilingual text embeddings (384-dim)."""
    model = _get_text_model()
    # e5 convention: prefix "query: " for queries, "passage: " for docs
    prefixed = [f"{kind}: {t}" for t in texts]
    vecs = model.encode(prefixed, batch_size=get_settings().embedding.batch_size,
                        normalize_embeddings=True)
    return [v.tolist() for v in vecs]


def _torch_embed_image_bytes(images: list[bytes]) -> tuple[list[list[float]], list[int]]:
    """CLIP embeddings from raw bytes. Skips undecodable images.
    Returns (vectors, indices_into_input) — vectors[i] corresponds to images[indices[i]]."""
    import torch
    model, processor = _get_clip()
    ok_imgs, ok_idx = [], []
    for i, b in enumerate(images):
        try:
            img = Image.open(io.BytesIO(b))
            img.verify()  # reject corrupt/svg/html masquerading as jpg
            ok_imgs.append(Image.open(io.BytesIO(b)).convert("RGB"))
            ok_idx.append(i)
        except Exception:
            continue
    if not ok_imgs:
        return [], []
    inputs = processor(images=ok_imgs, return_tensors="pt").to(get_device())
    with torch.no_grad():
        out = model.get_image_features(**inputs)
        feats = out.pooler_output if not torch.is_tensor(out) else out
    feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().tolist(), ok_idx


def _torch_embed_text_clip(texts: list[str]) -> list[list[float]]:
    """CLIP text encoder (512-dim) — for cross-modal query into image collection."""
    import torch
    model, processor = _get_clip()
    inputs = processor(text=texts, return_tensors="pt", padding=True,
                        truncation=True, max_length=77).to(get_device())
    with torch.no_grad():
        out = model.get_text_features(**inputs)
        feats = out.pooler_output if not torch.is_tensor(out) else out
    feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().tolist()


# ---------------------------------------------------------------- backend api (jina-clip-v2, index v3)

class EmbedError(RuntimeError):
    """Gọi endpoint embedding thất bại sau khi thử lại. Lỗi cứng: thiếu vector là sai dữ liệu, không bỏ qua."""


_usage_lock = threading.Lock()
USAGE = {"requests": 0, "tokens": 0, "image_tokens": 0, "cache_hits": 0}   # pilot đọc để ước tính chi phí
_cache_db: sqlite3.Connection | None = None
_cache_lock = threading.Lock()
IMAGE_MAX_SIDE = 512        # jina-clip-v2 nhận 512×512: resize trước khi gửi (ít token, khớp tiền xử lý local)


def backend() -> str:
    return get_settings().embedding.backend


def _cache():
    """Cache vector trên đĩa (sqlite) theo hash đầu vào: chạy lại / nhúng lại cùng nội dung không tốn thêm lượt gọi."""
    global _cache_db
    path = get_settings().embedding.cache_path
    if not path:
        return None
    with _cache_lock:
        if _cache_db is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            _cache_db = sqlite3.connect(path, check_same_thread=False)
            _cache_db.execute("PRAGMA journal_mode=WAL")
            _cache_db.execute("PRAGMA synchronous=NORMAL")   # WAL: bỏ fsync mỗi lượt ghi, vẫn an toàn khi crash
            _cache_db.execute("CREATE TABLE IF NOT EXISTS vec (k TEXT PRIMARY KEY, v BLOB)")
    return _cache_db


def _key(kind: str, payload: bytes) -> str:
    e = get_settings().embedding
    return hashlib.sha256(f"{e.model}|{e.dim}|{kind}|".encode() + payload).hexdigest()


def _cache_get(keys: list[str]) -> dict[str, list[float]]:
    db = _cache()
    if db is None or not keys:
        return {}
    out: dict[str, list[float]] = {}
    with _cache_lock:
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            q = "SELECT k, v FROM vec WHERE k IN (%s)" % ",".join("?" * len(part))
            for k, v in db.execute(q, part):
                out[k] = np.frombuffer(v, dtype=np.float32).tolist()
    return out


def _cache_put(items: dict[str, list[float]]) -> None:
    db = _cache()
    if db is None or not items:
        return
    with _cache_lock:
        db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)",
                       [(k, np.asarray(v, dtype=np.float32).tobytes()) for k, v in items.items()])
        db.commit()


_RETRY_STATUS = (408, 429, 500, 502, 503, 504)
_sent: list[tuple[float, int]] = []    # (thời điểm, token) các lượt gần đây — giới hạn token/phút phía client
_sent_lock = threading.Lock()


def _estimate_tokens(items: list[dict]) -> int:
    """Ước lượng token 1 lượt gửi (đo trên Jina jina-clip-v2: ảnh ≤ 512 px = 4.000; chữ ≈ 1 token / 2,5 ký tự)."""
    return sum(4000 if "image" in it else max(1, int(len(it.get("text", "")) / 2.5)) for it in items)


def _throttle(tokens: int) -> None:
    """Chờ tới khi gửi thêm `tokens` không vượt embedding.api_tpm trong 60 s gần nhất (0 = không giới hạn).
    Gói miễn phí Jina: 100k token/phút — vượt là 429 hàng loạt."""
    tpm = get_settings().embedding.api_tpm
    if not tpm:
        return
    while True:
        with _sent_lock:
            now = time.time()
            _sent[:] = [(t, n) for t, n in _sent if now - t < 60]
            used = sum(n for _, n in _sent)
            if used + tokens <= tpm or not _sent:
                _sent.append((now, tokens))
                return
            wait = 60 - (now - _sent[0][0]) + 0.5
        time.sleep(max(0.5, wait))


def _post(items: list[dict]) -> list[list[float]]:
    """POST {api_base}/embeddings (định dạng Jina: input là list {text}|{image}). Thử lại 429/5xx theo retry-after."""
    import httpx
    e = get_settings().embedding
    if not e.api_base:
        raise EmbedError("thiếu EMBED_API_BASE (embedding.backend=api)")
    headers = {"Authorization": f"Bearer {e.api_key}"} if e.api_key else {}
    body = {"model": e.model, "input": items, "normalized": True, "embedding_type": "float"}
    if e.dim:
        body["dimensions"] = e.dim
    last = ""
    _throttle(_estimate_tokens(items))
    for attempt in range(e.api_retries):
        wait = 0.0
        try:
            r = httpx.post(f"{e.api_base.rstrip('/')}/embeddings", json=body, headers=headers, timeout=e.timeout)
        except httpx.HTTPError as ex:
            last = f"{type(ex).__name__}: {ex}"
        else:
            if r.status_code == 200:
                d = r.json()
                u = d.get("usage") or {}
                with _usage_lock:
                    USAGE["requests"] += 1
                    USAGE["tokens"] += int(u.get("total_tokens") or 0)
                    USAGE["image_tokens"] += int((u.get("prompt_tokens_details") or {}).get("image_tokens")
                                                 or u.get("image_tokens") or 0)
                rows = sorted(d["data"], key=lambda x: x["index"])
                if len(rows) != len(items):
                    raise EmbedError(f"endpoint trả {len(rows)} vector cho {len(items)} đầu vào")
                return [x["embedding"] for x in rows]
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in _RETRY_STATUS:
                raise EmbedError(last)
            try:
                wait = float(r.headers.get("retry-after", ""))
            except ValueError:
                wait = 60.0 if r.status_code == 429 else 0.0   # giới hạn theo phút: chờ hết cửa sổ
        if attempt < e.api_retries - 1:
            time.sleep(min(max(wait, 2.0 ** attempt), 90))
    raise EmbedError(f"embedding endpoint lỗi sau {e.api_retries} lần: {last}")


def _embed_cached(kind: str, keys: list[bytes], make, batch: int) -> list[list[float] | None]:
    """Vector theo thứ tự `keys` (khoá nội dung). Chỉ mục chưa có trong cache mới gọi `make(i)` để dựng mục
    gửi đi (ảnh: giải mã + resize + base64 — không tốn cho mục đã cache) rồi gọi endpoint. `make` trả None =
    đầu vào không dùng được → vector None. Cùng nội dung trong một lượt chỉ gửi một lần.
    Các lượt gọi chạy SONG SONG (embedding.api_workers luồng): đo thực tế 1 luồng chỉ ~25 ảnh/phút vì mỗi lượt
    chờ mạng ~20 s; giới hạn token/phút vẫn do _throttle giữ chung cho mọi luồng."""
    from concurrent.futures import ThreadPoolExecutor
    ck = [_key(kind, k) for k in keys]
    have = _cache_get(ck)
    with _usage_lock:
        USAGE["cache_hits"] += sum(k in have for k in ck)
    first: dict[str, int] = {}
    for i, k in enumerate(ck):
        if k not in have:
            first.setdefault(k, i)
    todo = list(first.items())            # [(khoá cache, chỉ số đầu vào)]
    parts = [todo[s:s + batch] for s in range(0, len(todo), batch)]

    def run(part: list[tuple[str, int]]) -> dict[str, list[float]]:
        built = [(k, it) for k, i in part if (it := make(i)) is not None]   # dựng mục gửi đi ngay trong luồng
        if not built:
            return {}
        new = {k: v for (k, _), v in zip(built, _post([it for _, it in built]))}
        _cache_put(new)                   # ghi cache từng lượt: dừng giữa chừng không mất phần đã nhúng
        return new

    workers = max(1, min(get_settings().embedding.api_workers, len(parts) or 1))
    if workers == 1:
        results = [run(p) for p in parts]
    else:
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(run, parts))
    for new in results:
        have.update(new)
    return [have.get(k) for k in ck]


def prepare_image(data: bytes) -> bytes | None:
    """Giải mã + kiểm tra ảnh (loại ảnh hỏng/svg/html giả jpg), đưa về RGB, cạnh dài ≤ 512, JPEG. Lỗi → None."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")   # convert() nạp hết điểm ảnh → ảnh hỏng lỗi ở đây
    except Exception:
        return None
    if max(img.size) > IMAGE_MAX_SIDE:
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _api_embed_texts(texts: list[str], kind: str) -> list[list[float]]:
    e = get_settings().embedding
    pre = e.query_prefix if kind == "query" else ""
    return _embed_cached("t", [(pre + t).encode() for t in texts], lambda i: {"text": pre + texts[i]}, e.api_batch)


def _api_embed_image_bytes(images: list[bytes]) -> tuple[list[list[float]], list[int]]:
    """Khoá cache = sha256 byte gốc, nên ảnh đã có vector không phải giải mã/resize lại."""
    def make(i: int) -> dict | None:
        jpg = prepare_image(images[i])
        return {"image": base64.b64encode(jpg).decode("ascii")} if jpg else None

    vecs = _embed_cached(f"i{IMAGE_MAX_SIDE}", [hashlib.sha256(b).digest() for b in images], make,
                         get_settings().embedding.api_image_batch)
    ok = [i for i, v in enumerate(vecs) if v is not None]
    return [vecs[i] for i in ok], ok


# ---------------------------------------------------------------- giao diện chung

def embed_texts(texts: list[str], kind: str = "passage") -> list[list[float]]:
    """Vector chữ. kind="passage" (nội dung index) hoặc "query" (câu hỏi). Backend api: tiền tố query do cấu hình."""
    if not texts:
        return []
    return _api_embed_texts(texts, kind) if backend() == "api" else _torch_embed_texts(texts, kind)


def embed_query(text: str) -> list[float]:
    return embed_texts([text], "query")[0]


def embed_image_bytes(images: list[bytes]) -> tuple[list[list[float]], list[int]]:
    """Vector ảnh từ bytes. Bỏ qua ảnh không giải mã được. Trả (vectors, indices_into_input)."""
    return _api_embed_image_bytes(images) if backend() == "api" else _torch_embed_image_bytes(images)


def embed_image_base64(b64_list: list[str]) -> list[list[float]]:
    vecs, _ = embed_image_bytes([base64.b64decode(b) for b in b64_list])
    return vecs


def embed_text_clip(texts: list[str]) -> list[list[float]]:
    """Chữ → không gian ảnh. Backend api: chung không gian nên chính là vector query; torch: CLIP text 512-d."""
    if backend() == "api":
        return _api_embed_texts(texts, "query")
    return _torch_embed_text_clip(texts)
