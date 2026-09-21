"""Local OCR captioning (tesseract) — thay VLM caption_images khi vlm.enabled=False.

Callers: annotate_ocr.py (annotate_ocr_batch).
Schemas: ocr_text(image: bytes) → str ('' nếu không có chữ / thiếu tesseract).
"""
from __future__ import annotations

import io
import re


def ocr_text(image: bytes, max_len: int = 300) -> str:
    """Chữ trong ảnh → 1 dòng gọn. '' on any failure (missing binary, no text)."""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(image)).convert("RGB")
        txt = pytesseract.image_to_string(img, lang="vie+eng")
    except Exception:
        return ""
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len]


if __name__ == "__main__":  # self-check: vẽ chữ, đọc lại
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 80), "white")
    ImageDraw.Draw(im).text((10, 30), "TRAN VAN ANH", fill="black")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = ocr_text(buf.getvalue())
    assert "TRAN" in out.upper(), repr(out)
    print("OK — ocr_text:", out)
