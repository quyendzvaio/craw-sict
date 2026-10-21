"""Server tự host jina-clip-v2 (mặc định fp32) — hiện thực phần "tự host" của D22 bằng HTTP
cùng giao diện Jina `/embeddings`, để `embedder` backend `api` dùng được mà không cần cloud.

Client gọi qua EMBED_API_BASE (không cần EMBED_API_KEY). Ví dụ trên máy có GPU:

    uv run --group ml python scripts/serve_jina_clip.py --host 0.0.0.0 --port 8000
    # máy chạy compute:
    EMBED_BACKEND=api EMBED_API_BASE=http://127.0.0.1:8000 EMBED_DIM=1024 \
        uv run --group ml python -m sict_crawler.compute --snapshot _snapshot/<date> --stages images,videos

Hợp đồng khớp `sict_crawler/embedder.py::_post` và `sict_crawler/tests/test_embedder_api.py`:
  request  {model, input:[{"text"}|{"image": <base64>}], normalized, embedding_type, dimensions}
  response {data:[{index, embedding}], usage:{total_tokens,...}}  — thứ tự theo `index`.

Cài đặt: transformers>=4.57 + einops + timm + pillow (model cần trust_remote_code) + fastapi + uvicorn.
Smoke-test BẮT BUỘC trước khi nhúng thật (so cosine với vector cloud trong cache — xem
/DATA/craw-sict-data/embed_cache): fp32 tự host phải khớp cloud ~1.0 mới được trộn chung index v3.
"""
from __future__ import annotations

import argparse
import base64
import io
import os

import numpy as np
import torch
from fastapi import FastAPI, HTTPException, Request
from PIL import Image

MODEL_ID = os.environ.get("JINA_MODEL_ID", "jinaai/jina-clip-v2")
DTYPE = os.environ.get("JINA_DTYPE", "fp32").strip().lower()
IMAGE_MAX_SIDE = 512          # jina-clip-v2 nhận 512×512: khớp tiền xử lý phía client
TEXT_MAX_LEN = int(os.environ.get("JINA_MAX_LEN", "8192"))   # BẮT BUỘC khớp cloud: encode_text mặc định chỉ 512 → chunk dài lệch

_model = None


def _load():
    """Nạp model 1 lần (device auto, fp32 mặc định). torch bản CUDA không có → chạy CPU."""
    global _model
    if _model is None:
        from transformers import AutoModel
        dtype = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}.get(DTYPE, torch.float32)
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        model = AutoModel.from_pretrained(MODEL_ID, trust_remote_code=True)
        _model = model.to(device=dev, dtype=(dtype if dev == "cuda" else torch.float32)).eval()
        print(f"[serve_jina_clip] model={MODEL_ID} device={dev} dtype={DTYPE if dev == 'cuda' else 'fp32'}", flush=True)
    return _model


def _to_np(x) -> np.ndarray:
    if torch.is_tensor(x):
        x = x.detach().cpu().float().numpy()
    return np.asarray(x, dtype=np.float32)


def _norm(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    return a / np.clip(n, 1e-12, None)


def _decode_image(b64: str) -> Image.Image:
    if "base64," in b64:                       # phòng khi client gửi kèm data URI
        b64 = b64.split("base64,", 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    if max(img.size) > IMAGE_MAX_SIDE:         # client đã resize; resize lại cho chắc
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    return img


def _estimate_tokens(inputs: list[dict]) -> int:
    """Khớp cách ước lượng của client (ảnh ≤ 512 px ≈ 4.000 token; chữ ≈ 1 token / 2,5 ký tự)."""
    return sum(4000 if "image" in it else max(1, int(len(it.get("text", "")) / 2.5)) for it in inputs)


def embed(inputs: list[dict], dim: int | None, normalized: bool) -> list[list[float]]:
    """Nhúng theo lô, giữ đúng thứ tự đầu vào. `dim` = Matryoshka (None/0 → 1024 đầy đủ)."""
    model = _load()
    trunc = dim or None
    text_part = [(i, it["text"]) for i, it in enumerate(inputs) if "text" in it]
    image_part = [(i, _decode_image(it["image"])) for i, it in enumerate(inputs) if "image" in it]
    out: list[np.ndarray | None] = [None] * len(inputs)
    with torch.no_grad():
        if text_part:
            vecs = _to_np(model.encode_text([s for _, s in text_part], truncate_dim=trunc,
                                            max_length=TEXT_MAX_LEN))
            for (i, _), v in zip(text_part, vecs):
                out[i] = v
        if image_part:
            vecs = _to_np(model.encode_image([im for _, im in image_part], truncate_dim=trunc))
            for (i, _), v in zip(image_part, vecs):
                out[i] = v
    rows = []
    for v in out:
        if v is None:
            raise ValueError("phần tử input không có 'text' hay 'image'")
        rows.append((_norm(v) if normalized else v).astype(np.float32).tolist())
    return rows


def build_app():
    app = FastAPI(title="serve_jina_clip")

    @app.get("/health")
    def health():
        model = _load()
        return {"ok": True, "model": MODEL_ID, "device": str(next(model.parameters()).device),
                "dim": 1024}

    @app.post("/embeddings")
    async def embeddings(req: Request):
        body = await req.json()
        inputs = body.get("input") or []
        if not isinstance(inputs, list) or not inputs:
            raise HTTPException(status_code=400, detail="input phải là list không rỗng")
        try:
            vecs = embed(inputs, body.get("dimensions"), bool(body.get("normalized", True)))
        except Exception as ex:                                   # noqa: BLE001 — trả lỗi rõ ràng cho client
            raise HTTPException(status_code=400, detail=f"{type(ex).__name__}: {ex}")
        tokens = _estimate_tokens(inputs)
        data = [{"index": i, "embedding": v} for i, v in enumerate(vecs)]
        return {"data": data, "usage": {"total_tokens": tokens, "prompt_tokens": tokens}}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Tự host jina-clip-v2 qua giao diện Jina /embeddings")
    ap.add_argument("--host", default=os.environ.get("JINA_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("JINA_PORT", "8000")))
    args = ap.parse_args()
    _load()                                                       # nạp sẵn: request đầu không phải chờ
    import uvicorn
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
