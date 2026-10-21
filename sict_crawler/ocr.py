"""OCR ảnh cục bộ (tesseract vie+eng) — chữ trong ảnh/khung video → caption + graph.

Callers: indexer (incremental), compute (bulk), annotate_ocr (v1).
Schemas: ocr_text(image: bytes) → str ('' nếu không có chữ đáng tin / thiếu tesseract).

"OCR dày" (2026-10-18): trước đây chạy thẳng trên ảnh gốc và giữ mọi thứ tesseract trả → 24% ảnh có chữ ra
toàn rác ("T-HaUL { sẽ \\ mu Š …") đi vào caption / BM25 / graph. Nay:
  1. tiền xử lý: ảnh xám + tự cân tương phản; ảnh nhỏ (cạnh ngắn < 1.000 px) phóng ×≤2; ảnh > 4 MP thu nhỏ
     (giới hạn RAM: RSS tesseract tăng theo số điểm ảnh, ~70 MB với ảnh 2,8 MP);
  2. lọc theo độ tin cậy từng từ (image_to_data): giữ từ conf ≥ WORD_CONF; bỏ dòng mà mảnh 1–2 ký tự lạ chiếm đa
     số; kết quả < MIN_CHARS ký tự chữ cái → '' (ảnh chụp không có chữ);
  3. chữ quay ngang/ngược (poster dọc): chỉ khi lượt đầu gần như rỗng mà ảnh vẫn có nhiều nét — hỏi OSD hướng
     rồi xoay và đọc lại.
RAM không đổi đáng kể (đo: 68–75 MB / tiến trình tesseract); thời gian ~0,5–0,6 s / ảnh, + ~0,3 s khi phải xoay.
"""
from __future__ import annotations

import io
import re

OCR_VERSION = 2          # đổi khi đổi luật → cache OCR của compute (ocr_cache.jsonl) không dùng lại kết quả cũ
WORD_CONF = 60           # độ tin cậy tối thiểu của 1 từ (0–100)
MIN_CHARS = 8            # tổng ký tự chữ cái tối thiểu để coi ảnh có chữ
MAX_PIXELS = 4_000_000
UPSCALE_SHORT_SIDE = 1000
_SHORT_OK = re.compile(r"^(?:\d+[\d.,/:%-]*|[A-ZĐ]{2}|[a-zđ]{2}|[&+\-–])$")   # mảnh ngắn hợp lệ: số, "TS", "và"…
_VI_SHORT = {"và", "là", "có", "ở", "đi", "về", "từ", "do", "vì", "đã", "sẽ", "hè", "lý", "sư", "kỹ", "số", "giờ",
             "tổ", "an", "ai", "it", "vn", "cả", "ra", "bộ", "nữ", "ủy", "uỷ", "lễ", "tư", "cơ", "hệ", "lớp"}


def prepare(img):
    """PIL image → ảnh xám đã cân tương phản, kích thước hợp lý cho tesseract."""
    from PIL import Image, ImageOps
    g = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    w, h = g.size
    if w * h > MAX_PIXELS:
        s = (MAX_PIXELS / (w * h)) ** 0.5
        g = g.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    elif min(w, h) < UPSCALE_SHORT_SIDE:
        s = min(2.0, UPSCALE_SHORT_SIDE / max(1, min(w, h)))
        g = g.resize((int(w * s), int(h * s)), Image.LANCZOS)
    return g


def _good_short(w: str) -> bool:
    return w.lower() in _VI_SHORT or bool(_SHORT_OK.match(w))


def clean_words(data: dict) -> str:
    """Kết quả image_to_data → chữ đáng tin, giữ ranh giới dòng (' | ')."""
    lines: dict[tuple, list[str]] = {}
    for i, w in enumerate(data.get("text", [])):
        w = (w or "").strip()
        try:
            conf = float(data["conf"][i])
        except (KeyError, ValueError, TypeError):
            conf = -1
        if not w or conf < WORD_CONF:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(w)
    out = []
    for words in lines.values():
        alnum = [w for w in words if any(c.isalnum() for c in w)]
        if not alnum:
            continue
        bad = sum(1 for w in alnum if len(w) <= 2 and not _good_short(w))
        if bad / len(alnum) > 0.5:
            continue  # dòng toàn mảnh vụn (nhiễu nền ảnh chụp)
        out.append(" ".join(words))
    text = " | ".join(out)
    return text if sum(c.isalpha() for c in text) >= MIN_CHARS else ""


def _read(img) -> tuple[str, int]:
    import pytesseract
    d = pytesseract.image_to_data(img, lang="vie+eng", output_type=pytesseract.Output.DICT)
    raw_alpha = sum(c.isalpha() for c in "".join(d.get("text", [])))
    return clean_words(d), raw_alpha


def _rotate_by_osd(img):
    """Hướng chữ theo OSD (0/90/180/270); None nếu không chắc / không xác định được."""
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
    except Exception:
        return None
    rot = int(osd.get("rotate", 0) or 0)
    if rot == 0 or float(osd.get("orientation_conf", 0) or 0) < 2.0:
        return None
    return img.rotate(-rot, expand=True)


def ocr_text(image: bytes, max_len: int = 300) -> str:
    """Chữ đáng tin trong ảnh → 1 chuỗi gọn (các dòng nối bằng ' | '). '' khi lỗi / không có chữ."""
    try:
        from PIL import Image
        img = prepare(Image.open(io.BytesIO(image)))
        text, raw_alpha = _read(img)
        if not text and raw_alpha >= 40:   # có nhiều nét nhưng không đọc được → có thể chữ xoay
            rot = _rotate_by_osd(img)
            if rot is not None:
                text, _ = _read(rot)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:max_len]


if __name__ == "__main__":  # self-check: vẽ chữ, đọc lại
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 80), "white")
    ImageDraw.Draw(im).text((10, 30), "TRAN VAN ANH", fill="black")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = ocr_text(buf.getvalue())
    assert "TRAN" in out.upper(), repr(out)
    print("OK — ocr_text:", out)
