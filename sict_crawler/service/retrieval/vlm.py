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

from sict_crawler.core.config import get_settings

_MAX_PX = 1024  # resize cap for API payloads


def _parse_completion(text: str) -> str:
    """Extract message content from a chat completion response body.

    The proxy returns either a JSON object or SSE chunks (`data: {...}` lines,
    sometimes with a trailing non-JSON `data: [DONE]`) — normalize both to
    plain text. '' on unparseable bodies."""
    text = text.strip()
    if not text:
        return ""
    if text.startswith("{"):
        # JSON thường — gateway có thể nối thêm "\ndata: [DONE]" ở cuối: raw_decode bỏ qua phần đuôi
        try:
            obj = json.JSONDecoder().raw_decode(text)[0]
            return (obj["choices"][0]["message"].get("content") or "").strip()
        except Exception:
            pass
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


# Proxy 9router: tắt "token saver" (caveman/ponytail chèn ~750 token system prompt và đổi giọng
# trả lời, headroom nén ngữ cảnh) cho request của dự án. Endpoint khác bỏ qua header lạ.
NO_TOKEN_SAVER = {"x-9router-token-saver": "off"}


def _client(cfg=None) -> httpx.Client:
    cfg = cfg or get_settings().vlm
    return httpx.Client(
        base_url=cfg.api_base,
        headers={"Authorization": f"Bearer {cfg.api_key}", **NO_TOKEN_SAVER},
        timeout=float(getattr(cfg, "timeout", 60)),
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
        "stream": False,
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
        # ghi rõ: proxy kiểu 9router coi thiếu "stream" là stream=true
        "stream": False,
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


def parse_route_json(raw: str) -> dict | None:
    """Đọc JSON {rewritten, media, wants_file} từ output LLM (chịu được text thừa quanh JSON).
    Không parse được → None (caller dùng output như câu viết lại thuần + luật router)."""
    import json
    if not raw or "{" not in raw:
        return None
    try:
        obj = json.JSONDecoder().raw_decode(raw[raw.find("{"):])[0]
    except ValueError:
        return None
    if not isinstance(obj, dict) or not str(obj.get("rewritten", "")).strip():
        return None
    media = str(obj.get("media", "")).strip().lower()
    return {"rewritten": str(obj["rewritten"]).strip()[:500],
            "media": media if media in ("none", "optional", "required") else None,
            "wants_file": bool(obj.get("wants_file"))}


def rewrite_and_route(question: str) -> dict | None:
    """1 lượt LLM: viết lại câu hỏi + ý định thành phần câu trả lời (router D17).

    Trả {rewritten, media|None, wants_file} hoặc None (không key/lỗi). LLM trả text thường thay vì
    JSON → coi như câu viết lại, media=None (router dùng luật từ khoá).
    """
    if not get_settings().llm.api_key:
        return None
    prompt = (
        "Bạn là bộ định tuyến truy vấn cho chatbot website Trường Công nghệ thông tin và Truyền "
        "thông (SICT, Đại học Công nghiệp Hà Nội). Với câu hỏi dưới đây, trả về DUY NHẤT một JSON:\n"
        '{"rewritten": "<câu tìm kiếm: đúng chính tả, mở rộng viết tắt (CNTT → công nghệ thông tin, '
        'ATTT → an toàn thông tin), giữ nguyên tên riêng, tên người>", '
        '"media": "none|optional|required", "wants_file": true|false}\n'
        "Quy tắc media: required = người dùng muốn XEM ảnh/video; optional = hỏi về sự kiện, "
        "hoạt động mà ảnh có thể minh hoạ; none = hỏi thông tin, quy định, số liệu, thời hạn, "
        "liên hệ, con người (không cần ảnh). wants_file = muốn tải/xem văn bản, biểu mẫu, quy chế, "
        "quy định, kế hoạch, lịch.\n\n"
        f"Câu hỏi: {question}"
    )
    out = _text_call(get_settings().llm.model, prompt)
    if not out.strip():
        return None
    parsed = parse_route_json(out)
    if parsed:
        return parsed
    return {"rewritten": out.strip()[:500], "media": None, "wants_file": False}


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


def stream_answer(question: str, context: str, image_b64: str | None = None, max_tokens: int | None = None):
    """Yield answer chunks from cfg.llm given retrieved context. SSE-style text.
    Yields nothing if no key/call fails (caller falls back to echo context).
    image_b64: ảnh người dùng gửi (D21) — chỉ đưa vào lượt gọi khi llm.answer_with_image bật; mặc định
    LLM chỉ thấy chữ đã truy xuất."""
    cfg = get_settings().llm
    if not cfg.api_key:
        return
    prompt = (
        "Bạn là trợ lý ảo của Trường CNTT & Truyền thông, Đại học Công nghiệp Hà Nội (HaUI). "
        "Trả lời câu hỏi của người dùng CHỈ dựa vào ngữ cảnh được cung cấp. "
        "Trả lời ngắn gọn bằng tiếng Việt. Ngữ cảnh chỉ có một phần thông tin thì trả lời phần có (kèm [n]) rồi nói rõ "
        "phần còn thiếu; chỉ nói không có thông tin khi không nguồn nào liên quan đến câu hỏi. "
        "Ngữ cảnh gồm các nguồn đánh số [1], [2]…; MỌI ý trả lời phải kết thúc bằng số nguồn dạng [n] "
        "(nguồn là file thì kèm số trang có trong nhãn), chỉ dùng số nguồn có trong ngữ cảnh. "
        "Khi ngữ cảnh có nguồn ảnh/video, hãy nhắc người dùng xem mục Ảnh & Video. "
        "Câu hỏi yêu cầu liệt kê thì kể đủ mọi mục có trong ngữ cảnh (mỗi mục kèm [n]); ngữ cảnh có thể chưa đủ "
        "danh sách thì nói rõ.\n\n"
        # D27: ngữ cảnh đã nằm trong ngân sách (sources.build_context: llm.source_chars / context_chars)
        f"Ngữ cảnh:\n{context}\n\n"
        f"Câu hỏi: {question}"
    )
    body = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": (
            [{"type": "text", "text": prompt},
             {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_prep(base64.b64decode(image_b64))}"}}]
            if image_b64 and cfg.answer_with_image else prompt)}],
        "max_tokens": max_tokens or cfg.max_new_tokens,
        "temperature": 0.2,
        "stream": True,
    }
    wrote = thought = 0
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
                        thought += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
                        if delta.get("content"):
                            wrote += 1
                            yield delta["content"]
        if thought and not wrote:
            # model suy luận dùng hết max_tokens cho phần suy nghĩ → caller echo ngữ cảnh
            print(f"[llm] stream_answer: chỉ có {thought} ký tự suy luận, không có câu trả lời — tăng llm.max_new_tokens "
                  f"(hiện {body['max_tokens']})")
    except Exception as e:
        print(f"[llm] stream_answer fail: {e}")
        return
