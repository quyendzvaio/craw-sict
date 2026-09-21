"""Embedding: E5 for text, CLIP for images/video-keyframes (base64 input).

Callers: main.py, tasks.py, api.py (query embed).
Schemas: embed_texts(list[str]) → list[list[float]] dim=384; embed_images(list[bytes]) → dim=512.
"""
from __future__ import annotations

import io

from PIL import Image

from .config import get_settings

_text_model = None
_clip_model = None
_clip_preprocess = None


def _get_text_model():
    global _text_model
    if _text_model is None:
        from sentence_transformers import SentenceTransformer
        cfg = get_settings()
        _text_model = SentenceTransformer(cfg.embedding.text_model, device=cfg.embedding.device)
    return _text_model


def _get_clip():
    global _clip_model, _clip_preprocess
    if _clip_model is None:
        from transformers import CLIPModel, CLIPProcessor
        cfg = get_settings()
        _clip_model = CLIPModel.from_pretrained(cfg.embedding.image_model).to(cfg.embedding.device).eval()
        _clip_preprocess = CLIPProcessor.from_pretrained(cfg.embedding.image_model)
    return _clip_model, _clip_preprocess


def embed_texts(texts: list[str]) -> list[list[float]]:
    """E5 multilingual text embeddings (384-dim)."""
    model = _get_text_model()
    # e5 convention: prefix "query: " for queries, "passage: " for docs
    prefixed = [f"passage: {t}" for t in texts]
    vecs = model.encode(prefixed, batch_size=get_settings().embedding.batch_size,
                        normalize_embeddings=True)
    return [v.tolist() for v in vecs]


def embed_query(text: str) -> list[float]:
    model = _get_text_model()
    vec = model.encode([f"query: {text}"], normalize_embeddings=True)[0]
    return vec.tolist()


def embed_image_bytes(images: list[bytes]) -> tuple[list[list[float]], list[int]]:
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
    inputs = processor(images=ok_imgs, return_tensors="pt").to(get_settings().embedding.device)
    with torch.no_grad():
        out = model.get_image_features(**inputs)
        feats = out.pooler_output if not torch.is_tensor(out) else out
    feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().tolist(), ok_idx


def embed_image_base64(b64_list: list[str]) -> list[list[float]]:
    import base64
    vecs, _ = embed_image_bytes([base64.b64decode(b) for b in b64_list])
    return vecs


def embed_text_clip(texts: list[str]) -> list[list[float]]:
    """CLIP text encoder (512-dim) — for cross-modal query into image collection."""
    import torch
    model, processor = _get_clip()
    inputs = processor(text=texts, return_tensors="pt", padding=True,
                        truncation=True, max_length=77).to(get_settings().embedding.device)
    with torch.no_grad():
        out = model.get_text_features(**inputs)
        feats = out.pooler_output if not torch.is_tensor(out) else out
    feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().tolist()
