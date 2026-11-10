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


def _text_call(model: str, prompt: str, timeout: float | None = None, *, max_tokens: int | None = None) -> str:
    """Text-only chat completion against cfg.llm endpoint. '' on any failure."""
    cfg = get_settings().llm
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.max_new_tokens if max_tokens is None else max_tokens,
        "temperature": 0.0,
        # ghi rõ: proxy kiểu 9router coi thiếu "stream" là stream=true
        "stream": False,
    }
    try:
        if timeout is None:
            with _client(cfg) as c:
                r = c.post("/chat/completions", json=body)
        else:
            # HTTPX read timeout chỉ chặn khoảng im lặng; wait_for giới hạn toàn lượt,
            # kể cả server gửi từng mẩu liên tục, và hủy kết nối khi hết hạn.
            import asyncio
            async def request():
                async with httpx.AsyncClient(base_url=cfg.api_base,
                                             headers={"Authorization": f"Bearer {cfg.api_key}", **NO_TOKEN_SAVER},
                                             timeout=timeout) as client:
                    return await client.post("/chat/completions", json=body)
            async def bounded():
                return await asyncio.wait_for(request(), timeout=timeout)
            r = asyncio.run(bounded())
        if r.status_code != 200:
            print(f"[llm] {model} → {r.status_code}: {r.text[:120]}")
            return ""
        # proxy returns JSON object or SSE chunks — normalize both
        return _parse_completion(r.text)
    except Exception as e:
        print(f"[llm] {model} text call fail: {e}")
        return ""



def route_intent(question: str, has_image: bool) -> dict | None:
    """LLM chỉ đề xuất ý định; dữ liệu hỏng hoặc lỗi trả None."""
    from sict_crawler.service.retrieval.route_schema import RouteDecision
    cfg = get_settings().llm
    if not cfg.api_key:
        return None
    prompt = (
        "Bạn định tuyến câu hỏi cho SICT. Trả duy nhất JSON với media, wants_file, oos, confidence. "
        "Phân loại theo đầu ra người dùng yêu cầu, không theo chủ đề hoặc loại nguồn có thể chứa đáp án. "
        "media=required khi muốn xem ảnh/video; optional khi muốn mô tả tổng quan hoạt động có ích khi minh họa "
        "hoặc nói về ảnh gửi kèm. media=none khi hỏi dữ kiện cụ thể (ai, số lượng, ngày, địa điểm, kết quả) "
        "hoặc liệt kê thông tin, kể cả câu có nhắc sự kiện, hội thảo, lễ hay cuộc thi. "
        "wants_file=true khi xin tải/xem/lấy văn bản, biểu mẫu, hoặc tra cứu một tài liệu bằng tên "
        "(tên quy chế, quyết định, kế hoạch, tiến độ, thời khóa biểu, danh sách học phần). "
        "wants_file=false khi chỉ hỏi dữ kiện trong/về văn bản (số hiệu, người ký, căn cứ quyết định nào) "
        "hoặc hỏi có những quy chế/quy định nào mà không xin các file. "
        "oos=true khi câu hỏi ngoài phạm vi SICT; confidence từ 0 đến 1. "
        "Ảnh gửi kèm là đầu vào, trừ khi yêu cầu xem thêm ảnh.\n"
        "Phạm vi bao gồm cả thông tin liên quan tới trường HaUI, kể cả khi câu hỏi không nhắc rõ SICT; thiếu dữ liệu không đồng nghĩa ngoài phạm vi.\n"
        "Tuyển dụng, thực tập, hợp tác doanh nghiệp và cơ hội nghề nghiệp cho sinh viên thuộc phạm vi, "
        "kể cả câu chỉ nêu tên doanh nghiệp. Học phí/điểm chuẩn của một trường khác không liên quan HaUI là ngoài phạm vi.\n"
        f"Có ảnh gửi kèm: {has_image}. Câu hỏi: {question}"
    )
    try:
        raw = _text_call(cfg.router_model or cfg.model, prompt, timeout=cfg.router_timeout,
                         max_tokens=cfg.router_max_new_tokens or cfg.max_new_tokens)
        decision = RouteDecision.from_llm(raw)
        return decision.model_dump() if decision else None
    except Exception:
        return None


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


ANSWER_RULES = (
    "Bạn là trợ lý ảo của Trường CNTT & Truyền thông, Đại học Công nghiệp Hà Nội (HaUI). "
    "Trả lời câu hỏi của người dùng CHỈ dựa vào ngữ cảnh được cung cấp. "
    "Trả lời ngắn gọn bằng tiếng Việt. Ngữ cảnh chỉ có một phần thông tin thì trả lời phần có (kèm [n]) rồi nói rõ "
    "phần còn thiếu; chỉ nói không có thông tin khi không nguồn nào liên quan đến câu hỏi. "
    # 2026-11-10 (case kế hoạch đào tạo 2025): model khớp TÊN CHỦ ĐỀ với bất kỳ bài nào mang tên đó và
    # trình bày như đáp án. Tên chủ đề trùng tiêu đề nguồn KHÔNG phải bằng chứng nội dung.
    "Nguồn chỉ trùng tên chủ đề của câu hỏi (cùng nói về \"kế hoạch\", \"quy chế\"…) nhưng không chứa "
    "nội dung cụ thể được hỏi thì trả lời phần thật có trong nguồn (kèm [n]) và nói rõ nguồn chưa chứa "
    "chi tiết được hỏi — KHÔNG được trình bày nội dung nguồn khác như thể nó trả lời câu hỏi. "
    "Ngữ cảnh gồm các nguồn đánh số [1], [2]…; MỌI ý trả lời phải kết thúc bằng số nguồn dạng [n] "
    "(nguồn là file thì kèm số trang có trong nhãn), chỉ dùng số nguồn có trong ngữ cảnh. "
    # RAGAS 2026-10-26: người dùng hay gõ cụm từ / tên văn bản thay vì câu hỏi; model trả "không tìm thấy" dù
    # nguồn [1] đúng tên văn bản (q19, q20), hoặc chỉ nói "xem mục Ảnh & Video" — câu không nguồn nào chứng minh.
    "Người dùng có thể chỉ gõ một cụm từ hoặc tên văn bản thay vì câu hỏi: hiểu đó là yêu cầu cho biết "
    "thông tin chính về chủ đề ấy. "
    "Hỏi về một văn bản, quy định, kế hoạch, thông báo hay file: nêu tên văn bản, số hiệu / ngày ban hành nếu có, "
    "các điểm chính có trong ngữ cảnh (kèm [n]) và cho biết file ở nguồn nào; ngữ cảnh có nguồn mang đúng tên văn "
    "bản đó thì không được trả lời là không tìm thấy. "
    # 2026-11-04: 6 ảnh cùng một bài → 6 ý gần như giống nhau; link OCR đọc sai (hqui thay haui) được chép thành link.
    "Ảnh/video được gom theo bài: dòng \"Bài: … — đăng ngày …\" cho tên bài và ngày đăng (dùng được), mỗi dòng "
    "số nguồn bên dưới là một ảnh/video kèm chữ máy đọc từ ảnh (OCR) hoặc phụ đề video. Người dùng muốn xem "
    "ảnh/video: mỗi bài một ý gồm tên bài, ngày đăng, 2–3 câu nội dung chính của bài hoặc video (lấy từ nguồn văn "
    "bản của bài đó, phụ đề, hoặc chữ trong ảnh) rồi các số nguồn của bài, mỗi số một lần; "
    "không mô tả lặp lại từng ảnh; không nói về giao "
    "diện hay chỗ hiển thị ảnh. Chữ OCR có thể đọc sai chữ và số: không chép đường link, email, số điện thoại, "
    "ngày tháng chỉ có trong chữ OCR (ngày ở dòng \"Bài:\" và trong nguồn văn bản thì dùng được). "
    "Dòng [graph] ở cuối ngữ cảnh chỉ là thông tin phụ về các thực thể liên quan, không phải nguồn để trích [n]. "
    "Câu hỏi yêu cầu liệt kê thì kể đủ mọi mục có trong ngữ cảnh (mỗi mục kèm [n]); ngữ cảnh có thể chưa đủ "
    "danh sách thì nói rõ."
)


def answer_prompt(question: str, context: str) -> str:
    """Prompt trả lời. D27: ngữ cảnh đã nằm trong ngân sách (sources.build_context: llm.source_chars / context_chars)."""
    return f"{ANSWER_RULES}\n\nNgữ cảnh:\n{context}\n\nCâu hỏi: {question}"


def stream_answer(question: str, context: str, image_b64: str | None = None, max_tokens: int | None = None):
    """Yield answer chunks from cfg.llm given retrieved context. SSE-style text.
    Yields nothing if no key/call fails (caller falls back to echo context).
    image_b64: ảnh người dùng gửi (D21) — chỉ đưa vào lượt gọi khi llm.answer_with_image bật; mặc định
    LLM chỉ thấy chữ đã truy xuất."""
    cfg = get_settings().llm
    if not cfg.api_key:
        return
    prompt = answer_prompt(question, context)
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
    # Gọi lại khi lượt stream hỏng TRƯỚC chữ đầu tiên (2026-11-05): model suy luận ngầm (qwen3.8-27b-sale qua kiraai)
    # không gửi byte nào trong lúc nghĩ, gateway cắt kết nối im lặng sau ~60 s ("peer closed connection…") — 18 lượt
    # trên ~46 câu ở máy dev, người dùng nhận ngữ cảnh echo thay vì câu trả lời. Chỉ gọi lại khi lỗi mạng / 5xx / 429:
    # 4xx (model_not_found…) hay hết token cho phần suy nghĩ thì gọi lại cũng vậy. Đã gửi chữ rồi thì không gọi lại
    # (UI đã hiện phần đầu câu trả lời).
    # Lượt gọi lại KHÔNG stream: trên production cùng câu đó stream bị cắt 3/3 lượt (~190 s rồi echo), còn gọi không
    # stream trả lời trong ~17 s với ~500 token (đo 2026-11-05, ngữ cảnh ~20k ký tự) — lỗi nằm ở đường stream của
    # gateway chứ không ở độ dài suy nghĩ. Câu trả lời đến một lần thay vì từng mẩu.
    attempts = 1 + max(0, int(getattr(cfg, "stream_retries", 0)))
    for attempt in range(1, attempts + 1):
        st = {"wrote": 0, "thought": 0, "retry": False}
        if attempt == 1:
            yield from _stream_once(cfg, body, st)
        else:
            yield from _complete_once(cfg, {**body, "stream": False}, st)
        if st["wrote"] or not st["retry"]:
            break
        if attempt < attempts:
            print(f"[llm] stream_answer: gọi lại lần {attempt}/{attempts - 1} (không stream)")
    if st["thought"] and not st["wrote"]:
        # model suy luận dùng hết max_tokens cho phần suy nghĩ → caller echo ngữ cảnh
        print(f"[llm] stream_answer: chỉ có {st['thought']} ký tự suy luận, không có câu trả lời — tăng llm.max_new_tokens "
              f"(hiện {body['max_tokens']})")


def _complete_once(cfg, body: dict, st: dict):
    """Một lượt /chat/completions không stream: yield cả `content` một lần (nếu có); cùng quy ước st như _stream_once."""
    try:
        with _client(cfg) as c:
            r = c.post("/chat/completions", json=body, timeout=max(float(getattr(cfg, "timeout", 60)), 180.0))
        if r.status_code != 200:
            print(f"[llm] stream_answer HTTP {r.status_code}: {r.text[:300]}")
            st["retry"] = r.status_code >= 500 or r.status_code == 429
            return
        msg = ((r.json().get("choices") or [{}])[0].get("message") or {})
        st["thought"] += len(msg.get("reasoning_content") or msg.get("reasoning") or "")
        if msg.get("content"):
            st["wrote"] += 1
            yield msg["content"]
    except Exception as e:
        print(f"[llm] stream_answer fail (không stream): {e}")
        st["retry"] = True


def _stream_once(cfg, body: dict, st: dict):
    """Một lượt stream /chat/completions: yield từng mẩu `content`; ghi vào st số mẩu đã gửi (wrote), số ký tự suy luận
    (thought) và retry=True khi lỗi tạm thời (mạng, 5xx, 429) — stream_answer quyết định có gọi lại không."""
    try:
        with _client(cfg) as c:
            with c.stream("POST", "/chat/completions", json=body) as r:
                if r.status_code != 200:
                    # VD 404 model_not_found khi nhà cung cấp gỡ model: thân lỗi là JSON, không phải SSE, nên
                    # trước đây không yield gì mà cũng không báo → /chat lặng lẽ echo ngữ cảnh (2026-11-04)
                    print(f"[llm] stream_answer HTTP {r.status_code}: {r.read()[:300].decode('utf-8', 'replace')}")
                    st["retry"] = r.status_code >= 500 or r.status_code == 429
                    return
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
                        st["thought"] += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
                        if delta.get("content"):
                            st["wrote"] += 1
                            yield delta["content"]
    except Exception as e:
        print(f"[llm] stream_answer fail: {e}")
        st["retry"] = True
