"""Metadata filtering cho retrieval: lọc CỨNG (API/UI truyền rõ) + ưu tiên MỀM (suy từ câu hỏi).

Suy luận từ câu hỏi chỉ nâng điểm, không loại kết quả: "tuyển sinh 2025" có thể nằm trong bài đăng
năm 2024, "quy chế" có thể nằm ở trang tĩnh lẫn file đính kèm — lọc cứng theo phỏng đoán dễ trả rỗng.
Lọc cứng chỉ áp dụng khi caller truyền `filters` (VD dropdown mục / năm trên UI).

Payload/metadata dùng (index spec v2, indexing.DocMeta): section, doc_type, year, published_ts.
Tài liệu thiếu trường (index cũ, trang tĩnh không ngày) → boost trung tính (×1), không bị phạt.

Callers: api.retrieve, agent._lexical_fallback, vector_store (to_qdrant), scripts/eval_retrieval.py.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from sict_crawler.core.sparse import question_terms
from sict_crawler.core.text import fold

DOC_TYPES = {"article", "page", "attachment"}
MEDIA_KINDS = {"image", "video"}

# từ khoá (không dấu, lowercase) → các section slug thật của site (đo trên snapshot 2026-10-12).
# Trang tĩnh /vn/html/<slug> mỗi slug là 1 section (VD trang ngành), nên 1 ý có thể ứng nhiều slug.
_PROGRAMS = ["cong-nghe-thong-tin", "khoa-hoc-may-tinh", "ky-thuat-phan-mem", "an-toan-thong-tin",
             "tri-tue-nhan-tao", "dai-hoc-he-thong-thong-tin", "cong-nghe-da-phuong-tien",
             "mang-may-tinh-va-truyen-thong-du-lieu", "khoa-hoc-may-tinh-bang-tieng-anh"]
_SECTION_HINTS: list[tuple[str, list[str]]] = [
    (r"\bthong bao\b", ["thong-bao"]),
    (r"\btuyen sinh\b|\bxet tuyen\b|\bdiem chuan\b|\bchi tieu\b",
     ["tuyen-sinh-dai-hoc", "tuyen-sinh-sau-dai-hoc"]),
    (r"\bsau dai hoc\b|\bthac si\b|\btien si\b|\bcao hoc\b|\bnghien cuu sinh\b",
     ["tuyen-sinh-sau-dai-hoc", "sau-dai-hoc", "cao-hoc-he-thong-thong-tin"]),
    (r"\bnganh\b|\bchuong trinh dao tao\b|\bctdt\b", ["tuyen-sinh-dai-hoc", *_PROGRAMS]),
    (r"\btuyen dung\b|\bviec lam\b|\bthuc tap\b", ["tuyen-dung"]),
    (r"\bnghien cuu\b|\bnckh\b|\bbai bao\b|\bcong trinh\b|\bde tai\b",
     ["tin-khcn", "sinh-vien-nckh", "danh-muc-cong-trinh-da-cong-bo", "de-tai-du-an"]),
    (r"\bcau lac bo\b|\bclb\b|\bdoi olympic\b", ["cau-lac-bo"]),
    (r"\bke hoach\b|\blich thi\b|\blich hoc\b|\blich thuc hanh\b|\bthoi khoa bieu\b|\blop hoc phan\b",
     ["ke-hoach"]),
    (r"\btien do\b", ["tien-do"]),
    (r"\bquy che\b|\bquy dinh\b|\bchuan dau ra\b", ["quy-che"]),
    (r"\bgiang vien\b|\bcan bo\b|\btruong khoa\b|\bpgs\b|\bthay\b|\bco giao\b", ["can-bo-giang-vien"]),
    (r"\bgioi thieu\b|\blich su\b|\bchien luoc\b|\bsu menh\b|\btam nhin\b",
     ["gioi-thieu", "chien-luoc-phat-trien", "thong-tin-chung"]),
    (r"\bbo may\b|\bco cau to chuc\b|\bban giam hieu\b|\bcac khoa\b|\bphong ban\b|\btrung tam\b",
     ["bo-may-to-chuc", "khoa", "phong-trung-tam", "khoa-cong-nghe-thong-tin", "khoa-cong-nghe-phan-mem",
      "khoa-khoa-hoc-may-tinh", "khoa-mang-may-tinh-va-truyen-thong", "phong-tong-hop"]),
    (r"\blien he\b|\bdia chi\b|\bso dien thoai\b|\bemail\b", ["dia-chi-lien-he"]),
    (r"\bco so vat chat\b|\bphong may\b|\bphong thi nghiem\b", ["co-so-vat-chat"]),
    (r"\bguong sang\b|\bguong mat tieu bieu\b|\bsinh vien tieu bieu\b", ["guong-sang"]),
    (r"\bkhoa hoc ngan han\b|\bdao tao ngan han\b|\bkhoa hoc cho tre\b", ["dao-tao-ngan-han"]),
    (r"\btin tuc\b|\bsu kien\b", ["tin-tuc"]),
    (r"\bthu vien anh\b|\bhinh anh\b", ["thu-vien-anh"]),
]
_RECENT = re.compile(r"\bmoi nhat\b|\bgan day\b|\bvua qua\b|\bhien nay\b|\bhien hanh\b|"
                     r"\bsap toi\b|\btuan nay\b|\bthang nay\b|\bhoc ky nay\b|\bky nay\b")
# từ chung, không làm câu "mục + mới nhất" có nội dung riêng ("thông báo mới nhất của trường là gì")
_GENERIC = frozenset("truong nha khoa sict haui cntt tt dai hoc cong nghiep ha noi nghe thong tin truyen nhat".split())
_YEAR = re.compile(r"\b(20\d{2})\b")
_SCHOOL_YEAR = re.compile(r"\b(20\d{2})\s*[-–/]\s*(20\d{2})\b")

SECTION_BOOST = 1.25
DOC_TYPE_BOOST = 1.3
YEAR_BOOST = 1.3
RECENCY_MAX_BOOST = 0.35      # ×(1 + 0.35·e^(-tuổi/365))
RECENCY_HALF_DAYS = 365.0


@dataclass
class QueryFilters:
    # cứng (caller truyền rõ)
    sections: list[str] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    # mềm (suy từ câu hỏi)
    prefer_sections: list[str] = field(default_factory=list)
    prefer_years: list[int] = field(default_factory=list)
    recency: bool = False
    # câu hỏi chỉ gồm mục + "mới nhất" ("thông báo mới nhất?"): tìm theo nghĩa không sắp được theo ngày → retriever
    # đặt các bài mới nhất của mục lên đầu (vector_store.latest_text)
    latest: bool = False
    prefer_doc_types: list[str] = field(default_factory=list)  # router wants_file → ["attachment"]

    def has_hard(self) -> bool:
        return bool(self.sections or self.doc_types or self.year_from or self.year_to)

    def cache_key(self) -> str:
        d = asdict(self)
        return "|".join(f"{k}={d[k]}" for k in sorted(d) if d[k])

    def to_dict(self) -> dict:
        return asdict(self)


def from_request(d: dict | None) -> QueryFilters:
    """Validate filters từ API. Khoá lạ / giá trị sai → bỏ qua (không lỗi request)."""
    f = QueryFilters()
    if not d:
        return f
    slug = re.compile(r"^[a-z0-9-]{2,60}$")
    f.sections = [s for s in (d.get("sections") or []) if isinstance(s, str) and slug.match(s)][:10]
    f.doc_types = [t for t in (d.get("doc_types") or []) if t in DOC_TYPES]
    for k in ("year_from", "year_to"):
        v = d.get(k)
        if isinstance(v, int) and 2000 <= v <= 2100:
            setattr(f, k, v)
    return f


def infer(question: str, today: date | None = None) -> QueryFilters:
    today = today or date.today()
    q = fold(question)
    f = QueryFilters()
    rest = q
    for pat, secs in _SECTION_HINTS:
        if re.search(pat, q):
            f.prefer_sections += [x for x in secs if x not in f.prefer_sections]
            rest = re.sub(pat, " ", rest)
    years: list[int] = []
    for a, b in _SCHOOL_YEAR.findall(q):
        years += [int(a), int(b)]
    years += [int(y) for y in _YEAR.findall(q)]
    if re.search(r"\bnam nay\b", q):
        years.append(today.year)
    if re.search(r"\bnam ngoai\b|\bnam truoc\b", q):
        years.append(today.year - 1)
    f.prefer_years = sorted({y for y in years if 2000 <= y <= today.year + 2})
    f.recency = bool(_RECENT.search(q)) or (bool(re.search(r"\bnam nay\b", q)) and not f.prefer_years)
    left = [t for t in question_terms(_RECENT.sub(" ", rest)) if t not in _GENERIC]
    f.latest = bool(_RECENT.search(q)) and bool(f.prefer_sections) and not left
    return f


def merge(explicit: QueryFilters, inferred: QueryFilters) -> QueryFilters:
    out = QueryFilters(**{k: v for k, v in asdict(explicit).items()})
    out.prefer_sections = inferred.prefer_sections
    out.prefer_years = inferred.prefer_years
    out.recency = inferred.recency
    out.latest = inferred.latest
    return out


def matches(meta: dict, f: QueryFilters) -> bool:
    """Lọc cứng trên metadata 1 hit/chunk (dùng cho eval offline và hậu lọc)."""
    if f.sections and meta.get("section") not in f.sections:
        return False
    if f.doc_types and meta.get("doc_type") not in f.doc_types:
        return False
    y = meta.get("year")
    if f.year_from and (y is None or y < f.year_from):
        return False
    if f.year_to and (y is None or y > f.year_to):
        return False
    return True


def _age_days(meta: dict, today: date) -> float | None:
    pub = meta.get("published_at")
    if not pub:
        return None
    try:
        return max(0.0, (today - datetime.fromisoformat(pub).date()).days)
    except ValueError:
        return None


def boost(meta: dict, f: QueryFilters, today: date | None = None) -> float:
    """Hệ số nhân điểm ≥ 1 theo ưu tiên mềm. Thiếu metadata → 1.0."""
    today = today or date.today()
    m = 1.0
    if f.prefer_sections and meta.get("section") in f.prefer_sections:
        m *= SECTION_BOOST
    if f.prefer_years and meta.get("year") in f.prefer_years:
        m *= YEAR_BOOST
    if f.prefer_doc_types and meta.get("doc_type") in f.prefer_doc_types:
        m *= DOC_TYPE_BOOST
    if f.recency:
        age = _age_days(meta, today)
        if age is not None:
            m *= 1 + RECENCY_MAX_BOOST * math.exp(-age / RECENCY_HALF_DAYS)
    return m


def apply_boost(hits: list[dict], f: QueryFilters, key: str = "score",
                today: date | None = None) -> list[dict]:
    """Nhân điểm theo ưu tiên mềm rồi sắp xếp lại (ổn định với hit cùng điểm)."""
    if not (f.prefer_sections or f.prefer_years or f.recency or f.prefer_doc_types):
        return hits
    for h in hits:
        h[key] = h.get(key, 0.0) * boost(h, f, today)
    return sorted(hits, key=lambda h: -h.get(key, 0.0))


def to_qdrant(f: QueryFilters):
    """Lọc cứng → qdrant Filter (None nếu không có). Payload: section, doc_type, year."""
    if not f.has_hard():
        return None
    from qdrant_client.models import FieldCondition, Filter, MatchAny, Range
    must = []
    if f.sections:
        must.append(FieldCondition(key="section", match=MatchAny(any=f.sections)))
    if f.doc_types:
        must.append(FieldCondition(key="doc_type", match=MatchAny(any=f.doc_types)))
    if f.year_from or f.year_to:
        must.append(FieldCondition(key="year", range=Range(gte=f.year_from, lte=f.year_to)))
    return Filter(must=must)
