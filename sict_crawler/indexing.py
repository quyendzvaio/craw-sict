"""Index spec v2 — metadata + làm sạch markdown + chunk (chữ/bảng) + trích text file đính kèm.

Một nguồn sự thật cho cả bulk compute (compute.py, máy GPU) lẫn incremental về sau, để index
không lệch quy tắc giữa 2 đường. Hàm thuần, không gọi DB/vector store.

Quy tắc (AC-H, AC-E2, plan.md "index spec v2"):
  - metadata: section (slug URL), section_label (breadcrumb), doc_type (article|page|listing|
    attachment), published_at (ISO, +07:00) + year — phục vụ lọc/ưu tiên theo thời gian & mục.
  - markdown: bỏ URL trong link (giữ chữ), bỏ **/__ ; bảng do markdownify sinh KHÔNG nối thêm lần 2.
  - chunk: header ngữ cảnh "[tiêu đề | mục | dd/mm/yyyy]" ở đầu mọi chunk; bảng cắt theo nhóm
    dòng, lặp lại dòng tiêu đề cột; body_hash (không gồm header) để lọc boilerplate xuyên bài.
  - đính kèm: pdf (pdftotext), docx/xlsx/pptx (zip XML, stdlib), doc/xls cũ (catdoc/xls2csv);
    ppt cũ → bỏ qua.

Callers: compute.py; (sắp tới) main._process_page cho incremental.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from .chunker import _SENT_RE, _window_split, chunk_text

SPEC_VERSION = "v2"
VN_TZ = timezone(timedelta(hours=7))

# slug → nhãn hiển thị khi trang không có breadcrumb
SECTION_LABELS = {
    "tin-tuc": "Tin tức", "thong-bao": "Thông báo", "tuyen-sinh": "Tuyển sinh",
    "nghien-cuu-khoa-hoc": "Nghiên cứu khoa học", "thu-vien-anh": "Thư viện ảnh",
    "ke-hoach": "Kế hoạch", "tien-do": "Tiến độ", "tuyen-dung": "Tuyển dụng",
    "guong-sang": "Gương sáng", "cau-lac-bo": "Câu lạc bộ", "gioi-thieu": "Giới thiệu",
    "bo-may-to-chuc": "Bộ máy tổ chức", "can-bo-giang-vien": "Cán bộ giảng viên",
    "khoa": "Khoa", "phong-trung-tam": "Phòng/Trung tâm", "quy-che": "Quy chế",
    "tuyen-sinh-dai-hoc": "Tuyển sinh đại học", "tuyen-sinh-sau-dai-hoc": "Tuyển sinh sau đại học",
    "tin-khcn": "Tin khoa học công nghệ", "sinh-vien-nckh": "Sinh viên nghiên cứu khoa học",
    "danh-muc-cong-trinh-da-cong-bo": "Công trình đã công bố", "de-tai-du-an": "Đề tài, dự án",
    "dao-tao-ngan-han": "Đào tạo ngắn hạn", "dia-chi-lien-he": "Liên hệ",
    "co-so-vat-chat": "Cơ sở vật chất", "chien-luoc-phat-trien": "Chiến lược phát triển",
    "home": "Trang chủ",
}

_ARTICLE = re.compile(r"^/vn/[a-z0-9-]+/[^/]+/\d+/?$")
_LISTING = re.compile(r"^/vn/?$|^/vn/(?!html/)[a-z0-9-]+(/\d+)?/?$")
_DATETIME = re.compile(r"(?:(\d{1,2}):(\d{2})\s+)?(\d{1,2})/(\d{1,2})/(\d{4})")
_TITLE_SUFFIX = re.compile(r"\s*(?:-\s*Trường Công nghệ thông tin.*|\|\|.*)$", re.I)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|(\s*:?-{3,}:?\s*\|)+\s*$")


@dataclass
class DocMeta:
    doc_id: str
    url: str
    title: str
    section: str
    section_label: str
    doc_type: str            # article | page | listing | attachment
    published_at: str | None = None   # ISO 8601 +07:00
    year: int | None = None
    parent_url: str | None = None     # attachment → trang chứa link

    def header(self) -> str:
        parts = [self.title]
        if self.section_label:
            parts.append(self.section_label)
        if self.published_at:
            d = datetime.fromisoformat(self.published_at)
            parts.append(d.strftime("%d/%m/%Y"))
        return " | ".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return asdict(self)

    def chunk_meta(self) -> dict:
        """Phần metadata gắn vào mọi chunk của tài liệu."""
        return {k: getattr(self, k) for k in ("doc_id", "url", "title", "section", "section_label",
                                              "doc_type", "published_at", "year", "parent_url")}


@dataclass
class ChunkRec:
    idx: int
    text: str            # có header ngữ cảnh — đây là chuỗi đem embed
    heading: str
    kind: str            # text | table
    body_hash: str       # hash phần thân (không header) — lọc boilerplate xuyên bài
    meta: dict = field(default_factory=dict)
    heading_path: list[str] = field(default_factory=list)    # D23: đường dẫn heading của chunk (rỗng = chưa có cấu trúc)
    block_range: tuple[int, int] | None = None               # D23: [block đầu, block cuối] trong documents.structure
    page: int | None = None                                  # D24: trang (từ 1) trong file PDF/PPTX; None = không có trang


def doc_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def section_of(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2 or parts[0] != "vn":
        return "home"
    if parts[1] == "html":
        return parts[2] if len(parts) > 2 else "html"
    return parts[1]


def doc_type_of(url: str) -> str:
    path = urlparse(url).path
    if _ARTICLE.match(path):
        return "article"
    if _LISTING.match(path):
        return "listing"
    return "page"


def parse_published(text: str) -> str | None:
    """'Thứ Sáu, 10:31 11/09/2026' → '2026-09-11T10:31:00+07:00'. Không hợp lệ → None."""
    m = _DATETIME.search(text or "")
    if not m:
        return None
    hh, mm, d, mo, y = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=VN_TZ)
    except ValueError:
        return None
    return dt.isoformat()


def clean_title(title: str) -> str:
    return _TITLE_SUFFIX.sub("", title or "").strip()


def section_label_of(section: str, breadcrumb: str = "") -> str:
    if section in SECTION_LABELS:
        return SECTION_LABELS[section]
    return breadcrumb.strip().capitalize() if breadcrumb else section


def build_meta(url: str, title: str, published_text: str = "", breadcrumb: str = "",
               doc_type: str | None = None, parent_url: str | None = None) -> DocMeta:
    sec = section_of(parent_url or url)
    pub = parse_published(published_text)
    return DocMeta(
        doc_id=doc_id(url), url=url, title=clean_title(title) or url,
        section=sec, section_label=section_label_of(sec, breadcrumb),
        doc_type=doc_type or doc_type_of(url), published_at=pub,
        year=int(pub[:4]) if pub else None, parent_url=parent_url)


def clean_markdown(md: str) -> str:
    md = _MD_LINK.sub(r"\1", md or "")
    md = md.replace("**", "").replace("__", "")
    lines = []
    for line in md.splitlines():
        line = re.sub(r"[ \t]+", " ", line).rstrip()
        if line.strip() in ("*", "-", "|", "#"):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_blocks(md: str) -> list[tuple[str, str]]:
    """Tách markdown thành [('text'|'table', nội dung)] theo thứ tự xuất hiện."""
    blocks: list[tuple[str, list[str]]] = []
    for line in md.splitlines():
        kind = "table" if _TABLE_LINE.match(line) else "text"
        if blocks and blocks[-1][0] == kind:
            blocks[-1][1].append(line)
        else:
            blocks.append((kind, [line]))
    return [(k, "\n".join(ls).strip()) for k, ls in blocks if "\n".join(ls).strip()]


def _row_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def chunk_table(table_md: str, max_tokens: int) -> list[str]:
    """Bảng → các đoạn ≤ max_tokens, mỗi đoạn lặp lại dòng tiêu đề cột. Bỏ dòng rỗng."""
    lines = [ln for ln in table_md.splitlines() if ln.strip()]
    if not lines:
        return []
    header: list[str] = []
    body = lines
    if len(lines) >= 2 and _TABLE_SEP.match(lines[1]):
        header, body = lines[:2], lines[2:]
    body = [ln for ln in body
            if not _TABLE_SEP.match(ln) and any(_row_cells(ln))]
    if not body:
        body, header = [ln for ln in header if not _TABLE_SEP.match(ln)], []
    budget = max(50, max_tokens - _tokens("\n".join(header)))
    out, cur = [], []
    for ln in body:
        if cur and _tokens("\n".join(cur + [ln])) > budget:
            out.append("\n".join(header + cur))
            cur = []
        cur.append(ln)
    if cur:
        out.append("\n".join(header + cur))
    if any(_tokens(p) > 2 * max_tokens for p in out):
        return _chunk_table_compact(header, body, max_tokens)
    return out


def _compact_row(line: str) -> list[str]:
    return [c for c in _row_cells(line) if c]


def _chunk_table_compact(header: list[str], body: list[str], max_tokens: int) -> list[str]:
    """Bảng dàn trang bằng ô gộp (2026-10-21: 1 bài 2019 có dòng 389 cột → mỗi đoạn ~12.000 ký tự, vượt giới hạn
    2.048 token của dịch vụ embed tự host): bỏ ô rỗng + dòng `---`, không lặp tiêu đề dài hơn max_tokens/4 (đưa vào
    thân), dòng quá dài cắt theo nhóm ô, ô quá dài cắt theo từ. Chỉ chạy khi cách chia thường ra đoạn > 2×max_tokens
    → mọi bảng khác giữ nguyên từng byte (ID chunk / vector cũ còn dùng)."""
    fmt = lambda cells: "| " + " | ".join(cells) + " |"        # noqa: E731
    head_cells = _compact_row(header[0]) if header else []
    head = fmt(head_cells) if head_cells else ""
    rows = [_compact_row(ln) for ln in body]
    if head and _tokens(head) > max_tokens // 4:
        rows.insert(0, head_cells)
        head = ""
    budget = max(50, max_tokens - _tokens(head))
    lines: list[str] = []
    for cells in rows:
        if not cells:
            continue
        group: list[str] = []
        for cell in cells:
            while _tokens(cell) > budget:                       # 1 ô quá dài: cắt theo từ
                words, part = cell.split(), []
                while words and _tokens(" ".join(part + words[:1])) <= budget - 2:
                    part.append(words.pop(0))
                part = part or [words.pop(0)]
                if group:
                    lines.append(fmt(group))
                    group = []
                lines.append(fmt([" ".join(part)]))
                cell = " ".join(words)
            if not cell:
                continue
            if group and _tokens(fmt(group + [cell])) > budget:
                lines.append(fmt(group))
                group = []
            group.append(cell)
        if group:
            lines.append(fmt(group))
    out, cur = [], []
    for ln in lines:
        if cur and _tokens("\n".join(cur + [ln])) > budget:
            out.append("\n".join(([head] if head else []) + cur))
            cur = []
        cur.append(ln)
    if cur:
        out.append("\n".join(([head] if head else []) + cur))
    return out


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _norm_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode()).hexdigest()


def build_chunks(meta: DocMeta, markdown: str, min_tokens: int = 200, max_tokens: int = 500,
                 overlap_ratio: float = 0.1, semantic: bool = True) -> list[ChunkRec]:
    """markdown đã clean → ChunkRec. Chữ dùng chunker hiện có (semantic tùy chọn), bảng riêng."""
    header = meta.header()
    md_meta = meta.chunk_meta()
    out: list[ChunkRec] = []
    text_parts: list[str] = []

    def flush_text():
        body = "\n\n".join(text_parts).strip()
        text_parts.clear()
        if not body:
            return
        for c in chunk_text(body, header, min_tokens, max_tokens, overlap_ratio, semantic):
            raw = c.text.removeprefix(f"[{header}]").lstrip()
            if c.heading:
                raw = raw.removeprefix(f"[{c.heading}]").lstrip()
            out.append(ChunkRec(len(out), c.text, c.heading, "text", _norm_hash(raw), md_meta))

    for kind, block in split_blocks(markdown):
        if kind == "text":
            text_parts.append(block)
            continue
        flush_text()
        for piece in chunk_table(block, max_tokens):
            out.append(ChunkRec(len(out), f"[{header}] [Bảng] {piece}", "Bảng", "table",
                                _norm_hash(piece), md_meta))
    flush_text()
    return out


# ---------------- D23: cấu trúc trong trang → chunk theo block ----------------
_MEDIA_BLOCKS = ("image", "video", "attachment")


def outline(blocks: list[dict]) -> list[dict]:
    """Phác thảo cấu trúc trang để lưu (documents.structure): loại block, cấp + chữ heading, URL media/file,
    số dòng bảng. Không lặp lại toàn văn (chữ đã nằm trong chunk)."""
    out = []
    for b in blocks:
        o: dict = {"i": b["idx"], "t": b["type"]}
        if b["type"] == "heading":
            o["l"], o["x"] = b["level"], b["text"][:200]
        elif b["type"] in _MEDIA_BLOCKS:
            o["u"] = b["attrs"].get("url", "")
        elif b["type"] == "table":
            o["r"] = b["attrs"].get("rows", 0)
        out.append(o)
    return out


def hash_struct(rows: list[dict]) -> str:
    """sha1 của outline (kết quả `outline(blocks)`). Đổi khi thêm/bớt/đổi thứ tự heading, bảng, ảnh, video,
    file — những thứ hash_v2 (chỉ markdown sạch, không có URL ảnh/file) không thấy."""
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def path_str(path: list[str]) -> str:
    """Đường dẫn heading dạng hiển thị: 'Mục A › Mục A.1'."""
    return " › ".join(path)


def block_index(structure: list[dict] | None) -> dict[str, tuple[int, list[str]]]:
    """URL ảnh/video/file → (idx block đầu tiên chứa nó, heading_path tại đó), dựng từ outline đã lưu."""
    out: dict[str, tuple[int, list[str]]] = {}
    stack: list[tuple[int, str]] = []
    for o in structure or []:
        if o["t"] == "heading":
            while stack and stack[-1][0] >= o["l"]:
                stack.pop()
            stack.append((o["l"], o["x"]))
        elif o["t"] in _MEDIA_BLOCKS and o.get("u") and o["u"] not in out:
            out[o["u"]] = (o["i"], [h for _, h in stack])
    return out


def _table_md(text: str) -> str:
    rows = [ln.split(" | ") for ln in text.splitlines() if ln.strip()]
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    fmt = lambda r: "| " + " | ".join(r + [""] * (w - len(r))) + " |"      # noqa: E731
    return "\n".join([fmt(rows[0]), "| " + " | ".join(["---"] * w) + " |", *[fmt(r) for r in rows[1:]]])


def build_chunks_structured(meta: DocMeta, blocks: list[dict], min_tokens: int = 200, max_tokens: int = 500,
                            overlap_ratio: float = 0.1) -> list[ChunkRec]:
    """Chunk dựng từ dãy block (D23). Chữ liền nhau gộp tới max_tokens, ngắt ở ranh giới heading khi đã đủ
    min_tokens; đoạn quá ngân sách cắt theo câu (không embed). Bảng riêng, lặp dòng tiêu đề cột, và mang
    heading của MỤC chứa nó (cả chữ sau bảng giữ mục). Chunk mang `heading_path` (phần chung của các block
    trong chunk) và `block_range`. Ảnh/video/file không thành chữ: vị trí của chúng nằm ở documents.structure."""
    header = meta.header()
    md_meta = meta.chunk_meta()
    out: list[ChunkRec] = []
    buf: list[tuple[dict, str]] = []
    buf_tokens = 0

    def prefix(path: list[str]) -> str:
        return f"[{header}]" + (f" [{path_str(path)}]" if path else "")

    def flush():
        nonlocal buf_tokens
        items, buf_tokens = buf[:], 0
        buf.clear()
        if not items or all(b["type"] == "heading" for b, _ in items):
            return                                    # heading trơ trọi: đã có trong đường dẫn của block sau nó
        body = "\n\n".join(t for _, t in items)
        path = list(os.path.commonprefix([b["heading_path"] for b, _ in items]))
        rng = (items[0][0]["idx"], items[-1][0]["idx"])
        pieces = [body]
        if _tokens(body) > max_tokens:
            pieces = _window_split([x for x in _SENT_RE.split(body) if x.strip()], max_tokens, overlap_ratio) or [body]
        for piece in pieces:
            out.append(ChunkRec(len(out), f"{prefix(path)} {piece}", (path[-1] if path else "")[:512], "text",
                                _norm_hash(piece), md_meta, path, rng))

    for b in blocks:
        t = b["type"]
        if t in _MEDIA_BLOCKS:
            continue
        if t == "table":
            flush()                                   # heading trơ trọi trước bảng bị bỏ (đã có trong đường dẫn)
            for piece in chunk_table(_table_md(b["text"]), max_tokens):
                out.append(ChunkRec(len(out), f"{prefix(b['heading_path'])} [Bảng] {piece}", "Bảng", "table",
                                    _norm_hash(piece), md_meta, b["heading_path"], (b["idx"], b["idx"])))
            continue
        line = f"{'#' * b['level']} {b['text']}" if t == "heading" else b["text"]
        if t == "heading" and buf_tokens >= min_tokens:
            flush()
        elif buf and buf_tokens + _tokens(line) > max_tokens:
            flush()
        buf.append((b, line))
        buf_tokens += _tokens(line)
    flush()
    return out


def build_chunks_paged(meta: DocMeta, pages: list[tuple[int | None, str]], min_tokens: int = 200,
                       max_tokens: int = 500, overlap_ratio: float = 0.1, semantic: bool = False
                       ) -> list[ChunkRec]:
    """File nhiều trang (PDF/PPTX, D24): chunk từng trang riêng — không chunk nào vắt qua ranh giới trang —
    mỗi chunk mang `page` và dấu `[trang N]` sau header để câu hỏi "trang N" khớp được. `idx` liền mạch
    qua các trang (id chunk ổn định theo (doc, idx))."""
    out: list[ChunkRec] = []
    header = f"[{meta.header()}]"
    for page, text in pages:
        for c in build_chunks(meta, text, min_tokens, max_tokens, overlap_ratio, semantic):
            if page is not None and c.text.startswith(header):
                c.text = f"{header} [trang {page}]{c.text[len(header):]}"
            c.idx, c.page = len(out), page
            out.append(c)
    return out


# ---------------- file đính kèm ----------------
def _zip_parts(path: Path, members: list[str], tag: str) -> list[str]:
    """Chữ từng thành viên zip (đã sắp theo tên) — mỗi phần tử là các đoạn `tag` nối bằng xuống dòng."""
    texts = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if any(re.fullmatch(m, n) for m in members)]
        for n in sorted(names):
            root = ElementTree.fromstring(z.read(n))
            paras = []
            for p in root.iter():
                if p.tag.endswith(tag):
                    t = "".join(x.text or "" for x in p.iter() if x.tag.endswith("}t"))
                    if t.strip():
                        paras.append(t.strip())
            texts.append("\n".join(paras))
    return texts


def _zip_text(path: Path, members: list[str], tag: str) -> str:
    return "\n\n".join(t for t in _zip_parts(path, members, tag) if t)


def _pptx_slides(path: Path) -> list[str]:
    """Chữ từng slide theo thứ tự số (slide2 trước slide10)."""
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    return [t for n in names for t in _zip_parts(path, [re.escape(n)], "}p")]


def _xlsx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(x.text or "" for x in si.iter() if x.tag.endswith("}t")))
        rows_out = []
        for n in sorted(x for x in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x)):
            root = ElementTree.fromstring(z.read(n))
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                cells = []
                for c in row:
                    v = next((x.text for x in c if x.tag.endswith("}v")), None)
                    if v is None:
                        v = "".join(x.text or "" for x in c.iter() if x.tag.endswith("}t"))
                    elif c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    cells.append((v or "").strip())
                if any(cells):
                    rows_out.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows_out)


def _xls_text(path: Path, timeout: int) -> str:
    """xls (Excel 97-2003, nhị phân) qua `xls2csv` (gói catdoc) → dòng bảng "| a | b |"."""
    import csv
    import io
    r = subprocess.run(["xls2csv", "-d", "utf-8", "-c", ",", "-q", "2", str(path)],
                       capture_output=True, timeout=timeout)
    rows = []
    for sheet in r.stdout.decode("utf-8", "ignore").split("\f"):
        for cells in csv.reader(io.StringIO(sheet)):
            cells = [c.strip() for c in cells]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        rows.append("")
    return "\n".join(rows)


def sniff_format(path: Path) -> str:
    """Định dạng thật theo chữ ký byte — site SICT đặt sai đuôi (snapshot 2026-10-12: 133 ".xls" là
    xlsx, 19 ".doc" là docx). Zip OOXML → xem thư mục bên trong; OLE2/khác → tin đuôi file."""
    ext = path.suffix.lower().lstrip(".")
    try:
        head = path.read_bytes()[:8]
    except OSError:
        return ext
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return ext
        for prefix, fmt in (("word/", "docx"), ("xl/", "xlsx"), ("ppt/", "pptx")):
            if any(n.startswith(prefix) for n in names):
                return fmt
    return ext


def ocr_pdf_pages(path: Path, max_pages: int = 60, dpi: int = 200, workers: int | None = None,
                  timeout: int = 120) -> list[str]:
    """PDF scan (không có lớp text) → pdftoppm từng trang → tesseract vie+eng, song song theo trang.
    Trả danh sách chữ theo trang (phần tử i = trang i+1). 447 trang (52 PDF scan của snapshot
    2026-10-12) ≈ vài phút trên 8 nhân CPU."""
    import os
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory(prefix="sict_ocrpdf_") as tmp:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-l", str(max_pages),
                        str(path), f"{tmp}/p"], capture_output=True, timeout=timeout * 5)
        pages = sorted(Path(tmp).glob("p*.png"))

        # mỗi tesseract tự mở nhiều luồng OpenMP → chạy song song nhiều tiến trình sẽ quá tải CPU
        # (đo: 26 trang > 150 s, timeout). 1 luồng/tiến trình, song song theo trang.
        env = {**os.environ, "OMP_THREAD_LIMIT": "1"}

        def one(png: Path) -> str:
            try:
                r = subprocess.run(["tesseract", str(png), "-", "-l", "vie+eng", "--psm", "3"],
                                   capture_output=True, timeout=timeout * 3, env=env)
            except subprocess.TimeoutExpired:
                return ""  # 1 trang kẹt không bỏ cả file
            return r.stdout.decode("utf-8", "ignore")

        n = workers or max(1, (os.cpu_count() or 2) - 1)
        with ThreadPoolExecutor(n) as pool:
            return list(pool.map(one, pages))


def ocr_pdf(path: Path, **kw) -> str:
    return "\n\n".join(ocr_pdf_pages(path, **kw))


def _clean_page(text: str) -> str:
    # pdftotext -layout: cột bảng cách nhau bằng khoảng trắng rộng → giữ ranh giới cột thành " | " trước khi
    # gộp khoảng trắng (không thì tên ở 2 cột liền nhau dính thành 1 người)
    text = re.sub(r"(?<=\S)[ \t]{3,}(?=\S)", " | ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_file_pages(path: Path, timeout: int = 120, ocr_scanned: bool = False
                      ) -> tuple[list[tuple[int | None, str]], str]:
    """→ ([(trang, chữ)], status). Trang đánh số từ 1 (PDF: theo ngắt trang `\\f` của pdftotext hoặc theo
    trang OCR; PPTX: theo slide); docx/xlsx/doc/xls không có trang thật → [(None, chữ)]. Trang trống giữ
    số thứ tự nhưng không có trong danh sách. status như extract_file_text.

    doc/xls (Office 97-2003) cần binary `catdoc`/`xls2csv` (gói apt catdoc, có trong image worker);
    thiếu binary → status error:FileNotFoundError (không crash compute)."""
    ext = sniff_format(path)
    pages: list[tuple[int | None, str]]
    try:
        if ext == "doc":
            r = subprocess.run(["catdoc", "-d", "utf-8", "-w", str(path)],
                               capture_output=True, timeout=timeout)
            pages = [(None, r.stdout.decode("utf-8", "ignore"))]
        elif ext == "xls":
            pages = [(None, _xls_text(path, timeout))]
        elif ext == "pdf":
            r = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
                               capture_output=True, timeout=timeout)
            parts = r.stdout.decode("utf-8", "ignore").split("\f")
            if parts and not parts[-1].strip():
                parts.pop()                                   # pdftotext kết thúc mỗi trang bằng \f
            pages = [(i, t) for i, t in enumerate(parts, 1)]
        elif ext == "docx":
            pages = [(None, _zip_text(path, [r"word/document\.xml"], "}p"))]
        elif ext == "pptx":
            pages = [(i, t) for i, t in enumerate(_pptx_slides(path), 1)]
        elif ext == "xlsx":
            pages = [(None, _xlsx_text(path))]
        else:
            return [], "unsupported"
    except Exception as e:  # noqa: BLE001 — file hỏng/định dạng lạ
        return [], f"error:{type(e).__name__}"
    status = "ok"
    if ext == "pdf" and len("".join(t for _, t in pages).strip()) < 50 and ocr_scanned:
        try:
            pages, status = [(i, t) for i, t in enumerate(ocr_pdf_pages(path, timeout=timeout), 1)], "ok_ocr"
        except Exception as e:  # noqa: BLE001 — thiếu binary / file hỏng
            return [], f"error:{type(e).__name__}"
    pages = [(n, c) for n, t in pages if (c := _clean_page(t))]
    if sum(len(t) for _, t in pages) < 50:
        return [], "empty"
    return pages, status


def extract_file_text(path: Path, timeout: int = 120, ocr_scanned: bool = False
                      ) -> tuple[str, str]:
    """→ (text, status): chữ của cả file (các trang nối bằng dòng trống). Xem extract_file_pages."""
    pages, status = extract_file_pages(path, timeout, ocr_scanned)
    return "\n\n".join(t for _, t in pages), status
