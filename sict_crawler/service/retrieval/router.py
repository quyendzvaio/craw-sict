"""Router thành phần câu trả lời (D17): ý định (trước tìm kiếm) + chặn theo bằng chứng (sau).

Không phải câu hỏi nào cũng có ảnh liên quan. Hai tín hiệu:
  1. Ý định — media `none|optional|required` + `wants_file`. Nguồn: JSON từ chính lượt LLM viết lại
     câu hỏi (vlm.rewrite_and_route, không thêm lượt gọi) hoặc luật từ khoá khi không có LLM.
     `none` → api bỏ hẳn nhánh media (không CLIP, không caption trong ngữ cảnh LLM).
  2. Bằng chứng — `gate`: `optional` chỉ giữ media thuộc các bài đứng đầu nhánh chữ (tối đa 3);
     `required` tối đa top_k. Mọi media phải vượt ngưỡng điểm GỐC theo loại (cosine CLIP, cosine E5
     của caption) — không cắt bằng điểm RRF. Không gì đạt → rỗng (AC-R9), không trả ảnh sai cho đủ số.
     Mỗi video chiếm 1 vị trí (AC-R3).

Ngưỡng ở config `router` — giá trị mặc định là TẠM, hiệu chỉnh bằng bộ câu hỏi vàng (AC-G3).
Graph (D20): ảnh/video gắn thực thể nêu trong câu hỏi (modality `graph`, raw_score = trọng số cạnh)
đứng trước CLIP — tên người/sự kiện là bằng chứng mạnh hơn cosine CLIP (text encoder tiếng Anh).
Callers: api.retrieve, agent._lexical_fallback.
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
_REQUIRED = re.compile(
    r"\bhinh anh\b|\banh chup\b|\bxem anh\b|\bcho xem\b|\bbo anh\b|\balbum\b|\bthu vien anh\b"
    r"|\bvideo\b|\bclip\b|\btrong nhu the nao\b|\btrong ra sao\b"
    r"|(?:^|[^a-z])(?<!tieng )anh (?:cac|cua|ve|le|su kien|hoi|cuoc thi|ngay|buoi|nhung)\b")
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
    entities: list[str] = field(default_factory=list)  # thực thể graph nêu trong câu hỏi (tên hiển thị)
    facts: list[str] = field(default_factory=list)     # dữ kiện graph (thực thể liên quan) cho LLM
    inputs: list[str] = field(default_factory=list)    # D21: đầu vào thực tế của truy vấn: text | image
    outputs: list[str] = field(default_factory=list)   # D21: đầu ra thực tế: text | image
    list_question: bool = False                         # D27: câu hỏi liệt kê (is_list_question) → truy xuất rộng

    def to_dict(self) -> dict:
        return asdict(self)


# "ảnh" CÓ DẤU đứng riêng = hình ảnh ("Ảnh PGS.TS …", "ảnh cô …", "ảnh hoạt động CLB …"); bản bỏ
# dấu trùng "anh" (anh chị, tiếng Anh) nên chỉ xét chữ gốc. Trừ "ảnh hưởng".
_ANH_ACCENT = re.compile(r"(?<![^\W\d_])ảnh(?![^\W\d_])(?!\s+hưởng)")


def classify_rules(question: str) -> Route:
    q = fold(question)
    if _REQUIRED.search(q) or _ANH_ACCENT.search(unicodedata.normalize("NFC", question.lower())):
        media = "required"
    elif _EVENT.search(q):
        media = "optional"
    else:
        media = "none"
    return Route(media=media, wants_file=bool(_FILE.search(q)), source="rules")


def decide(question: str, llm: dict | None = None) -> Route:
    """Gộp ý định từ LLM (nếu hợp lệ) với luật. Luật `required` (từ khoá hình/video rõ ràng) và
    luật `wants_file` luôn được giữ — LLM chỉ có thể nâng mức, không hạ được tín hiệu rõ ràng."""
    rules = classify_rules(question)
    media = (llm or {}).get("media")
    if media not in MEDIA_MODES:
        return rules
    if rules.media == "required":
        media = "required"
    wants = bool((llm or {}).get("wants_file")) or rules.wants_file
    return Route(media=media, wants_file=wants, source="llm+rules")


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


def passes(h: dict, clip_min: float, caption_min: float, graph_min: float = 0.6,
           i2i_min: float | None = None, i2t_min: float | None = None) -> bool:
    """Vượt ngưỡng điểm gốc theo loại. caption_kw (khớp từ khoá caption) coi là có bằng chứng.
    graph: raw_score = trọng số cạnh ảnh↔thực thể nêu trong câu hỏi (graph D20)."""
    mod = h.get("modality")
    raw = h.get("raw_score")
    if mod == "clip":
        return raw is not None and raw >= clip_min
    if mod == "caption":
        return raw is not None and raw >= caption_min
    if mod == "graph":
        return raw is not None and raw >= graph_min
    if mod == "i2i":    # ảnh truy vấn ↔ ảnh trong index (D21 U3/U6)
        return raw is not None and raw >= (clip_min if i2i_min is None else i2i_min)
    if mod == "i2t":    # ảnh truy vấn ↔ caption (D21 U4)
        return raw is not None and raw >= (caption_min if i2t_min is None else i2t_min)
    return mod == "caption_kw"


def gate(route: Route, top_text_hits: list[dict], candidates: list[dict], top_k: int,
         clip_min: float, caption_min: float, optional_max: int = 3,
         graph_min: float = 0.6, i2i_min: float | None = None, i2t_min: float | None = None) -> list[dict]:
    """Chọn media hiển thị. candidates: dict có `modality` (clip|caption|caption_kw), `raw_score`,
    `document_id`/`doc_id`. top_text_hits: các hit chữ đứng đầu (đã xếp hạng)."""
    if route.media == "none":
        return []
    top_docs = [d for d in (_doc_of(h) for h in top_text_hits) if d]
    rank_of_doc = {d: i for i, d in reversed(list(enumerate(top_docs)))}
    best: dict[tuple, dict] = {}
    for h in candidates:
        if not passes(h, clip_min, caption_min, graph_min, i2i_min, i2t_min):
            continue
        rank = min((rank_of_doc[d] for d in _docs_of(h) if d in rank_of_doc), default=None)
        # ảnh giống ảnh người dùng gửi (i2i) là bằng chứng trực tiếp → đứng đầu, không cần thuộc top bài chữ. Truy vấn
        # chỉ có ảnh: "top bài chữ" lại do caption i2t yếu (cosine ~0,38) quyết định → trước đây ảnh trùng 0,99 bị
        # xếp sau 5 caption đó và rơi khỏi top_k (đo 2026-10-20 trên index v3).
        if h.get("modality") == "i2i":
            rank = -1
        coupled = rank is not None
        # ảnh người dùng gửi là yêu cầu rõ ràng: ảnh giống không bị buộc phải thuộc top bài chữ
        if route.media == "optional" and not coupled and h.get("modality") not in ("i2i", "i2t"):
            continue
        k = _media_key(h)
        cur = best.get(k)
        score = h.get("raw_score") or 0.0
        if cur is None or (coupled, score) > (cur["_coupled"], cur.get("raw_score") or 0.0):
            best[k] = {**h, "_coupled": coupled, "_rank": len(top_docs) if rank is None else rank}
    kept = sorted(best.values(), key=lambda h: (
        not h["_coupled"], h["_rank"],
        {"graph": 0, "clip": 1, "i2i": 1, "caption": 2, "i2t": 2, "caption_kw": 3}.get(h.get("modality"), 4),
        -(h.get("raw_score") or 0.0), -(h.get("score") or 0.0)))
    cap = optional_max if route.media == "optional" else top_k
    return [{k: v for k, v in h.items() if k not in ("_coupled", "_rank")} for h in kept[:cap]]
