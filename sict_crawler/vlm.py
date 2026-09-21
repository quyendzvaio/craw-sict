"""OpenAI-compatible VLM wrapper for image captioning + person identification.

Callers: api.py (chat answer), query_rewriter.py (rewrite).
Schemas: caption_images(list[bytes]) → list[str]; read_name(bytes, candidates) → str;
         infer_person(bytes, page_title, page_text) → str.
"""
from __future__ import annotations

import base64
import io
import json

import httpx
from PIL import Image

from .config import get_settings

_MAX_PX = 1024  # resize cap for API payloads


def _parse_completion(text: str) -> str:
    """Extract message content from a chat completion response body.

    The proxy returns either a JSON object or SSE chunks (`data: {...}` lines,
    sometimes with a trailing non-JSON `data: [DONE]`) — normalize both to
    plain text. '' on unparseable bodies."""
    text = text.strip()
    if not text:
        return ""
    if text.startswith("data:") or "\ndata:" in text:
        parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            for ch in chunk.get("choices", []):
                delta = ch.get("delta") or ch.get("message") or {}
                c = delta.get("content")
                if c:
                    parts.append(c)
        return "".join(parts).strip()
    try:
        obj = json.JSONDecoder().raw_decode(text)[0]
        return obj["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


def _client(cfg=None) -> httpx.Client:
    cfg = cfg or get_settings().vlm
    return httpx.Client(
        base_url=cfg.api_base,
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=60.0,
    )


def _prep(image: bytes) -> str:
    """Resize + base64 encode JPEG for API payload."""
    img = Image.open(io.BytesIO(image)).convert("RGB")
    img.thumbnail((_MAX_PX, _MAX_PX))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _call(model: str, image_b64: str, prompt: str) -> str:
    """Single chat completion. Returns '' on any failure. 429/503 → sleep+retry."""
    import re
    import time

    cfg = get_settings().vlm
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}
        ],
        "max_tokens": cfg.max_new_tokens,
        "temperature": 0.0,
    }
    for attempt in range(4):
        try:
            with _client() as c:
                r = c.post("/chat/completions", json=body)
            if r.status_code == 200:
                time.sleep(1.0)  # fallback 1.0: 0.8 caused timeouts
                return _parse_completion(r.text)
            # 429 / 503 wrapping 429 → respect reset-after; 530 tunnel transient
            is_rate = r.status_code in (429, 503) or "Too Many Requests" in r.text
            is_transient = r.status_code == 530 or "Origin DNS" in r.text
            print(f"[vlm] {model} → {r.status_code}: {r.text[:200]}")
            if is_rate:
                # handle "1m 4s" or "2m 8s" formats
                m = re.search(r"reset after (?:(\d+)m )?(\d+)s", r.text)
                if m:
                    mins = int(m.group(1) or 0)
                    secs = int(m.group(2))
                    wait = mins * 60 + secs
                else:
                    wait = 5
                wait = wait + 5 + attempt * 2  # full reset + grace
                print(f"[vlm] rate-limited, sleep {wait}s (attempt {attempt+1}/4)")
                time.sleep(wait)
                continue
            if is_transient:
                wait = 5 + attempt * 3
                print(f"[vlm] transient 530, sleep {wait}s (attempt {attempt+1}/4)")
                time.sleep(wait)
                continue
            return ""
        except Exception as e:
            print(f"[vlm] {model} call fail: {e}")
            time.sleep(2 + attempt * 2)
            continue
    return ""


def _ask_with_rotation(image: bytes, prompt: str) -> str:
    """Try each model in cfg.vlm.models in order, return first non-empty answer.

    vlm.enabled=False → '' ngay: chặn mọi call VLM từ pipeline annotate
    (read_name / infer_person / caption_images) tại một điểm duy nhất."""
    cfg = get_settings().vlm
    if not cfg.enabled:
        return ""
    b64 = _prep(image)
    for m in cfg.models:
        text = _call(m, b64, prompt)
        if text:
            return text
    return ""


def caption_images(images: list[bytes]) -> list[str]:
    """1-2 câu tiếng Việt; nêu tên người/bảng tên nếu có."""
    prompt = ("Mô tả ngắn gọn ảnh này bằng 1-2 câu tiếng Việt. "
              "Nếu trong ảnh có tên người (bảng tên, giấy khen, chú thích), nêu rõ tên đó.")
    return [_ask_with_rotation(b, prompt) for b in images]


def read_name(image: bytes, candidates: list[str]) -> str:
    """Đọc bảng tên/giấy khen; trả tên trong candidates nếu khớp, '' nếu không."""
    if not candidates:
        return ""
    names = ", ".join(candidates[:50])
    prompt = (f"Trong ảnh có thể có tên người in trên bảng tên, giấy khen hoặc chữ viết. "
              f"Danh sách tên cần kiểm tra: {names}. "
              f"Tên nào xuất hiện trong ảnh? Chỉ trả về đúng một tên trong danh sách, "
              f"hoặc trả về KHÔNG nếu không tên nào có.")
    out = _ask_with_rotation(image, prompt).strip().strip(".")
    if not out or out.upper().startswith("KHÔNG"):
        return ""
    for c in candidates:
        if c.lower() in out.lower() or out.lower() in c.lower():
            return c
    return ""


def infer_person(image: bytes, page_title: str, page_text: str) -> str:
    """Suy luận nhân vật từ nội dung ảnh + context page. '' nếu không suy ra được."""
    prompt = (f"Ảnh này nằm trên trang web có tiêu đề \"{page_title}\" "
              f"và nội dung: \"{page_text[:500]}\". "
              f"Dựa vào nội dung ảnh và trang, ai là nhân vật chính trong ảnh? "
              f"Trả về tên người (nếu có) hoặc MỞ RỘNG nếu không xác định được.")
    out = _ask_with_rotation(image, prompt)
    if out.upper().startswith("MỞ RỘNG"):
        return ""
    return out[:200]


def _text_call(model: str, prompt: str) -> str:
    """Text-only chat completion against cfg.llm endpoint. '' on any failure."""
    cfg = get_settings().llm
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.max_new_tokens,
        "temperature": 0.0,
    }
    try:
        with _client(get_settings().llm) as c:
            r = c.post("/chat/completions", json=body)
        if r.status_code != 200:
            print(f"[llm] {model} → {r.status_code}: {r.text[:120]}")
            return ""
        # proxy returns JSON object or SSE chunks — normalize both
        return _parse_completion(r.text)
    except Exception as e:
        print(f"[llm] {model} text call fail: {e}")
        return ""


def rewrite_query(question: str) -> str:
    """LLM (cfg.llm, model 'sict') rewrite: expand viết tắt, đúng chính tả.
    Trả câu đã rewrite; '' nếu fail (caller fallback rule-based)."""
    if not get_settings().llm.api_key:
        return ""
    prompt = (
        "Bạn là bộ truy vấn tìm kiếm cho website Trường CNTT & Truyền thông (HaUI). "
        "Viết lại câu hỏi sau thành một câu tìm kiếm hiệu quả: viết đúng chính tả, "
        "mở rộng từ viết tắt (VD: CNTT → công nghệ thông tin, ATTT → an toàn thông tin), "
        "giữ nguyên tên riêng và tên người. Chỉ trả về câu đã viết lại, không giải thích.\n\n"
        f"Câu hỏi: {question}"
    )
    out = _text_call(get_settings().llm.model, prompt)
    return out[:500]


def stream_answer(question: str, context: str, max_tokens: int | None = None):
    """Yield answer chunks from cfg.llm given retrieved context. SSE-style text.
    Yields nothing if no key/call fails (caller falls back to echo context)."""
    cfg = get_settings().llm
    if not cfg.api_key:
        return
    prompt = (
        "Bạn là trợ lý ảo của Trường CNTT & Truyền thông, Đại học Công nghiệp Hà Nội (HaUI). "
        "Trả lời câu hỏi của người dùng CHỈ dựa vào ngữ cảnh được cung cấp. "
        "Trả lời ngắn gọn bằng tiếng Việt. Nếu ngữ cảnh không đủ để trả lời, nói rõ điều đó. "
        "Khi ngữ cảnh có nguồn ảnh/video, hãy nhắc người dùng xem mục Ảnh & Video.\n\n"
        f"Ngữ cảnh:\n{context[:6000]}\n\n"
        f"Câu hỏi: {question}"
    )
    body = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens or cfg.max_new_tokens,
        "temperature": 0.2,
        "stream": True,
    }
    try:
        with _client(cfg) as c:
            with c.stream("POST", "/chat/completions", json=body) as r:
                for line in r.iter_lines():
                    line = (line or "").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue
                    for ch in chunk.get("choices", []):
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            yield delta["content"]
    except Exception as e:
        print(f"[llm] stream_answer fail: {e}")
        return
