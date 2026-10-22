"""Graph thực thể nối chữ ↔ ảnh/video (D20): người, sự kiện, CLB/tổ chức.

Vì sao: alt ảnh của site 85% chỉ lặp tiêu đề bài và hầu như không có chú thích riêng, nên CLIP
(text encoder tiếng Anh) + caption không đủ để trả lời "ảnh thầy X", "ảnh IT Festival 2018". Graph
gom bằng chứng rải rác (tiêu đề, thân bài, alt riêng, đoạn văn sát ảnh, OCR, trang cán bộ) thành cạnh
có trọng số, lúc truy xuất lọc thẳng theo thực thể nhắc trong câu hỏi.

Nút: tài liệu, media (ảnh/frame), video, thực thể (person | event | org). Cạnh (`entity_links`):
  about     thực thể ở tiêu đề / trang hồ sơ          (tài liệu, w=1.0)
  mentions  thực thể trong thân bài                      (tài liệu, w=0.5)
  depicts   ảnh/video thể hiện thực thể — mạnh: tên nằm trong alt riêng / đoạn sát ảnh (1.0) hoặc
            OCR (0.8) hoặc trang hồ sơ của chính người đó (0.8); yếu: ảnh thuộc bài "about" thực
            thể đó (0.6)
Khoá thực thể = chữ đã bỏ dấu (filters.fold), sự kiện bỏ năm ("IT Festival 2016/2018" → 1 chuỗi sự
kiện, năm nằm ở metadata bài). Payload Qdrant: `entities` (chunk, caption, media) + `entities_strong`
(media) để lọc thẳng; Postgres giữ graph đầy đủ.

Trích bằng luật (không model/LLM, chạy CPU, xác định — incremental cho cùng kết quả với bulk):
  người   = cụm chữ Hoa 2–5 từ bắt đầu bằng họ Việt, sau học hàm (GS/PGS/TS/ThS/NCS…) hoặc vai trò
            (thầy/cô/sinh viên/em/anh/chị…); không có tiền tố thì cần ≥ 3 từ.
  sự kiện = tên trong ngoặc (`…`, “…”) ở tiêu đề/alt + cụm bắt đầu bằng từ khoá sự kiện (hội thảo,
            cuộc thi, lễ tốt nghiệp, ngày hội, seminar…) cắt tới dấu phân cách.
  org     = "CLB …" / "Câu lạc bộ …".
Callers: loader.py, indexer.index_page, api.retrieve (Gazetteer.match).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sict_crawler.core.text import fold

KINDS = ("person", "event", "org")
W_ABOUT, W_MENTION = 1.0, 0.5
W_DEPICT_STRONG, W_DEPICT_OCR, W_DEPICT_WEAK = 1.0, 0.8, 0.6
W_DEPICT_PROFILE = 0.8    # ảnh trong trang hồ sơ (cán bộ / gương sáng) của chính người đó
STRONG_MIN = 0.8          # depicts ≥ ngưỡng này → entities_strong

# ký tự chữ Việt hoa / thường (dựng 1 lần từ unicodedata, re không có \p{Lu})
_LETTERS = [chr(c) for c in list(range(0x41, 0x5B)) + list(range(0x61, 0x7B)) + list(range(0xC0, 0x250))
            + list(range(0x1E00, 0x1F00)) if chr(c).isalpha()]
_UP = "".join(c for c in _LETTERS if c.isupper())
_LO = "".join(c for c in _LETTERS if c.islower())
_TW = f"[{_UP}][{_LO}]+"                         # 1 từ viết hoa chữ đầu
_RUN = rf"{_TW}(?:[ \t]+{_TW}){{1,4}}"           # 2–5 từ
_HONOR = r"(?:(?:GS|PGS|TSKH|TS|ThS|Th\.S|NCS|KS|BS)\s*\.?\s*,?\s*)+"
_ROLE = (r"(?:[Tt]hầy|[Cc]ô giáo|[Cc]ô|[Ss]inh viên|SV|[Ee]m|[Bb]ạn|[Aa]nh|[Cc]hị|[Đđ]ồng chí|[Ôô]ng|"
         r"[Bb]à|[Gg]iảng viên|[Cc]ựu sinh viên|[Hh]ọc viên|[Nn]ghiên cứu sinh|[Tt]ân sinh viên|"
         r"[Tt]ác giả|[Dd]iễn giả|[Kk]ỹ sư|[Tt]hạc sĩ|[Tt]iến sĩ|[Tt]rưởng khoa|[Hh]iệu trưởng)")
_P_PREFIXED = re.compile(rf"(?:{_HONOR}|\b{_ROLE}\s+)({_RUN})")
_P_BARE = re.compile(rf"(?<![{_UP}{_LO}])({_TW}(?:[ \t]+{_TW}){{2,4}})")

SURNAMES = set("""Nguyễn Trần Lê Phạm Hoàng Huỳnh Phan Vũ Võ Đặng Bùi Đỗ Hồ Ngô Dương Lý Đào Đinh Mai
Trịnh Lương Đoàn Vương Tạ Lại Chu Cao Hà Tô Lâm Phùng Kiều Thái Tăng Quách Lưu Khúc Triệu Giang Đàm
Nghiêm Âu Thân Hứa Mạc Trương Đồng Khổng Doãn Vi Lò Nông La Tống Từ Ninh Bạch Cù Diệp Kim Lục Phí
Viên Hầu Lã Lạc Tào Văn Quan Châu Đậu Khương Nhữ Tiêu Mạnh Trình Ứng Kha Phương Sầm Ma
An Lù Chảo Quyền Liêu Thào Giàng Sùng""".split())   # họ hiếm (dân tộc / ít gặp) — bộ vàng 2026-10-18 bỏ sót
# từ không thể nằm trong tên người (địa danh, tổ chức) — cắt false positive kiểu "Hà Nội", "Thái Nguyên"
_NOT_NAME = set("""Nội   Viện Đại  Nghiệp Phố  Quận Huyện Phường Đường Bộ Sở Ban Phòng Hội Đoàn
Nhà Xuất Bản Tập Hàng Thông Tin Truyền Ngày Tháng Năm Lễ Cuộc  Chương Trình Giải Kỳ Lớp
Ngành Thực Dệt May Điện Tử Cơ Khí Kinh Tế Phần Mềm Mạng Máy Tính Dữ Liệu Khởi Lần Thứ Câu Lạc
Đội Tuyển Sinh Viên Giảng Trưởng Phó Của Và Với Đến Cho Trong Dụng Thuật Toán Năng Chức Binh
Lược Hệ Thống Mô Hình Pháp Chất Được Nghệ Ty Biểu Đẳng Khóa Khoá""".split())   # "Cao Đẳng Khóa 21"
# chữ viết hoa hay đứng ngay sau tên người (chức danh): "Đặng Trọng Hợp Hiệu trưởng" → tên chạy lấn
# từ đứng trước "tên đệm + tên" cho thấy đó là tên người ("cô Cẩm Ngoan", "TS. Trọng Hợp")
ROLE_PREV = {"thay", "co", "anh", "chi", "em", "ban", "ong", "ba", "ts", "ths", "pgs", "gs", "ncs", "ks",
             "sv", "vien", "chi"}
RUNON_TAIL = {"hieu", "chu", "pho", "truong", "giam", "bi", "uy", "can", "thu", "cau", "ban", "giang",
              "dai", "dong", "ky", "email", "sdt", "dien", "khoa", "vien", "tong", "nguyen", "tran"}
# tên riêng rất phổ biến (Thành, Trung, Tâm, Công, Quốc, Gia, Ngân…) KHÔNG chặn theo từ đơn — chặn theo
# cặp từ không phải tên ("Thành Phố", "Trung Tâm", "Quốc Gia", "Công Nghệ")
# chữ vừa là tên riêng vừa là từ thường ("Mai Đăng Khoa" / "Khoa CNTT", "Nguyễn Xuân Trường" / "Trường
# Đại học"): nhận làm tên, trừ khi ngay sau nó còn chữ viết hoa (VD "… Trường Đại học") → cắt bỏ
_AMBIG_TAIL = {"Trường", "Khoa", "Học", "Thi", "Viện"}
# chữ đứng NGAY TRƯỚC cho thấy là địa chỉ / tên trường / di tích, không phải người ("đường Nguyễn Huy Tưởng",
# "Phường Lê Hồng Phong", "THPT Lê Quý Đôn", "Đền thờ Chu Văn An"); so chữ CÓ DẤU (bỏ dấu thì "trường" trùng
# "trưởng": "Hiệu trưởng Nguyễn Văn A" là người).
_PLACE_PREV = {"đường", "phố", "ngõ", "ngách", "thpt", "thcs", "tiểu", "trường", "quận", "phường", "tỉnh",
               "huyện", "xã", "số", "thờ", "đền", "chùa", "tòa", "toà", "cầu", "chợ", "lăng"}


def _place_before(text: str, start: int) -> bool:
    """Từ đứng NGAY trước vị trí `start` (chỉ cách bằng khoảng trắng) là địa danh. Số đứng trước KHÔNG tính:
    bảng bị dàn phẳng thành "… 1 Trần Tuấn Khanh …" (cột STT) thường gặp hơn số nhà."""
    m = re.search(r"([^\W_]+)[ \t]+$", text[max(0, start - 20):start])
    if not m:
        return False
    prev = m.group(1).lower()
    return prev in _PLACE_PREV
_NOT_NAME_BIGRAMS = {"thanh nien", "doan vien", "doan truong", "doan khoa", "cuoc thi", "ky thi", "hoc vien", "hoc bong", "hoc ky", "khoa cong", "khoa may",
                     "khoa mang", "khoa ky", "khoa cntt", "truong dai", "truong cong", "truong cao",
                     "ha dong", "dai loan", "nhat ban", "han quoc", "viet nhat", "ha nam", "thanh pho", "trung tam", "trung quoc", "quoc gia", "quoc te", "cong nghe",
                     "cong nghiep", "cong ty", "cong tac", "gia dinh", "tam ly", "thanh lap",
                     "thanh vien", "ngan hang", "tieu bieu", "hoc may", "khoa hoc", "dai hoc"}
# "Ông" là họ hiếm, còn "ông" (Mr.) rất phổ biến → bỏ khỏi họ; "Ứng/Chiến…" chỉ nằm trong tiêu đề viết hoa
# cụm 2 từ đầu là địa danh (tên tỉnh trùng họ + tên riêng phổ biến: "Hà Nam", "Thái Bình") hoặc tên
# riêng chủ yếu dùng làm địa danh/tổ chức ("TP. Hồ Chí Minh", "Đoàn TNCS Hồ Chí Minh")
_PLACE_PREFIX = {"ha noi", "ha nam", "ha tinh", "ha giang", "thai binh", "thai nguyen", "nam dinh",
                 "hai phong", "hai duong", "da nang", "bac ninh", "bac giang", "hung yen", "vinh phuc",
                 "phu tho", "lang son", "cao bang", "quang ninh", "ninh binh", "thanh hoa", "nghe an",
                 "ho chi minh", "ho chi", "tran hung dao", "le loi", "ly thuong kiet", "hoang mai", "dong anh",
                 "tay ho", "long bien", "bac tu liem", "nam tu liem", "cau giay", "ba dinh"}

_EVENT_HEADS = ["le tot nghiep", "le khai giang", "le trao bang", "le trao giai", "le ky niem", "le bao ve",
                "le ra mat", "le vinh danh", "le tong ket", "le ky ket", "hoi thao", "hoi nghi", "cuoc thi",
                "hoi thi", "ngay hoi", "seminar", "workshop", "toa dam", "talkshow", "chung ket",
                "festival", "giai chay", "dem nhac", "hoi trai", "tuan le", "hoi cho viec lam",
                "giai bong da", "giai thuong", "hackathon", "olympic"]
# cụm chỉ có từ khoá (≤ 3 từ) là quá chung → bỏ, trừ các chuỗi sự kiện lặp hằng năm có nghĩa riêng
_SERIES_OK = {"le tot nghiep", "le khai giang", "le trao bang", "ngay hoi viec lam", "hoi cho viec lam",
              "le tot nghiep va trao bang", "olympic tin hoc", "hoi thao viec lam", "ngay hoi tu van",
              "hoi nghi lop truong", "le bao ve do an"}
# quá chung dù dài hơn từ khoá: không phải 1 sự kiện/chuỗi sự kiện cụ thể
_GENERIC = {"hoi thao khoa hoc", "hoi nghi khoa hoc", "chung ket cuoc thi", "seminar chuyen nganh",
            "seminar khoa hoc", "hoi thao quoc te", "cuoc thi sinh vien", "hoi thao chuyen de",
            "seminar chuyen nganh khoa cntt", "workshop kickoff cuoc thi", "seminar khoa hoc voi chu de"}
_QUOTED = re.compile(r"[`“\"‘«]([^`”\"’»]{3,80})[`”\"’»]")
_CUT = re.compile(r"\s*(?:[–—:|,;()\[\]!?“”\"`«»]|\s-\s|\s+(?:tại|của|với|dành cho|cho|là|được|đã|sẽ|"
                  r"nhằm|thu hút|diễn ra|trong|tổ chức)\s|\s+năm\s+\d|\s+(?:19|20)\d{2}\b)")
_ORG = re.compile(rf"\b(?:CLB|[Cc]âu lạc bộ)\s+((?:[{_UP}{_LO}0-9&\-]+\s*){{1,5}})")
_YEARISH = re.compile(r"\b(?:19|20)\d{2}\b|\blan thu \d+\b|\bnam hoc\b|\bkhoa \d+\b")
_PROFILE_SECTIONS = {"can-bo-giang-vien", "guong-sang"}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


_TONE_MARKS = {"\u0300": "2", "\u0301": "1", "\u0309": "3", "\u0303": "4", "\u0323": "5"}
_WORD = re.compile(r"[^\W_]+")


def tone_token(word: str) -> str:
    """1 âm tiết → khoá giữ dấu, chuẩn hoá vị trí dấu thanh: chữ (giữ mũ/móc/trăng) + số thanh.
    "thủy" = "thuỷ" → "thuy3"; "hùng" → "hung2" ≠ "hưng" → "hưng"."""
    d = unicodedata.normalize("NFD", word.lower())
    tone = next((_TONE_MARKS[c] for c in d if c in _TONE_MARKS), "")
    base = unicodedata.normalize("NFC", "".join(c for c in d if c not in _TONE_MARKS))
    return base + tone


def person_key(name: str) -> str:
    """Khoá người GIỮ DẤU: bỏ dấu gộp nhầm người khác nhau (Nguyễn Văn Hùng / Hưng, Tuấn / Tuân)."""
    return " ".join(tone_token(w) for w in _WORD.findall(_nfc(name)))


def untone(key: str) -> str:
    """Khoá người → dạng bỏ dấu (khớp câu hỏi gõ không dấu)."""
    return fold(re.sub(r"(?<=\D)[1-5](?=\s|$)", "", key))


def _clean_key(s: str) -> str:
    k = _YEARISH.sub(" ", fold(s))
    k = re.sub(r"[^a-z0-9 ]+", " ", k)
    return re.sub(r"\s+", " ", k).strip()


@dataclass
class Mention:
    kind: str
    key: str
    name: str
    aliases: list[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)


def extract_persons(text: str, bare: bool = True) -> list[Mention]:
    """bare=False: chỉ nhận tên có học hàm/vai trò đứng trước (dùng cho OCR — chữ OCR nhiễu)."""
    text = _nfc(text)
    out: list[Mention] = []
    seen: set[str] = set()

    def add(run: str, min_words: int, end: int) -> None:
        words = run.split()
        # "Đoàn" vừa là họ vừa là "Đoàn TN": đứng đầu thì là họ ("Đoàn Văn Trung"), đứng giữa thì cắt
        cut = next((i for i, w in enumerate(words) if w in _NOT_NAME and not (i == 0 and w in SURNAMES)), None)
        truncated = cut is not None
        if truncated:  # "Nguyễn Văn A Trường Đại học" → cắt tại chữ không thể là tên
            words = words[:cut]
        nxt = re.match(r"[ \t]+(\w)", text[end:])
        if words and words[-1] in _AMBIG_TAIL and (truncated or (nxt and nxt.group(1).isupper())):
            words.pop()
            if len(words) < 3:
                return  # "ThS. Nguyễn Xuân Trường Đại học": mơ hồ, không đoán thành "Nguyễn Xuân"
        if len(words) < min_words or words[0] not in SURNAMES:
            return
        if len(words) == 2 and words[1] in ("Văn", "Thị"):
            return  # "Nguyễn Văn", "Lê Thị": tên đệm, tên bị cắt
        fw = fold(" ".join(words)).split()
        if any(f"{a} {b}" in _NOT_NAME_BIGRAMS for a, b in zip(fw, fw[1:])):
            return
        name = " ".join(words)
        fkey = fold(name)
        if any(fkey == p or fkey.startswith(p + " ") for p in _PLACE_PREFIX) or " viet nam" in fkey:
            return
        key = person_key(name)
        if key not in seen:
            seen.add(key)
            out.append(Mention("person", key, name))

    for m in _P_PREFIXED.finditer(text):
        add(m.group(1), 2, m.end(1))
    if bare:
        streets = catalog().streets
        for m in _P_BARE.finditer(text):
            if _place_before(text, m.start()) or person_key(m.group(1)) in streets:
                continue
            add(m.group(1), 3, m.end(1))
    return out


# từ nối không thể đứng cuối tên sự kiện ("Lễ ký kết Biên bản ghi nhớ hợp tác giữa") → cắt bỏ
_EVENT_TAIL_STOP = {"va", "cua", "cho", "ve", "giua", "lan", "nam", "ngay", "tai", "voi", "cac", "nhung", "trong",
                    "tu", "den", "de", "thu"}
# tiền tố vòng thi / loại cuộc thi: "Chung kết X", "Cuộc thi X", "X" là 1 sự kiện
_EVENT_CORE_PREFIX = ("vong chung ket", "chung ket", "cuoc thi", "hoi thi", "ky thi")


def event_key(name: str) -> str:
    """Khoá sự kiện: bỏ dấu + bỏ năm + bỏ tiền tố vòng thi ("Chung kết Cuộc thi DENSO Factory Hacks" = "DENSO
    Factory Hacks"). Chỉ bỏ khi phần còn lại ≥ 2 từ VÀ là tên riêng (có chữ hoa sau từ đầu / viết tắt / số):
    "Cuộc thi Nghiên cứu khoa học" giữ nguyên — bỏ tiền tố thì còn cụm danh từ chung khớp khắp thân bài."""
    words = _nfc(name).split()
    changed = True
    while changed:
        changed = False
        for p in _EVENT_CORE_PREFIX:
            n = len(p.split())
            rest = words[n:]
            if (_clean_key(" ".join(words[:n])) == p and len(rest) >= 2
                    and (any(w[:1].isupper() for w in rest[1:]) or any(c.isdigit() for w in rest for c in w)
                         or (len(rest[0]) > 1 and rest[0].isupper()))):
                words, changed = rest, True
    return _clean_key(" ".join(words))


def _event_name(phrase: str) -> str | None:
    name = _CUT.split(phrase, maxsplit=1)[0].strip(" .-–")
    words = name.split()
    if len(words) > 12:
        return None          # câu mô tả dài, không phải tên riêng — cắt ở N từ sinh tên cụt ("…Hệ thống thông")
    while words and fold(words[-1]) in _EVENT_TAIL_STOP:
        words.pop()
    name = " ".join(words)
    key = _clean_key(name)
    if len(key.split()) < 2 or key in _GENERIC:
        return None
    if key not in _SERIES_OK and any(key.startswith(h) and len(key.split()) - len(h.split()) <= 1
                                     for h in _EVENT_HEADS):
        return None
    return name


def extract_events(title: str) -> list[Mention]:
    """Sự kiện từ tiêu đề bài / alt riêng (thân bài nhiều cụm chung chung → không dùng)."""
    t = _nfc(title)
    out: dict[str, Mention] = {}
    for m in _QUOTED.finditer(t):
        name = m.group(1).strip()
        if not name[:1].isupper() and not name[:1].isdigit():
            continue  # cụm viết thường trong ngoặc là khẩu hiệu / tính từ ("đa năng", "bắt cóc online")
        key = event_key(name)
        words = name.split()
        if (2 <= len(words) <= 5 and words[0].title() in SURNAMES
                and not any(key.startswith(h) or f" {h}" in key for h in _EVENT_HEADS)):
            continue  # “NGUYỄN THANH BÌNH”: tên người trong ngoặc, không phải sự kiện
        if len(key) >= 5 and len(key.split()) <= 12:
            out.setdefault(key, Mention("event", key, name))
    ft = fold(t)
    if len(ft) == len(t):  # fold giữ độ dài với chuỗi NFC → cắt được trên bản gốc
        for head in _EVENT_HEADS:
            for m in re.finditer(rf"\b{head}\b", ft):
                name = _event_name(t[m.start():])
                if name:
                    key = event_key(name)
                    out.setdefault(key, Mention("event", key, name))
    return list(out.values())


_ORG_STOP = {"Trường", "Khoa", "Viện", "là", "của", "tại", "đã", "và", "với", "được", "trong", "đến",
             "cùng", "thuộc", "sẽ", "có", "đang", "Đại", "nhằm", "cho", "Được", "Để", "nhiệm", "vận",
             "thực", "hy", "ngày", "giới", "vinh", "Hội", "cũng", "để", "vớicác"}
_ACRONYM = re.compile(r"^[A-ZĐ][A-ZĐ0-9\-&]{1,}$")


def extract_orgs(text: str) -> list[Mention]:
    """CLB: khoá theo tên viết tắt nếu có ("CLB Tin học HIT" = "CLB HIT" → "clb hit"), alias tên
    đầy đủ; cần ít nhất 1 từ tên riêng (viết hoa) để bỏ "câu lạc bộ học thuật của ..."."""
    out: dict[str, Mention] = {}
    for m in _ORG.finditer(_nfc(text)):
        words = []
        for w in _CUT.split(m.group(1), maxsplit=1)[0].split()[:5]:
            if w in _ORG_STOP:
                break
            words.append(w)
        if not words or not words[0][0].isupper():
            continue
        acr = next((w for w in words if _ACRONYM.match(w)), None)
        full = "clb " + _clean_key(" ".join(words))
        key = "clb " + _clean_key(acr) if acr else full
        if len(key) < 6:
            continue
        mt = out.setdefault(key, Mention("org", key, "CLB " + " ".join(words)))
        if full != key:
            mt.aliases = sorted({*getattr(mt, "aliases", []), full})
    return list(out.values())


# ------------------------------------------------------------------ danh mục chuẩn CLB / sự kiện (dữ liệu)
_CATALOG_PATH = Path(__file__).with_name("graph_catalog.yaml")


@dataclass
class Catalog:
    """graph_catalog.yaml → thực thể chuẩn (attrs: catalog=True, acronyms) + khoá bị chặn + tên danh nhân dùng làm
    tên đường (person_key; không nhận là người khi đứng trần)."""
    entities: list
    blocked: set
    streets: set = field(default_factory=set)

    def canon(self, m: "Mention") -> "Mention | None":
        """Ứng viên luật → thực thể chuẩn nếu khoá / tên chứa 1 cách gọi của nó (cụm dài nhất thắng); khoá bị
        chặn → None; không liên quan → giữ nguyên."""
        if m.kind == "person":
            return m
        if (m.kind, m.key) in self.blocked:
            return None
        toks = m.key.split()
        raw = _WORD.findall(_nfc(m.name))
        best, best_len = None, 0
        for e in self.entities:
            if e.kind != m.kind:
                continue
            for a in (e.key, *e.aliases):
                at = a.split()
                n = len(at)
                if n > best_len and (n >= 2 or len(a) >= 5) and any(toks[i:i + n] == at
                                                                    for i in range(len(toks) - n + 1)):
                    best, best_len = e, n
            for acr in e.attrs.get("acronyms", []):
                at = _WORD.findall(acr)
                if len(at) > best_len and any(raw[i:i + len(at)] == at for i in range(len(raw) - len(at) + 1)):
                    best, best_len = e, len(at)
        if best is None or best.key == m.key:
            return m
        return Mention(m.kind, best.key, m.name, sorted({*m.aliases, m.key}), m.attrs)


_catalog: Catalog | None = None


def load_catalog(path: Path | None = None) -> Catalog:
    import yaml
    data = yaml.safe_load(Path(path or _CATALOG_PATH).read_text(encoding="utf-8")) or {}
    ents = []
    for kind in ("org", "event"):
        for it in data.get(kind) or []:
            aliases = sorted({event_key(a) if kind == "event" else _clean_key(a) for a in it.get("aliases", [])}
                             - {it["key"]})
            ents.append(Entity(kind=kind, key=it["key"], name=it["name"], aliases=aliases, doc_count=0,
                               attrs={"catalog": True, "acronyms": list(it.get("acronyms", []))}))
    blocked = {(k, key) for k, keys in (data.get("block") or {}).items() for key in keys}
    return Catalog(ents, blocked, {person_key(n) for n in data.get("street_names") or []})


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = load_catalog()
    return _catalog


def set_catalog(c: Catalog | None) -> None:
    """Tests: thay danh mục (None = nạp lại từ file lần gọi sau)."""
    global _catalog
    _catalog = c


# ------------------------------------------------------------------ danh bạ + khớp
@dataclass
class Entity:
    kind: str
    key: str
    name: str
    aliases: list[str] = field(default_factory=list)
    doc_count: int = 0
    id: int | None = None
    attrs: dict = field(default_factory=dict)   # person: {"staff": True, "honorific", "profile_url"}

    def __hash__(self) -> int:
        return hash((self.kind, self.key))

    @property
    def fkey(self) -> str:
        """Dạng bỏ dấu của khoá (người: khoá giữ dấu → bỏ số thanh + dấu)."""
        return untone(self.key) if self.kind == "person" else self.key

    def falias(self, a: str) -> str:
        return untone(a) if self.kind == "person" else a


class Gazetteer:
    """Khớp thực thể trong 1 đoạn chữ / câu hỏi: n-gram token đã bỏ dấu, tại mỗi vị trí lấy cụm dài
    nhất, cho phép chồng lấn giữa các vị trí. Người (khoá giữ dấu): đoạn chữ CÓ DẤU phải khớp đúng
    dấu (Hùng ≠ Hưng); đoạn KHÔNG DẤU nhận khi chỉ 1 ứng viên, hoặc 1 người là cán bộ trong danh mục,
    hoặc 1 người chiếm ≥ 2/3 số bài. "Tên đệm + tên" ("cô Cẩm Ngoan") theo cùng luật."""

    MAX_N = 12

    def __init__(self, entities: list[Entity]):
        self.entities = {(e.kind, e.key): e for e in entities}
        # khoá bỏ dấu → ứng viên (+ khoá giữ dấu tương ứng để so khi đoạn chữ có dấu)
        # src: key | alias | short (alias 1 chữ: viết tắt) | last2 (tên đệm + tên)
        self.index: dict[tuple[str, ...], list[tuple[Entity, tuple[str, ...] | None, str]]] = defaultdict(list)

        def put(e: Entity, tone_toks: tuple[str, ...] | None, ftoks: tuple[str, ...], src: str) -> None:
            if all(x[0] is not e or x[1] != tone_toks or x[2] != src for x in self.index[ftoks]):
                self.index[ftoks].append((e, tone_toks, src))

        for e in entities:
            if len(e.key.split()) >= 2 or (len(e.key) >= 5 and e.kind != "person"):
                put(e, tuple(e.key.split()) if e.kind == "person" else None, tuple(e.fkey.split()), "key")
        for e in entities:  # alias (tên đầy đủ / viết tắt có chọn lọc) sau khoá chính
            for a in e.aliases:
                if len(a) >= 3:
                    ft = tuple(e.falias(a).split())
                    put(e, tuple(a.split()) if e.kind == "person" else None, ft,
                        "short" if len(ft) == 1 else "alias")
            if e.kind == "person" and len(e.key.split()) >= 3:
                put(e, tuple(e.key.split()[-2:]), tuple(e.fkey.split()[-2:]), "last2")
            for acr in e.attrs.get("acronyms", []):   # danh mục chuẩn: viết tắt khớp đúng hoa/thường trong bài
                put(e, None, tuple(fold(w) for w in _WORD.findall(acr)), "acr:" + " ".join(_WORD.findall(acr)))
        self.index = dict(self.index)

    def __len__(self) -> int:
        return len(self.entities)

    def _lens(self) -> dict[str, list[int]]:
        if not hasattr(self, "_by_first"):
            by: dict[str, set] = defaultdict(set)
            for toks in self.index:
                by[toks[0]].add(len(toks))
            self._by_first = {k: sorted(v, reverse=True) for k, v in by.items()}
        return self._by_first

    @staticmethod
    def _pick(cands, span_tone: tuple[str, ...], accented: bool) -> Entity | None:
        cands = [(e, t) for e, t, *_ in cands]
        others = list({id(e): e for e, t in cands if t is None}.values())
        if others:  # viết tắt dùng chung nhiều thực thể ("hit": CLB HIT, HIT Open Day) → mơ hồ, bỏ
            return others[0] if len(others) == 1 else None
        if accented:  # có dấu → phải khớp đúng dấu
            exact = list({id(e): e for e, t in cands if t == span_tone}.values())
            return exact[0] if len(exact) == 1 else None
        es = sorted({id(e): e for e, _ in cands}.values(), key=lambda e: -e.doc_count)
        staff = [e for e in es if e.attrs.get("staff")]
        if len(es) == 1:
            return es[0]
        if len(staff) == 1:
            return staff[0]
        return es[0] if es[0].doc_count >= 2 * sum(e.doc_count for e in es[1:]) else None

    def lookup(self, kind: str, key: str) -> Entity | None:
        """Khoá (người: giữ dấu) → thực thể qua khoá chính hoặc alias, đúng loại."""
        e = self.entities.get((kind, key))
        if e is not None:
            return e
        ftoks = tuple((untone(key) if kind == "person" else key).split())
        cands = [(x, t) for x, t, _ in self.index.get(ftoks, []) if x.kind == kind]
        if not cands:
            return None
        return self._pick(cands, tuple(key.split()), kind == "person")

    def match(self, text: str, query: bool = False) -> list[Entity]:
        """query=False (văn bản bài viết): tên người phải viết hoa; alias "tên đệm + tên" chỉ nhận khi
        đứng ngay sau thầy/cô/anh/chị/học hàm ("người chiến thắng" ≠ Nguyễn Chiến Thắng, "Tập đoàn Hồng Hải"
        ≠ Hà Hồng Hải); bỏ alias 1 chữ (viết tắt "STP", "Procon" trong bài thường chỉ chương trình khác).
        query=True (câu hỏi người dùng gõ thường, gõ tắt): cho phép cả hai."""
        text = _nfc(text)
        spans = list(_WORD.finditer(text))
        raw = [m.group(0) for m in spans]
        words = [w.lower() for w in raw]
        ftoks = [fold(w) for w in words]
        lens = self._lens()
        out: list[Entity] = []
        person_end = -1  # tên người nằm trọn trong tên người đã khớp ("Thị Kim" trong "… Kim Anh") → bỏ
        for i in range(len(ftoks)):
            for n in lens.get(ftoks[i], ()):
                if i + n > len(ftoks):
                    continue
                cands = self.index.get(tuple(ftoks[i:i + n]))
                if cands and not query:
                    # văn bản bài viết: tên người phải VIẾT HOA như tên riêng ("Cao đẳng khóa 21", địa chỉ
                    # "trần duy hưng" không phải người); "tên đệm + tên" chỉ nhận ngay sau thầy/cô/TS…
                    # ("Tập đoàn Hồng Hải", "Việt Nam Hải Phòng", "ĐH Quốc Gia Hà Nội" viết hoa nhưng không phải
                    # người). Đo trên bộ vàng 150 bài (2026-10-18): precision người 0,90 → 0,97, recall giữ 0,91.
                    cap = all(w[:1].isupper() for w in raw[i:i + n])
                    role = i > 0 and ftoks[i - 1] in ROLE_PREV
                    span_raw = " ".join(raw[i:i + n])
                    # viết tắt đứng riêng: "HIT" nhưng không "HIT_Titans", "HaUI.HIT-H2K" (tên đội thi)
                    a, b = spans[i].start(), spans[i + n - 1].end()
                    alone = text[a - 1:a] not in ("_", ".", "-") and text[b:b + 1] not in ("_", "-")
                    cands = [c for c in cands if c[2] != "short" and (c[2] != "last2" or role)
                             and (c[0].kind != "person" or cap)
                             and (not c[2].startswith("acr:") or (c[2][4:] == span_raw and alone))]
                if not cands:
                    continue
                span = words[i:i + n]
                accented = any(w != f for w, f in zip(span, ftoks[i:i + n]))
                e = self._pick(cands, tuple(tone_token(w) for w in span), accented)
                if e is None:
                    continue
                if e.kind == "person" and i + n <= person_end:
                    break
                if e.kind == "person" and not query and (_place_before(text, spans[i].start()) or (
                        e.key in catalog().streets and not (i > 0 and ftoks[i - 1] in ROLE_PREV))):
                    break   # "Phường Lê Hồng Phong", "THPT Lê Quý Đôn": địa danh, không phải người
                if e.kind == "person":
                    person_end = max(person_end, i + n)
                if e not in out:
                    out.append(e)
                break  # cụm dài nhất tại vị trí này; vị trí sau vẫn xét (chồng lấn)
        return out


def doc_mentions(doc: dict, body: str, alts: list[str] = (), contexts: list[str] = (),
                 ocrs: list[str] = ()) -> list[Mention]:
    """Ứng viên thực thể của 1 tài liệu. Sự kiện chỉ lấy từ tiêu đề + alt riêng (thân bài/đoạn sát
    ảnh có nhiều cụm chung chung kiểu "seminar có sự tham gia ..."); người/CLB lấy mọi nguồn."""
    title = doc.get("title", "")
    every = " \n ".join([title, body, *alts, *contexts])
    ms = extract_persons(every) + extract_orgs(every)
    have = {m.key for m in ms}
    for t in ocrs:  # OCR nhiễu → chỉ tên có học hàm/vai trò đứng trước (giấy chứng nhận, poster)
        ms += [m for m in extract_persons(t, bare=False) if m.key not in have]
    if doc.get("doc_type") in ("article", "page"):
        for t in (title, *alts):
            ms += extract_events(t)
    return list({(m.kind, m.key): m for m in ms}.values())


# viết tắt quá phổ biến để làm alias riêng của 1 thực thể
_ACR_STOP = {"haui", "sict", "cntt", "fit", "it", "ai", "clb", "dhcnhn", "vn", "sv", "tp", "ict", "cn",
             "ts", "ths", "pgs", "gs", "ncs", "ktx", "iot", "dhcn", "tnts", "tncs", "ubnd", "dh", "hn",
             "it&t", "cntt&tt", "dhqg", "bch", "lch", "tt", "ptit", "hust", "neu", "ceo", "cto"}
_ACR_TOKEN = re.compile(r"^(?:[A-ZĐ][A-ZĐ0-9]{2,}|[a-z]+[A-Z][A-Za-z]+|[A-Z][a-z]+[A-Z][A-Za-z]*)$")


def _merge_prefixes(ents: list[Entity]) -> list[Entity]:
    """Sự kiện/CLB: khoá dài có tiền tố (theo token) là 1 thực thể khác đã gặp ở ≥ 2 bài → gộp vào
    khoá ngắn, khoá dài thành alias ("Hội thảo việc làm và tuyển dụng trực tiếp Cán…" → "Hội thảo
    việc làm"). Cụm do cắt câu sinh ra không thành thực thể rác."""
    by_kind: dict[str, dict[str, Entity]] = defaultdict(dict)
    for e in ents:
        by_kind[e.kind][e.key] = e
    out = []
    for kind, m in by_kind.items():
        # người: cụm chữ Hoa chạy lấn sang từ sau ("Đặng Trọng Hợp Hiệu trưởng", "Chu Thị Quyên Chu
        # Thị …") → gộp vào tên ngắn ≥ 3 từ đã gặp ít nhất ngang số bài
        min_n = 3 if kind == "person" else 2

        def ok(parent: Entity, child: Entity) -> bool:
            if kind != "person":
                return parent.doc_count >= 2
            return parent.doc_count >= child.doc_count and runon_tail(child.key, parent.key)

        keys = sorted(m, key=lambda k: len(k.split()))
        merged: dict[str, Entity] = {}
        for k in keys:
            toks = k.split()
            parent = next((merged[" ".join(toks[:n])] for n in range(len(toks) - 1, min_n - 1, -1)
                           if " ".join(toks[:n]) in merged and ok(merged[" ".join(toks[:n])], m[k])),
                          None)
            e = m[k]
            if parent is None or e.attrs.get("catalog"):
                merged[k] = e
            else:
                parent.aliases = sorted({*parent.aliases, k, *e.aliases})
                parent.doc_count += e.doc_count
        out += merged.values()
    return out


def _merge_contained_events(ents: list[Entity]) -> list[Entity]:
    """Sự kiện có khoá chứa trọn khoá của 1 sự kiện khác đã gặp ở ≥ 2 bài (≥ 3 từ, không phải cụm chung) →
    cùng sự kiện, gộp vào khoá ngắn: "Học sinh, sinh viên với ý tưởng khởi nghiệp" ⊃ "Sinh viên với ý tưởng
    khởi nghiệp". Trước đây 2 biến thể thành 2 thực thể rồi nối nhau bằng quan hệ."""
    evs = sorted((e for e in ents if e.kind == "event"), key=lambda e: len(e.key.split()))
    parents: list[Entity] = []
    gone: set[int] = set()
    for e in evs:
        toks = e.key.split()
        par = next((p for p in reversed(parents)
                    if any(toks[i:i + len(p.key.split())] == p.key.split()
                           for i in range(len(toks) - len(p.key.split()) + 1))), None)
        if par is not None and not e.attrs.get("catalog"):
            par.aliases = sorted({*par.aliases, e.key, *e.aliases})
            par.doc_count += e.doc_count
            gone.add(id(e))
        elif len(toks) >= 3 and e.doc_count >= 2 and e.key not in _GENERIC:
            parents.append(e)
    return [e for e in ents if id(e) not in gone]


def runon_tail(child_key: str, parent_key: str) -> bool:
    """Người: phần thừa của tên dài là chức danh / họ của tên kế tiếp ("… Hợp Hiệu", "Chu Thị Quyên Chu
    Thị") → cùng người. Phần thừa là tên riêng ("Nguyễn Thị Kim" + "Anh") → KHÁC người, không gộp."""
    extra = child_key.split()[len(parent_key.split()):]
    if not extra:
        return False
    first = untone(extra[0])
    # "… Hợp Hiệu": chức danh. "Chu Thị Quyên Chu Thị": tên kế tiếp (≥ 2 chữ, bắt đầu bằng họ).
    # 1 chữ thừa trùng họ ("Nguyễn Thị Thu Hà", "… Thùy Dương") là tên riêng → khác người
    return first in RUNON_TAIL or (len(extra) >= 2 and any(fold(sn) == first for sn in SURNAMES))


def _acronyms(name: str) -> list[str]:
    letters = [c for c in name if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        return []  # tên viết HOA toàn bộ ("DÁM THAY ĐỔI") — từ hoa không phải viết tắt
    return [fold(t) for t in re.findall(r"[^\s`“”\"'(),:–\-]+", name)
            if _ACR_TOKEN.match(t) and fold(t) not in _ACR_STOP]


def _merge_acronyms(ents: list[Entity]) -> list[Entity]:
    """Tên viết tắt / tên riêng (iStar, ICISN, HIT) → alias 1 từ. Nhiều thực thể cùng kind chung 1
    viết tắt ("Hội thảo quốc tế ICISN", "ICISN 2025 …") → gộp vào thực thể nhiều bài nhất."""
    owners: dict[tuple[str, str], list[Entity]] = defaultdict(list)
    for e in ents:
        if e.kind != "person":
            for a in dict.fromkeys(_acronyms(e.name)):
                owners[(e.kind, a)].append(e)
    gone: set[int] = set()
    for (kind, a), es in sorted(owners.items(), key=lambda kv: -len(kv[1])):
        es = [e for e in {id(e): e for e in es}.values() if id(e) not in gone]
        if not es:
            continue
        top = max(es, key=lambda e: (bool(e.attrs.get("catalog")), e.doc_count))   # mục chuẩn luôn giữ
        for e in es:
            if e is not top:
                top.aliases = sorted({*top.aliases, e.key, *e.aliases})
                top.doc_count += e.doc_count
                gone.add(id(e))
        top.aliases = sorted({*top.aliases, a} | ({"clb " + a} if kind == "org" else set()))
    return [e for e in ents if id(e) not in gone]


def build_gazetteer(mentions_by_doc: list[list[Mention]], cat: Catalog | None = None) -> Gazetteer:
    """Pha 1 (bulk): gộp ứng viên của mọi tài liệu → danh bạ (tên hiển thị = dạng gặp nhiều nhất). Danh mục
    chuẩn (graph_catalog.yaml): ứng viên chứa 1 cách gọi của mục → gộp vào mục; mục luôn có trong danh bạ (khớp
    được trong thân bài kể cả khi luật không trích ra từ tiêu đề); tên hiển thị = tên chuẩn."""
    cat = catalog() if cat is None else cat
    mentions_by_doc = [[c for m in ms if (c := cat.canon(m)) is not None] for ms in mentions_by_doc]
    names: dict[tuple[str, str], Counter] = defaultdict(Counter)
    aliases: dict[tuple[str, str], set] = defaultdict(set)
    attrs: dict[tuple[str, str], dict] = defaultdict(dict)
    df: Counter = Counter()
    for ms in mentions_by_doc:
        for m in ms:
            k = (m.kind, m.key)
            names[k][m.name] += 1
            aliases[k].update(m.aliases)
            attrs[k].update(m.attrs)
            df[k] += 1
    ents = [Entity(kind=k, key=key, name=c.most_common(1)[0][0], aliases=sorted(aliases[(k, key)]),
                   doc_count=df[(k, key)], attrs=dict(attrs[(k, key)])) for (k, key), c in names.items()]
    have = {(e.kind, e.key): e for e in ents}
    for ce in cat.entities:
        e = have.get((ce.kind, ce.key))
        if e is None:
            ents.append(Entity(ce.kind, ce.key, ce.name, list(ce.aliases), 0, attrs=dict(ce.attrs)))
        else:
            e.name, e.attrs = ce.name, {**e.attrs, **ce.attrs}
            e.aliases = sorted({*e.aliases, *ce.aliases})
    ents = _merge_contained_events(_merge_prefixes(_merge_acronyms(ents)))
    return Gazetteer(ents)


# ------------------------------------------------------------------ alias đã duyệt + resolve incremental
def apply_aliases(ms: list[Mention], alias_map: dict[tuple[str, str], str | None]) -> list[Mention]:
    """Alias/chặn lưu trong Postgres (entity_aliases: duyệt tay, danh mục cán bộ) áp lên ứng viên
    TRƯỚC khi gộp: (kind, key) → key đích, None = chặn (VD "Học máy" không phải sự kiện). Danh sách
    chặn thành dữ liệu duyệt được, không phải sửa code."""
    out = []
    for m in ms:
        k = (m.kind, m.key)
        if k in alias_map:
            target = alias_map[k]
            if target is None:
                continue
            m = Mention(m.kind, target, m.name, [*m.aliases, m.key], m.attrs)
        out.append(m)
    return out


def resolve_mention(m: Mention, gaz: Gazetteer) -> Entity | None:
    """Incremental: ứng viên mới → thực thể đã có, theo cùng luật gộp của bulk (khoá/alias, tên chạy
    lấn ≥ 3 từ, tiền tố sự kiện/CLB đã gặp ở ≥ 2 bài, tên viết tắt). None = thực thể mới."""
    m = catalog().canon(m) or m
    toks = m.key.split()
    hit = gaz.lookup(m.kind, m.key)
    if hit is not None and (m.kind != "person" or len(toks) >= 3 or hit.key == m.key):
        return hit
    min_n = 3 if m.kind == "person" else 2
    for n in range(len(toks) - 1, min_n - 1, -1):
        parent = gaz.entities.get((m.kind, " ".join(toks[:n])))
        if parent is not None and (parent.doc_count >= 2 if m.kind != "person"
                                   else runon_tail(m.key, parent.key)):
            return parent
    if m.kind != "person":
        for a in _acronyms(m.name):
            owner = gaz.lookup(m.kind, a)
            if owner is not None:
                return owner
    return None


def near_duplicates(e: Entity, candidates, min_ratio: float = 0.88) -> list[Entity]:
    """Người có tên gần giống (cùng tên riêng, số từ ±1) → chỉ ĐỀ XUẤT vào hàng chờ duyệt, KHÔNG
    tự gộp: "Nguyễn Thị Hương Lan" vs "Nguyễn Hương Lan" có thể là 1 người, nhưng "… Hương Lan" vs
    "… Hương Ly" là 2 người (AC-C1, C7). candidates: list hoặc NearDupIndex (theo tên riêng)."""
    from difflib import SequenceMatcher
    toks = e.key.split()
    pool = candidates.get(untone(toks[-1]), []) if isinstance(candidates, dict) else candidates
    out = []
    for c in pool:
        if c.key == e.key or abs(len(c.key.split()) - len(toks)) > 1:
            continue
        sm = SequenceMatcher(None, e.key, c.key)
        if sm.real_quick_ratio() >= min_ratio and sm.quick_ratio() >= min_ratio and sm.ratio() >= min_ratio:
            out.append(c)
    return out


def near_dup_index(pool: list[Entity]) -> dict[str, list[Entity]]:
    """Bộ đối chiếu theo tên riêng (từ cuối) → so trong nhóm, không so mọi cặp."""
    idx: dict[str, list[Entity]] = defaultdict(list)
    for c in pool:
        idx[untone(c.key.split()[-1])].append(c)
    return dict(idx)


_PROFILE_PREFIX = re.compile(rf"^\s*(?:{_HONOR}|(?:Thạc sĩ|Tiến sĩ|Phó Giáo sư|Giáo sư)\.?\s*)+", re.I)


def staff_from_profile(doc: dict) -> Mention | None:
    """Trang hồ sơ cán bộ (/vn/html/can-bo-giang-vien/<slug>): tiêu đề "PGS.TS. Nguyễn Thị Kim Sơn"
    → người trong DANH MỤC CHUẨN (AC-C7). Tiêu đề hồ sơ là tên chuẩn: KHÔNG qua bộ lọc của extract_persons
    (họ hiếm "An Văn Minh", "Đoàn Văn Trung" bị chặn nhầm), chỉ bỏ học hàm và sửa hoa/thường ("Tuyết MaI").
    Đối chiếu slug URL (không dấu) để chắc tiêu đề là tên người: slug bị cắt cụt ("…-xuan-hoa" cho Hoàng)
    vẫn nhận khi phần còn lại khớp."""
    from urllib.parse import urlparse
    parts = [p for p in urlparse(doc.get("url", "")).path.split("/") if p]
    if doc.get("section") != "can-bo-giang-vien" or len(parts) < 4:
        return None
    title = "".join(c for c in _nfc(doc.get("title", "")) if unicodedata.category(c) != "Cf")  # bỏ \u200b
    hm = re.match(rf"\s*({_HONOR})", title)
    rest = _PROFILE_PREFIX.sub("", title).strip(" .,-–")
    words = [w[:1].upper() + w[1:].lower() for w in rest.split()]
    if not 2 <= len(words) <= 5 or not all(_WORD.fullmatch(w) for w in words):
        return None
    slug = [t.rstrip("0123456789") for t in parts[3].lower().split("-") if t not in ("ths", "ts", "pgs", "gs", "tskh", "ncs", "ks")]
    fw = fold(" ".join(words)).split()
    if len(slug) != len(fw) or any(not a.startswith(b) for a, b in zip(fw, slug)):
        return None
    name = " ".join(words)
    return Mention("person", person_key(name), name, [],
                   {"staff": True, "honorific": re.sub(r"\s+", "", hm.group(1)) if hm else "",
                    "profile_url": doc.get("url")})


# ------------------------------------------------------------------ cạnh cho 1 tài liệu
@dataclass
class MediaRef:
    key: str                 # media sha256 | video_key
    kind: str                # image | video
    alt: str = ""
    context: str = ""
    ocr: str = ""


@dataclass
class DocGraph:
    about: list[Entity] = field(default_factory=list)
    mentions: list[Entity] = field(default_factory=list)
    # (kind, key) → các chunk_id nhắc tên (payload `entities` của chunk sinh từ đây)
    mention_chunks: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # media key → [(entity, weight, source)]
    depicts: dict[str, list[tuple[Entity, float, str]]] = field(default_factory=dict)
    # (media key | "", kind, key) → đoạn trích chứa tên (AC-C6: tên phải kèm bằng chứng)
    evidence: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def entity_keys(self) -> list[str]:
        return ekeys(self.about + self.mentions)

    def chunk_keys(self, chunk_id: str) -> list[str]:
        """Thực thể của 1 chunk = thực thể tiêu đề bài + thực thể nhắc trong chính chunk đó."""
        return ekeys([*self.about, *(e for e in self.mentions
                                     if chunk_id in self.mention_chunks.get((e.kind, e.key), ()))])

    def media_keys(self, key: str, strong_only: bool = False) -> list[str]:
        return ekeys([e for e, w, _ in self.depicts.get(key, []) if not strong_only or w >= STRONG_MIN])


def ekey(e: Entity) -> str:
    """Khoá payload Qdrant: "<kind>:<key>"."""
    return f"{e.kind}:{e.key}"


def ekeys(es) -> list[str]:
    return list(dict.fromkeys(ekey(e) for e in es))


def link_document(doc: dict, chunks, media: list[MediaRef], gaz: Gazetteer) -> DocGraph:
    """Pha 2: khớp danh bạ trên tiêu đề / từng chunk / từng ảnh → cạnh. chunks: [(chunk_id, text)]
    (hoặc 1 chuỗi thân bài). Trang hồ sơ (cán bộ, gương sáng) và bài có tiêu đề nêu đúng 1 người:
    người đó là chủ thể của ảnh trong bài."""
    if isinstance(chunks, str):
        chunks = [("", chunks)]
    g = DocGraph()
    title = doc.get("title", "")
    g.about = gaz.match(title)
    for e in g.about:
        g.evidence[("", e.kind, e.key)] = title[:300]
    for cid, text in chunks:
        f = None
        for e in gaz.match(text):
            if e in g.about:
                continue
            k = (e.kind, e.key)
            if k not in g.mention_chunks:
                g.mentions.append(e)
                g.mention_chunks[k] = []
            if cid not in g.mention_chunks[k]:
                g.mention_chunks[k].append(cid)
                f = f or _Folded(text)
                g.evidence[(f"chunk:{cid}", e.kind, e.key)] = _snippet(f, e)
    persons_about = [e for e in g.about if e.kind == "person"]
    subject_ok = doc.get("section") in _PROFILE_SECTIONS or len(persons_about) == 1
    for m in media:
        links: dict[tuple[str, str], tuple[Entity, float, str]] = {}

        def add(e: Entity, w: float, src: str, text: str) -> None:
            k = (e.kind, e.key)
            if k not in links or links[k][1] < w:
                links[k] = (e, w, src)
                g.evidence[(m.key, e.kind, e.key)] = _snippet(text, e)

        own_alt = m.alt if m.alt and fold(m.alt) != fold(title) else ""
        for src, text, w in (("alt", own_alt, W_DEPICT_STRONG), ("context", m.context, W_DEPICT_STRONG),
                             ("ocr", m.ocr, W_DEPICT_OCR)):
            if text:
                for e in gaz.match(text):
                    add(e, w, src, text)
        profile = doc.get("section") in _PROFILE_SECTIONS
        for e in g.about:
            if e.kind != "person" or subject_ok:
                add(e, W_DEPICT_PROFILE if profile and e.kind == "person" else W_DEPICT_WEAK, "doc",
                    title)
        g.depicts[m.key] = list(links.values())
    return g


class _Folded:
    """Văn bản NFC + bản bỏ dấu tính 1 lần (thân bài đính kèm có thể dài hàng trăm KB)."""

    def __init__(self, text: str):
        self.t = _nfc(text)
        self.ft = fold(self.t)


def _snippet(text, e: Entity, width: int = 300) -> str:
    """Đoạn quanh lần xuất hiện đầu của tên (so trên bản bỏ dấu, cắt trên bản gốc NFC)."""
    f = text if isinstance(text, _Folded) else _Folded(text)
    t, ft = f.t, f.ft
    pos = -1
    for a in (e.fkey, *(e.falias(x) for x in e.aliases)):
        pos = ft.find(a)
        if pos >= 0:
            break
    if pos < 0 or len(ft) != len(t):
        return re.sub(r"\s+", " ", t[:width]).strip()
    start = max(0, pos - width // 3)
    return re.sub(r"\s+", " ", t[start:start + width]).strip()


def link_rows(g: DocGraph, document_id: int, media_ids: dict[str, int],
              video_ids: dict[str, int], entity_ids: dict[tuple[str, str], int]) -> list[dict]:
    """DocGraph → dòng entity_links (Postgres)."""
    rows = []
    for e in g.about:
        rows.append({"entity_id": entity_ids.get((e.kind, e.key)), "document_id": document_id,
                     "media_id": None, "video_id": None, "chunk_id": None, "rel": "about",
                     "weight": W_ABOUT, "source": "doc",
                     "evidence": g.evidence.get(("", e.kind, e.key), "")[:300]})
    for e in g.mentions:  # 1 dòng / chunk nhắc tên
        for cid in g.mention_chunks.get((e.kind, e.key), [""]):
            rows.append({"entity_id": entity_ids.get((e.kind, e.key)), "document_id": document_id,
                         "media_id": None, "video_id": None, "chunk_id": cid or None,
                         "rel": "mentions", "weight": W_MENTION, "source": "doc",
                         "evidence": g.evidence.get((f"chunk:{cid}", e.kind, e.key), "")[:300]})
    for key, links in g.depicts.items():
        for e, w, src in links:
            rows.append({"entity_id": entity_ids.get((e.kind, e.key)), "document_id": document_id,
                         "media_id": media_ids.get(key), "video_id": video_ids.get(key), "chunk_id": None,
                         "rel": "depicts", "weight": w, "source": src,
                         "evidence": g.evidence.get((key, e.kind, e.key), "")[:300]})
    return [r for r in rows if r["entity_id"] is not None
            and (r["rel"] != "depicts" or r["media_id"] or r["video_id"])]


# ------------------------------------------------------------------ cạnh thực thể ↔ thực thể
R_SAME_MEDIA, R_BOTH_TITLE, R_TITLE_BODY = 1.0, 0.8, 0.5
_KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
MAX_PER_MEDIA = 10        # ảnh nêu > N thực thể (danh sách, poster) → không ghép cặp
MAX_MENTIONS = 60         # thân bài nêu > N thực thể (danh sách SV) → chỉ ghép N đầu với tiêu đề


REL_DOC_DAMP = 0.5   # 1 bài làm chứng tối đa 0,5 (1 ảnh chung không còn đẩy thẳng lên 1,0); 2 bài 0,75; 3 bài 0,875


def relations_from_links(links: list[dict], kinds: dict[int, str], exclude: set[int] = frozenset()) -> list[dict]:
    """entity_links → cạnh thực thể ↔ thực thể. links: {entity_id, document_id, media_id,
    video_id, rel, weight}; kinds: entity_id → kind. Bằng chứng trong 1 bài (lấy mức cao nhất):
      cùng được nêu tên trên 1 ảnh/video (depicts ≥ STRONG_MIN)  1.0
      cùng ở tiêu đề (about)                                     0.8
      1 bên tiêu đề, 1 bên thân bài / ảnh                         0.5
    Hai bên chỉ cùng ở thân bài → bỏ (danh sách SV trong file đính kèm sinh hàng triệu cặp vô nghĩa).
    Nhiều bài: weight = 1 − Π(1 − REL_DOC_DAMP·w_bài) (noisy-OR giảm chấn: độ tin tăng theo SỐ BÀI).
    exclude: thực thể không tạo quan hệ (sự kiện / CLB chỉ gặp ở 1 bài, chưa có trong danh mục chuẩn — phần lớn
    là cụm luật trích nhầm)."""
    by_doc: dict[int, dict] = defaultdict(lambda: {"about": set(), "other": [], "media": defaultdict(set),
                                                   "roles": defaultdict(set)})
    for r in links:
        if r["entity_id"] in exclude:
            continue
        d = by_doc[r["document_id"]]
        e = r["entity_id"]
        d["roles"][e].update(r.get("roles") or [])
        if r["rel"] == "about":
            d["about"].add(e)
        elif r["rel"] == "mentions":
            d["other"].append(e)
        elif r["rel"] == "depicts":
            if r["weight"] >= STRONG_MIN:
                d["media"][(r.get("media_id"), r.get("video_id"))].add(e)
                d["other"].append(e)
    per_pair: dict[tuple[int, int], dict] = defaultdict(
        lambda: {"docs": {}, "media": 0, "src": set(), "dst": set()})

    def put(a: int, b: int, doc: int, w: float) -> None:
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        cur = per_pair[k]["docs"].get(doc, 0.0)
        per_pair[k]["docs"][doc] = max(cur, w)
        roles = by_doc[doc]["roles"]   # vai trò của mỗi bên trong bài chung (LLM, giai đoạn C)
        per_pair[k]["src"].update(roles.get(k[0], ()))
        per_pair[k]["dst"].update(roles.get(k[1], ()))

    for doc, d in by_doc.items():
        about = sorted(d["about"])
        for m_ents in d["media"].values():
            if 2 <= len(m_ents) <= MAX_PER_MEDIA:
                ms = sorted(m_ents)
                for i, a in enumerate(ms):
                    for b in ms[i + 1:]:
                        put(a, b, doc, R_SAME_MEDIA)
                        per_pair[(a, b)]["media"] += 1
        for i, a in enumerate(about):
            for b in about[i + 1:]:
                put(a, b, doc, R_BOTH_TITLE)
        others = list(dict.fromkeys(e for e in d["other"] if e not in d["about"]))[:MAX_MENTIONS]
        for a in about:
            for b in others:
                put(a, b, doc, R_TITLE_BODY)
    rows = []
    for (a, b), v in per_pair.items():
        docs = sorted(v["docs"].items(), key=lambda x: -x[1])
        miss = 1.0
        for _, w in docs:
            miss *= 1 - REL_DOC_DAMP * w
        ka, kb = kinds.get(a, "?"), kinds.get(b, "?")
        rel = "-".join(sorted((ka, kb), key=lambda k: _KIND_ORDER.get(k, 9)))
        rows.append({"src_id": a, "dst_id": b, "rel": rel, "weight": round(1 - miss, 4),
                     "doc_count": len(docs), "media_count": v["media"],
                     "doc_ids": [d for d, _ in docs[:10]],
                     "roles": {"src": sorted(v["src"]), "dst": sorted(v["dst"])}})
    return rows


@dataclass
class Related:
    entity: Entity
    weight: float
    doc_count: int
    media_count: int
    roles: list[str] = field(default_factory=list)   # vai trò của thực thể GỐC liên quan tới entity này


# ------------------------------------------------------------------ danh bạ dùng lúc truy xuất
_current: Gazetteer | None = None


def current() -> Gazetteer | None:
    """Danh bạ API đang dùng (nạp từ Postgres lúc khởi động + làm mới định kỳ). None → tắt graph."""
    return _current


def set_current(g: Gazetteer | None) -> None:
    global _current
    _current = g


_related: dict[int, list[Related]] = {}


def set_relations(rows: list[tuple], gaz: Gazetteer | None) -> None:
    """rows: (src_id, dst_id, weight, doc_count, media_count[, roles]) → kề 2 chiều, mạnh nhất trước."""
    global _related
    by_id = {e.id: e for e in (gaz.entities.values() if gaz else []) if e.id is not None}
    adj: dict[int, list[Related]] = defaultdict(list)
    for row in rows:
        a, b, w, dc, mc = row[:5]
        roles = (row[5] if len(row) > 5 else None) or {}
        if a in by_id and b in by_id:
            adj[a].append(Related(by_id[b], w, dc, mc, list(roles.get("src", []))))
            adj[b].append(Related(by_id[a], w, dc, mc, list(roles.get("dst", []))))
    for v in adj.values():
        # nhiều bài làm chứng trước (1 poster chung đã đẩy weight lên 1.0), rồi tới trọng số
        v.sort(key=lambda r: (-r.doc_count, -r.media_count, -r.weight))
    _related = dict(adj)


def related(e: Entity, limit: int = 8, kind: str | None = None) -> list[Related]:
    if e.id is None:
        return []
    return [r for r in _related.get(e.id, []) if kind is None or r.entity.kind == kind][:limit]


_KIND_VI = {"person": "người", "event": "sự kiện", "org": "CLB/tổ chức"}


def facts(ents: list[Entity], per_kind: int = 4) -> list[str]:
    """Dữ kiện graph cho ngữ cảnh LLM: thực thể nêu trong câu hỏi + thực thể liên quan mạnh nhất
    theo từng loại (sự kiện, CLB, người; kèm số bài làm chứng) — giúp trả lời câu tổng hợp
    ("thầy X tham gia sự kiện nào", "CLB Y có ai")."""
    out = []
    for e in ents:
        groups = []
        for kind in ("event", "org", "person"):
            rel = related(e, per_kind, kind=kind)
            if rel:
                groups.append(f"{_KIND_VI[kind]}: " + "; ".join(
                    f"{r.entity.name} ({r.doc_count} bài"
                    + (f"; vai trò: {', '.join(r.roles)}" if r.roles else "") + ")" for r in rel))
        if groups:
            out.append(f"{e.name} ({_KIND_VI.get(e.kind, e.kind)}) liên quan — " + " | ".join(groups))
    return out


def match_question(*texts: str) -> list[Entity]:
    g = _current
    if g is None:
        return []
    out: list[Entity] = []
    for t in texts:
        for e in g.match(t or "", query=True):
            if e not in out:
                out.append(e)
    return out
