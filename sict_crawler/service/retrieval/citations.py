"""Trích dẫn bắt buộc (D24, AC-S8). Hàm thuần — không gọi mạng, dễ kiểm thử.

Ngữ cảnh gửi LLM đánh số nguồn [1]…[n] (api.build_context: nguồn chữ trước, media sau). LLM được yêu cầu kết
mỗi ý bằng [n]; `enforce` kiểm lại phía server: bỏ [n] không tồn tại, và nếu câu trả lời không có [n] hợp lệ
nào thì ghép dòng "Nguồn:" lấy từ các nguồn đứng đầu. Mỗi nguồn có `cite` (nhãn dựng sẵn: "Tên file — trang N"
cho PDF/PPTX, "Tiêu đề › Mục" cho trang web) và `url`.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from sict_crawler.service.index.indexing import path_str

_MARK = re.compile(r"\[(\d{1,3})\]")
# Một số model (gpt-oss) trích dẫn bằng ngoặc toàn khổ 【3】 / ［3］: coi như [3]
# (cả dạng kèm số dòng 【2†L1-L4】)
_WIDE_MARK = re.compile(r"[【［](\d{1,3})(?:†[^】］\n]{0,24})?[】］]")
_PLACEHOLDER = re.compile(r"[ \t]*\[n\]")        # model chép nguyên chỗ giữ chỗ "[n]" của prompt
# "[Nguồn: 6, 7]" (gpt-oss, golden_v2 g061/g065 2026-11-04): coi như [6][7]
_NGUON_MARK = re.compile(r"\[\s*[Nn]guồn\s*:?\s*(\d{1,3}(?:\s*[,;]?[\s\u00a0\u202f]*\d{1,3})*)\s*\]")


# gpt-oss còn chấm câu bằng dấu toàn khổ ("… [4]。"), tiếng Việt không dùng: đổi về dấu ASCII
_WIDE_PUNCT = str.maketrans("。，：；！？（）", ".,:;!?()")
_WIDE_PUNCT_GLUED = re.compile(r"([。，：；！？])(?=\w)")   # "a。Bb" → "a. Bb"


def _normalize(answer: str) -> str:
    answer = _NGUON_MARK.sub(lambda m: "".join(f"[{n}]" for n in re.findall(r"\d{1,3}", m.group(1))),
                             _PLACEHOLDER.sub("", answer or ""))
    answer = _WIDE_PUNCT_GLUED.sub(r"\1 ", _WIDE_MARK.sub(r"[\1]", answer))
    return answer.translate(_WIDE_PUNCT)
FOOTER_SOURCES = 3      # số nguồn ghép vào dòng "Nguồn:" khi LLM không trích dẫn


def _file_name(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", 1)[-1])


def make_cite(title: str, url: str, kind: str, page: int | None = None,
              heading_path: list[str] | None = None) -> str:
    """Nhãn trích dẫn người đọc kiểm chứng được: file → tên + số trang; trang web → tiêu đề + mục."""
    name = (title or _file_name(url) or url).strip()
    if kind == "file":
        return f"{name} — trang {page}" if page else name
    return f"{name} › {path_str(heading_path)}" if heading_path else name


def used(answer: str, n_sources: int) -> list[int]:
    """[n] hợp lệ (1 ≤ n ≤ số nguồn) theo thứ tự xuất hiện, không lặp."""
    seen: list[int] = []
    for m in _MARK.finditer(_normalize(answer)):
        n = int(m.group(1))
        if 1 <= n <= n_sources and n not in seen:
            seen.append(n)
    return seen


def entry(n: int, s) -> dict:
    """Mục trích dẫn gửi cho client (s: api.Source)."""
    return {"n": n, "url": s.url, "title": s.title, "kind": s.kind, "page": s.page,
            "heading_path": s.heading_path, "cite": s.cite}


def index(sources: list) -> list[dict]:
    """Toàn bộ nguồn đã đánh số (đúng thứ tự trong ngữ cảnh) — cho /query."""
    return [entry(i, s) for i, s in enumerate(sources, 1)]


_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"https?://[^\s<>()\[\]`'\"]*[^\s<>()\[\]`'\".,;:!?]")


def _url_key(u: str) -> str:
    return re.sub(r"^https?://", "", u.strip().lower()).rstrip("/")


def unlink_unverified(answer: str, sources: list) -> str:
    """Link trong câu trả lời phải có trong nguồn văn bản (url, trang chứa, nội dung) hoặc là url của một nguồn
    ảnh/video. Link chỉ có trong chữ OCR/phụ đề (máy đọc, có thể sai) hay do model tự viết → bỏ dạng link:
    [chữ](url) còn chữ, URL trần thành `url` (UI không biến thành link). 2026-11-04: OCR đọc "haui" thành "hqui",
    câu trả lời dẫn link tới tên miền sai."""
    known = " ".join(_url_key(x) for s in sources for x in (s.url, getattr(s, "parent_url", None) or "") if x)
    known += " " + " ".join((s.content or s.excerpt or "").lower() for s in sources if s.kind in ("text", "file"))
    known = re.sub(r"https?://", "", known)

    def ok(u: str) -> bool:
        return _url_key(u) in known

    answer = _MD_LINK.sub(lambda m: m.group(0) if ok(m.group(2)) else m.group(1), answer)
    out, last = [], 0
    for m in _URL.finditer(answer):
        if ok(m.group(0)) or answer[max(0, m.start() - 2):m.start()] == "](" or answer[m.start() - 1:m.start()] == "`":
            continue
        out += [answer[last:m.start()], f"`{m.group(0)}`"]
        last = m.end()
    return "".join(out) + answer[last:]


_RUN = re.compile(r"\[\d{1,3}\](?:[\s,\u00a0\u202f]*\[\d{1,3}\])+")


def dedupe_runs(answer: str) -> str:
    """Cụm [n] liền nhau (cách bởi dấu phẩy / khoảng trắng) → mỗi số một lần, viết liền "[9][10]".
    gpt-oss viết "[9] , [10] [9][10]" (2026-11-04)."""
    def fix(m):
        ns = list(dict.fromkeys(_MARK.findall(m.group(0))))
        return "".join(f"[{n}]" for n in ns)
    return _RUN.sub(fix, answer)


def enforce(answer: str, sources: list) -> tuple[str, list[dict]]:
    """→ (câu trả lời đã chuẩn hoá, danh sách trích dẫn được dùng). Không có nguồn (ngữ cảnh rỗng) thì không
    ép trích dẫn. [n] ngoài khoảng bị bỏ; không còn [n] hợp lệ → ghép "Nguồn: [1] … — url" cho các nguồn đầu."""
    if not sources:
        return answer, []
    n_src = len(sources)
    answer = dedupe_runs(unlink_unverified(answer, sources))
    answer = _MARK.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_src else "", _normalize(answer))
    ns = used(answer, n_src)
    if not ns:
        ns = list(range(1, min(FOOTER_SOURCES, n_src) + 1))
        lines = [f"[{n}] {sources[n - 1].cite} — {sources[n - 1].url}" for n in ns]
        answer = answer.rstrip() + "\n\nNguồn:\n" + "\n".join(lines)
    return answer, [entry(n, sources[n - 1]) for n in ns]
