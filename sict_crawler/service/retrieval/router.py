"""Router thành phần câu trả lời (D17): ý định (trước tìm kiếm) + chặn theo bằng chứng (sau).

Không phải câu hỏi nào cũng có ảnh liên quan. Hai tín hiệu:
  1. Ý định — media `none|optional|required` + `wants_file` + `wants_video`. Nguồn: JSON từ chính lượt LLM viết lại
     câu hỏi (vlm.rewrite_and_route, không thêm lượt gọi) hoặc luật từ khoá khi không có LLM.
     Ý định quyết định BAO NHIÊU media (`required` tối đa top_k, `optional` tối đa optional_max, `none` tối đa
     none_max — mặc định 0), không quyết định ảnh nào.
  2. Bằng chứng — `gate` (2026-11-05, nhánh feat/media-evidence-gate): ứng viên gồm ảnh / khung hình của chính các bài
     đứng đầu nhánh chữ (modality `doc`, cosine câu hỏi↔ảnh), CLIP toàn collection, caption. Ảnh "gắn" (thuộc bài chữ
     đứng đầu) xếp trước theo hạng bài rồi cosine; `optional` / `none` chỉ nhận ảnh gắn; `caption_kw` (khớp từ khoá
     caption, không có điểm gốc) chỉ được nhận khi gắn. Mọi media có điểm phải vượt ngưỡng điểm GỐC theo loại (cosine
     ảnh, cosine caption) — không cắt bằng điểm RRF. Không gì đạt → rỗng (AC-R9), không trả ảnh sai cho đủ số.
     Mỗi video chiếm 1 vị trí (AC-R3). Câu đòi video (`wants_video`): video xếp trước, ảnh chỉ nhận khi gắn.
  3. Sau `agent.evidence`: `sufficient` False → chỉ giữ media gắn với bài chữ đứng đầu, đúng loại được hỏi
     (`supported_media`); câu ngoài phạm vi không còn ảnh ngẫu nhiên.

Ngưỡng ở config `router` — giá trị mặc định là TẠM, hiệu chỉnh bằng bộ câu hỏi vàng (AC-G3).
Callers: retriever.retrieve, agent.run, agent._lexical_fallback.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from sict_crawler.core.text import fold

MEDIA_MODES = ("none", "optional", "required")
_RANK = {m: i for i, m in enumerate(MEDIA_MODES)}

# hỏi thẳng về hình/video (độ chính xác cao). "anh" đứng riêng dễ trùng "tiếng Anh"/"anh chị"
# nên chỉ nhận cụm có ngữ cảnh.
# Câu đòi video: chính hai từ "video|clip" của _REQUIRED (không thêm từ khoá) → gate xếp video trước (wants_video).
_VIDEO = r"\bvideo\b|\bclip\b"
_REQUIRED_EXPLICIT = re.compile(
    r"\bhinh anh\b|\banh chup\b|\bxem anh\b|\bcho xem\b|\bbo anh\b|\balbum\b|\bthu vien anh\b"
    r"|" + _VIDEO + r"|\btrong nhu the nao\b|\btrong ra sao\b")
# "ảnh của …" nhập nhằng: vừa là đòi xem, vừa là ảnh người dùng gửi ("Đây là ảnh của trường nào?").
# Tách khỏi _REQUIRED_EXPLICIT để classify_rules bỏ nhánh này khi có ảnh đầu vào (_INPUT_CONTEXT).
_REQUIRED_REF = re.compile(
    r"(?:^|[^a-z])(?<!tieng )anh (?:cac|cua|ve|le|su kien|hoi|cuoc thi|ngay|buoi|nhung)\b")
_REQUIRED = re.compile(_REQUIRED_EXPLICIT.pattern + "|" + _REQUIRED_REF.pattern)
# sự kiện → ảnh có thể giúp ích nhưng không bắt buộc. "le" đứng riêng trùng họ "Lê" khi bỏ dấu
# → chỉ nhận "lễ <loại lễ>".
_EVENT = re.compile(
    r"\ble (?:khai giang|tot nghiep|ky niem|trao|ra mat|chao|phat bang|tong ket|vinh danh|don|bao ve)\b"
    r"|\bdien ra\b|\bawards?\b|\bgiai thuong\b|\bhoi thao\b|\bhoi nghi\b|\bcuoc thi\b|\bngay hoi\b|\bsu kien\b|\bky niem\b"
    r"|\bkhai giang\b|\btong ket\b|\btrao giai\b|\bvinh danh\b|\bhoi cho\b|\bworkshop\b"
    r"|\bseminar\b|\bfestival\b|\btham gia\b|\bchung ket\b|\btham quan\b")
# muốn tài liệu/file đính kèm. "tai" đứng riêng = "tại" → chỉ nhận "tai ve/tai xuong/tai file".
_FILE = re.compile(
    r"\btai ve\b|\btai xuong\b|\btai file\b|\bdownload\b|\bfile\b|\bbieu mau\b|\bmau don\b"
    r"|\bquy che\b|\bquy dinh\b|\btien do dao tao\b|\blich thuc hanh\b|\bthoi khoa bieu\b"
    r"|\bke hoach dao tao\b|\blop hoc phan\b|\bquyet dinh\b")
# D27: câu hỏi liệt kê — đáp án rải trên nhiều bài, không đoạn nào chứa đủ ("có những ngành nào", "danh sách PGS")
# → retrieve lấy rộng hơn, mỗi bài 1 đoạn. Nhận nhầm chỉ làm truy xuất rộng hơn, không hỏng câu trả lời.
_LIST = re.compile(
    r"\b(?:nhung|cac)\b.{0,60}?\bnao\b|\bnhung (?:ai|gi)\b|\b(?:gom|bao gom) nhung\b"
    r"|\bdanh sach\b|\bliet ke\b|\bke ten\b"
    r"|\bco (?:bao nhieu|may) (?:nganh|khoa|chuong trinh|cau lac bo|clb|phong|trung tam|doi|nhom|phuong thuc)\b")


def is_list_question(question: str) -> bool:
    return bool(_LIST.search(fold(question or "")))


@dataclass
class Route:
    media: str = "none"
    wants_file: bool = False
    source: str = "rules"   # rules | llm | llm+rules
    entities: list[str] = field(default_factory=list)  # khái niệm graph (D33) nêu trong câu hỏi (tên hiển thị)
    inputs: list[str] = field(default_factory=list)    # D21: đầu vào thực tế của truy vấn: text | image
    outputs: list[str] = field(default_factory=list)   # D21: đầu ra thực tế: text | image
    list_question: bool = False                         # D27: câu hỏi liệt kê (is_list_question) → truy xuất rộng
    wants_video: bool = False                           # câu nêu "video"/"clip" → gate xếp video trước

    def to_dict(self) -> dict:
        return asdict(self)


# "ảnh" CÓ DẤU đứng riêng = hình ảnh ("Ảnh PGS.TS …", "ảnh cô …", "ảnh hoạt động CLB …"); bản bỏ
# dấu trùng "anh" (anh chị, tiếng Anh) nên chỉ xét chữ gốc. Trừ "ảnh hưởng".
_ANH_ACCENT = re.compile(r"(?<![^\W\d_])ảnh(?![^\W\d_])(?!\s+hưởng)")
# Cụm quy chiếu ẢNH ĐẦU VÀO của người dùng (I2): "sự kiện trong ảnh này là gì" hỏi về ảnh mình
# gửi, không phải đòi xem thêm ảnh → chữ "ảnh" không tự nâng media lên required.
_INPUT_CONTEXT = re.compile(r"ảnh này|ảnh trên|trong ảnh|của ảnh|ảnh của|ảnh đính kèm|photo|picture")


def classify_rules(question: str, has_image: bool = False) -> Route:
    q = fold(question)
    qn = unicodedata.normalize("NFC", question.lower())
    input_ctx = bool(has_image and _INPUT_CONTEXT.search(qn))
    bare_anh = bool(_ANH_ACCENT.search(qn)) and not input_ctx
    required = bool(_REQUIRED_EXPLICIT.search(q)) or (not input_ctx and bool(_REQUIRED_REF.search(q)))
    if required or bare_anh:
        media = "required"
    elif input_ctx or _EVENT.search(q):
        media = "optional"  # ảnh gửi kèm là đầu vào: không đòi ảnh nhưng vẫn có nhánh media
    else:
        media = "none"
    return Route(media=media, wants_file=bool(_FILE.search(q)), source="rules",
                 inputs=["image"] if has_image else [],
                 wants_video=bool(re.search(_VIDEO, q)))


def decide(question: str, llm: dict | None = None, has_image: bool = False) -> Route:
    """Gộp ý định từ LLM (nếu hợp lệ) với luật. Luật `required` (từ khoá hình/video rõ ràng) và
    luật `wants_file` luôn được giữ — LLM chỉ có thể nâng mức, không hạ được tín hiệu rõ ràng."""
    rules = classify_rules(question, has_image=has_image)
    media = (llm or {}).get("media")
    if media not in MEDIA_MODES:
        return rules
    if rules.media == "required":
        media = "required"
    wants = bool((llm or {}).get("wants_file")) or rules.wants_file
    return Route(media=media, wants_file=wants, source="llm+rules",
                 inputs=rules.inputs, wants_video=rules.wants_video)


def _docs_of(h: dict) -> list[str]:
    """Bài chứa media. Point ảnh v2 dùng chung nhiều bài → document_id là danh sách."""
    v = h.get("doc_id", h.get("document_id"))
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _doc_of(h: dict) -> str | None:
    d = _docs_of(h)
    return d[0] if d else None


def _media_key(h: dict) -> tuple:
    kind = h.get("media_kind") if h.get("kind") == "media_caption" else h.get("kind")
    if kind in ("video_frame", "video"):
        return ("video", h.get("video_key") or h.get("url"))   # 1 video = 1 vị trí
    return ("image", h.get("media_key") or h.get("media_id") or h.get("sha256") or h.get("url"))


def is_video(h: dict) -> bool:
    return _media_key(h)[0] == "video"


def passes(h: dict, clip_min: float, caption_min: float,
           i2i_min: float | None = None, i2t_min: float | None = None) -> bool:
    """Vượt ngưỡng điểm gốc theo loại. caption_kw (khớp từ khoá caption, không có điểm gốc) không tự vượt được:
    gate chỉ nhận nó khi media thuộc bài chữ đứng đầu (trước 2026-11-05 nó luôn qua, nên câu ngoài phạm vi "cho xem ảnh
    tháp Eiffel" nhận 8 ảnh caption_kw ngẫu nhiên)."""
    mod = h.get("modality")
    raw = h.get("raw_score")
    if mod in ("clip", "doc"):   # doc: ảnh của bài chữ đứng đầu, raw = cosine câu hỏi↔ảnh như clip
        return raw is not None and raw >= clip_min
    if mod == "caption":
        return raw is not None and raw >= caption_min
    if mod == "i2i":    # ảnh truy vấn ↔ ảnh trong index (D21 U3/U6)
        return raw is not None and raw >= (clip_min if i2i_min is None else i2i_min)
    if mod == "i2t":    # ảnh truy vấn ↔ caption (D21 U4)
        return raw is not None and raw >= (caption_min if i2t_min is None else i2t_min)
    return mod == "caption_kw"


_MOD_ORDER = {"graph": 0, "doc": 1, "clip": 1, "i2i": 1, "caption": 2, "i2t": 2, "caption_kw": 3}


def media_cap(route: Route, top_k: int, optional_max: int, none_max: int = 0) -> int:
    """Số media tối đa theo ý định: hỏi thẳng ảnh/video → top_k; sự kiện → optional_max; còn lại → none_max."""
    return {"required": top_k, "optional": optional_max}.get(route.media, none_max)


def gate(route: Route, top_text_hits: list[dict], candidates: list[dict], top_k: int,
         clip_min: float, caption_min: float, optional_max: int = 3,
         i2i_min: float | None = None, i2t_min: float | None = None, none_max: int = 0) -> list[dict]:
    """Chọn media hiển thị. candidates: dict có `modality` (doc|clip|caption|caption_kw|i2i|i2t), `raw_score`,
    `document_id`/`doc_id`. top_text_hits: các hit chữ đứng đầu (đã xếp hạng).

    Ảnh "gắn" = thuộc một bài trong top_text_hits; nó đứng trước, theo hạng bài rồi loại rồi cosine, và mang
    `page_url` = url của bài chữ đã kéo nó lên (ảnh dùng chung nhiều bài thì bài cha là bài đang trả lời). Chia lượt
    giữa các bài (mỗi hit chữ một ảnh) đã thử 2026-11-05: media_v1 optional precision 0,974 → 0,634, image 0,82 → 0,72
    (bài hạng 1 thường đúng), nên không dùng.
    `optional` / `none` chỉ nhận ảnh gắn (hoặc ảnh giống ảnh người dùng gửi), `required` nhận thêm ảnh không gắn đủ
    ngưỡng. `route.wants_video`: video đứng trước mọi ảnh, ảnh chỉ nhận khi gắn; bài chữ đứng đầu có video → bỏ mọi
    media không gắn."""
    cap = media_cap(route, top_k, optional_max, none_max)
    if cap <= 0:
        return []
    top_docs = [d for d in (_doc_of(h) for h in top_text_hits) if d]
    rank_of_doc = {d: i for i, d in reversed(list(enumerate(top_docs)))}
    url_of_doc: dict[str, str] = {}
    for h in top_text_hits:
        d = _doc_of(h)
        if d and d not in url_of_doc and h.get("url"):
            url_of_doc[d] = h["url"]
    best: dict[tuple, dict] = {}
    for h in candidates:
        if not passes(h, clip_min, caption_min, i2i_min, i2t_min):
            continue
        mod = h.get("modality")
        cdoc = min((d for d in _docs_of(h) if d in rank_of_doc), key=lambda d: rank_of_doc[d], default=None)
        rank = None if cdoc is None else rank_of_doc[cdoc]
        # ảnh giống ảnh người dùng gửi (i2i) là bằng chứng trực tiếp → đứng đầu, không cần thuộc top bài chữ. Truy vấn
        # chỉ có ảnh: "top bài chữ" lại do caption i2t yếu (cosine ~0,38) quyết định → trước đây ảnh trùng 0,99 bị
        # xếp sau 5 caption đó và rơi khỏi top_k (đo 2026-10-20 trên index v3).
        if mod == "i2i":
            rank = -1
        coupled = rank is not None
        user_img = mod in ("i2i", "i2t")   # ảnh người dùng gửi là yêu cầu rõ ràng: không buộc thuộc top bài chữ
        if not coupled and (mod == "caption_kw" or (route.media != "required" and not user_img)
                            or (route.wants_video and not is_video(h) and not user_img)):
            continue
        if cdoc is not None and cdoc in url_of_doc:
            h = {**h, "page_url": url_of_doc[cdoc]}
        k = _media_key(h)
        cur = best.get(k)
        score = h.get("raw_score") or 0.0
        if cur is None or (coupled, score) > (cur["_coupled"], cur.get("raw_score") or 0.0):
            best[k] = {**h, "_coupled": coupled, "_rank": len(top_docs) if rank is None else rank}

    def order(h):
        return _MOD_ORDER.get(h.get("modality"), 4), -(h.get("raw_score") or 0.0), -(h.get("score") or 0.0)

    vals = list(best.values())
    if route.wants_video and any(h["_coupled"] and is_video(h) for h in vals if h["_rank"] >= 0):
        # bài chữ đứng đầu có video → đó là câu trả lời; video không gắn (CLIP khung hình yếu) chỉ là dự phòng khi không có
        vals = [h for h in vals if h["_coupled"]]
    mine = sorted((h for h in vals if h["_rank"] >= 0 and h["_coupled"]), key=lambda h: (h["_rank"], order(h)))
    loose = sorted((h for h in vals if not h["_coupled"]), key=order)
    kept = sorted((h for h in vals if h["_rank"] < 0), key=order)    # ảnh giống ảnh người dùng gửi
    if route.wants_video:   # video của bài chữ → video khác (dự phòng) → ảnh của bài chữ; ảnh không gắn đã bị loại
        kept += [h for h in mine if is_video(h)] + [h for h in loose if is_video(h)] + [h for h in mine if not is_video(h)]
    else:
        kept += mine + loose
    return [{k: v for k, v in h.items() if k not in ("_coupled", "_rank")} for h in kept[:cap]]


def supported_media(media: list, text: list, n_top: int, wants_video: bool = False) -> list:
    """C (2026-11-05): `sufficient` False (agent.evidence) → chỉ giữ media thuộc `n_top` nguồn chữ đứng đầu
    (`parent_url` ∈ url / parent_url của chúng; gate đặt `page_url` của ảnh gắn = url bài đó), đúng loại được hỏi (câu
    đòi video → chỉ video), và ảnh giống ảnh người dùng gửi (`via` i2i). Chữ yếu thì ảnh không gắn với bài nào chỉ là
    ảnh gần nghĩa ngẫu nhiên: "cho xem ảnh tháp Eiffel" trước đây ra 8 ảnh. media / text: Source (hoặc vật có cùng thuộc
    tính)."""
    top = set()
    for s in text[:n_top]:
        top.update(u for u in (getattr(s, "url", None), getattr(s, "parent_url", None)) if u)
    out = []
    for m in media:
        if getattr(m, "via", None) == "i2i":
            out.append(m)
        elif getattr(m, "parent_url", None) in top and (not wants_video or (getattr(m, "kind", "") or "").startswith("video")):
            out.append(m)
    return out
