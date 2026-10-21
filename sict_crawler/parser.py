"""Parse HTML → markdown text, tables, image URLs, video URLs.

Callers: crawler.py, main.py.
Schemas: returns ParsedPage(title, markdown, tables[list[dict]], image_urls, video_urls, links).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify as md


@dataclass
class ParsedPage:
    url: str
    title: str
    markdown: str
    tables: list[list[list[str]]] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    image_alts: list[str] = field(default_factory=list)  # alt text song song image_urls
    # văn bản sát ảnh (figcaption / ô bảng cùng hàng / đoạn liền trước-sau), song song image_urls —
    # nguồn chính để biết ảnh chụp ai/sự kiện gì (alt của site 85% chỉ lặp tiêu đề bài)
    image_contexts: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    content_fallback: bool = False  # True = không selector nào khớp → đã lấy cả trang (AC-H3)
    published_text: str = ""  # "Thứ Sáu, 10:31 11/09/2026" (trước khi strip khối meta)
    section_label: str = ""   # breadcrumb mục: "TIN TỨC"
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (url, anchor) pdf/doc/xls/ppt
    # D23: cấu trúc trong trang theo thứ tự DOM — [{idx, type, level, heading_path, text, attrs}];
    # type: heading | paragraph | list | quote | table | image | video | attachment
    blocks: list[dict] = field(default_factory=list)


# DNN content container heuristics — main body lives in these.
# Theme hiện tại (irs-*): bài chi tiết ở irs-blog-single-col; mọi loại trang có cột
# chính col-md-8 cạnh sidebar col-md-4 (irs-side-bar: video/ảnh chung toàn site).
_CONTENT_SELECTORS = [
    "div.irs-blog-single-col",
    "section.irs-blog-single-field div.col-md-8",
    "div[itemprop='articleBody']",
    "div[class*='detailcontent']",
    "div[class*='DetailContent']",
    "div[class*='content-detail']",
    "div[class*='page-content']",
    "div[id*='dnn_ctr']",
    "article",
    "main",
]

# nav/footer junk to strip
_STRIP_SELECTORS = [
    "nav", "header", "footer", "script", "style", "noscript",
    "div[class*='breadcrumb']", "div[class*='menu']", "ul[class*='menu']",
    "div[class*='sidebar']", "div[class*='side-bar']", "div[class*='footer']",
    "div[class*='banner']",
    "div[class*='carousel']", "div[class*='slide']",
    "div[class*='irs-info-text']",  # ngày đăng + lượt xem (lượt xem đổi liên tục → hash nhảy)
    "div[class*='irs-courses-img-text']",  # meta ngày/lượt xem dạng khác (thông báo ngắn)
    "div.bottom-box",               # tags + nút chia sẻ cuối bài
    "div.row:has(> h3.irs-sidebar-title)",  # khối "Các bài đã đăng" (list tiêu đề bài khác)
]
# Selector theme irs-* nhận cả thông báo ngắn ("…xem tại đây" + file đính kèm); selector
# chung giữ ngưỡng 200 ký tự để tránh bắt nhầm khung rỗng.
_THEME_SELECTORS = {"div.irs-blog-single-col"}

_DATE_RE = re.compile(r"(?:\d{1,2}:\d{2}\s+)?\d{1,2}/\d{1,2}/\d{4}")
_ATTACH_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?)$", re.I)


def _extract_content(soup: BeautifulSoup) -> BeautifulSoup:
    for sel in _CONTENT_SELECTORS:
        node = soup.select_one(sel)
        min_len = 20 if sel in _THEME_SELECTORS else 200
        if node and len(node.get_text(strip=True)) > min_len:
            return node
    return soup


_BLOCKS = ("p", "figure", "li", "td", "th", "h2", "h3", "h4", "h5", "div")


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el is not None else ""


def _image_context(img, limit: int = 300) -> str:
    """Văn bản gần ảnh nhất: figcaption → chữ trong cùng khối → hàng bảng → khối liền trước/sau
    (bỏ khối rỗng, tối đa 2 bước mỗi phía)."""
    fig = img.find_parent("figure")
    if fig is not None and fig.find("figcaption"):
        return _text(fig.find("figcaption"))[:limit]
    block = img.find_parent(_BLOCKS)
    if block is None:
        return ""
    own = _text(block)
    if own:
        return own[:limit]
    if block.name in ("td", "th"):
        row = block.find_parent("tr")
        if row is not None and _text(row):
            return _text(row)[:limit]
    parts = []
    for direction in ("find_previous_sibling", "find_next_sibling"):
        sib = block
        for _ in range(2):
            sib = getattr(sib, direction)()
            if sib is None or sib.find("img") is not None:
                break
            t = _text(sib)
            if t:
                parts.append(t[:limit // 2])
                break
    return " … ".join(parts)[:limit]


# ---- D23: cây block theo thứ tự DOM (heading có cấp + đường dẫn heading, vị trí ảnh/video/file) ----

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_TEXT_BLOCKS = {"p": "paragraph", "ul": "list", "ol": "list", "blockquote": "quote", "pre": "paragraph"}
_INLINE = {"img", "a", "span", "strong", "b", "em", "i", "u", "small", "sup", "sub", "font", "br", "label", "code", "mark"}
_VIDEO_SRC = re.compile(r"(youtube|vimeo|facebook)\.com|\.mp4")


def _media_blocks(node, url: str, push) -> None:
    """Ảnh / video / file đính kèm nằm trong `node`, theo thứ tự xuất hiện (tên ảnh no-image bị bỏ như ở parse_html)."""
    tags = ["img", "video", "iframe", "a"]
    for el in ([node] if getattr(node, "name", None) in tags else []) + node.find_all(tags):
        if el.name == "img":
            src = el.get("src") or el.get("data-src") or ""
            if src and "no-image" not in src.lower():
                push("image", "", {"url": urljoin(url, src),
                                    "alt": (el.get("alt") or el.get("title") or "").strip()[:500]})
        elif el.name == "video":
            src = el.get("src") or next((s.get("src") for s in el.find_all("source") if s.get("src")), "")
            if src:
                push("video", "", {"url": urljoin(url, src)})
        elif el.name == "iframe":
            src = el.get("src", "")
            if _VIDEO_SRC.search(src):
                push("video", "", {"url": urljoin(url, src)})
        else:
            u = urljoin(url, el.get("href", ""))
            if el.get("href") and _ATTACH_RE.search(u.split("?", 1)[0]):
                blk = el.find_parent(["li", "p", "td", "h3", "h4"]) or el
                push("attachment", "", {"url": u, "desc": blk.get_text(" ", strip=True)[:300]})


def build_blocks(content, url: str) -> list[dict]:
    """Duyệt vùng nội dung (đã strip nav/sidebar) thành dãy block theo thứ tự DOM. Heading cập nhật
    `heading_path` (tổ tiên theo cấp) cho mọi block đứng sau nó; bảng thừa hưởng heading của mục chứa nó."""
    blocks: list[dict] = []
    stack: list[tuple[int, str]] = []      # (cấp, chữ heading)

    def push(type_: str, text: str, attrs: dict | None = None, level: int = 0) -> None:
        blocks.append({"idx": len(blocks), "type": type_, "level": level,
                       "heading_path": [h for _, h in stack], "text": text, "attrs": attrs or {}})

    def inline_flush(buf: list) -> None:
        if not buf:
            return
        text = re.sub(r"\s+", " ", " ".join(
            (x.get_text(" ", strip=True) if hasattr(x, "get_text") else str(x)) for x in buf)).strip()
        if text:
            push("paragraph", text)
        for x in buf:
            if hasattr(x, "find_all"):
                _media_blocks(x, url, push)
        buf.clear()

    def walk(node) -> None:
        buf: list = []
        for ch in node.children:
            name = getattr(ch, "name", None)
            if name is None:                                   # chữ trần
                if str(ch).strip():
                    buf.append(ch)
            elif name in _INLINE:
                buf.append(ch)
            elif name in _HEADINGS:
                inline_flush(buf)
                lvl, text = _HEADINGS[name], _text(ch)
                if text:
                    while stack and stack[-1][0] >= lvl:
                        stack.pop()
                    stack.append((lvl, text))
                    push("heading", text, level=lvl)
                _media_blocks(ch, url, push)
            elif name == "table":
                inline_flush(buf)
                rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in ch.find_all("tr")]
                rows = [r for r in rows if any(r)]
                if rows:
                    push("table", "\n".join(" | ".join(r) for r in rows), {"rows": len(rows)})
                _media_blocks(ch, url, push)
            elif name in _TEXT_BLOCKS:
                inline_flush(buf)
                text = _text(ch)
                if text:
                    push(_TEXT_BLOCKS[name], text)
                _media_blocks(ch, url, push)
            elif name in ("figure", "video", "iframe", "picture"):
                inline_flush(buf)
                cap = ch.find("figcaption")
                _media_blocks(ch, url, push)
                if cap is not None and _text(cap):
                    push("paragraph", _text(cap))
            else:                                              # div, section, article, ... → đi sâu
                inline_flush(buf)
                walk(ch)
        inline_flush(buf)

    walk(content)
    return blocks


def parse_html(url: str, html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "") or url

    content = _extract_content(soup)
    fallback = content is soup
    published = ""
    info = (content.select_one("div[class*='irs-info-text']")
            or content.select_one("div[class*='irs-courses-img-text']"))
    m = _DATE_RE.search(info.get_text(" ", strip=True)) if info else None
    if m:
        published = info.get_text(" ", strip=True)[:m.end()].strip()
    heading = soup.select_one(".irs-inner-heading h2")
    section_label = heading.get_text(" ", strip=True) if heading else ""
    for sel in _STRIP_SELECTORS:
        for node in content.select(sel):
            node.decompose()
    # ô bảng dùng <br> để xuống dòng giữa các mục (danh sách sinh viên của đề tài, nhiều tên 1 ô) —
    # markdownify / get_text nối thành dấu cách → 2 tên dính thành 1 ("Cao Bá Lộc Lê Minh Hiếu"). Giữ ranh
    # giới bằng "; " (80/1.919 trang snapshot 2026-10-12 có ô kiểu này).
    for br in content.select("td br, th br"):
        br.replace_with("; ")

    # tables → list of rows of cells
    tables: list[list[list[str]]] = []
    for tbl in content.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)

    # images (skip tiny icons / no-image placeholders) — giữ alt làm caption gốc
    seen: dict[str, str] = {}
    contexts: dict[str, str] = {}
    for img in content.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or "no-image" in src.lower():
            continue
        u = urljoin(url, src)
        if u not in seen:
            seen[u] = (img.get("alt") or img.get("title") or "").strip()[:500]
            contexts[u] = _image_context(img)

    # videos: <video src>, <source>, youtube/vimeo embeds
    video_urls = []
    for v in content.find_all("video"):
        if v.get("src"):
            video_urls.append(urljoin(url, v["src"]))
        for s in v.find_all("source"):
            if s.get("src"):
                video_urls.append(urljoin(url, s["src"]))
    for iframe in content.find_all("iframe"):
        src = iframe.get("src", "")
        if _VIDEO_SRC.search(src):
            video_urls.append(urljoin(url, src))

    # file đính kèm trong vùng nội dung (mọi host: thường ở *.haui.edu.vn/media/...)
    attachments: dict[str, str] = {}
    for a in content.find_all("a", href=True):
        u = urljoin(url, a["href"])
        if _ATTACH_RE.search(u.split("?", 1)[0]) and u not in attachments:
            # anchor thường chỉ là "Tải về tại đây" → lấy text khối cha làm mô tả
            block = a.find_parent(["li", "p", "td", "h3", "h4"]) or a
            attachments[u] = block.get_text(" ", strip=True)[:300]

    # internal links for frontier
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        links.append(urljoin(url, href))

    blocks = build_blocks(content, url)
    markdown = md(str(content), heading_style="ATX", strip=["img"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return ParsedPage(
        url=url, title=title, markdown=markdown, tables=tables,
        image_urls=list(seen.keys()),
        image_alts=[seen[u] for u in seen],
        image_contexts=[contexts[u] for u in seen],
        video_urls=list(dict.fromkeys(video_urls)),
        links=list(dict.fromkeys(links)),
        content_fallback=fallback,
        published_text=published,
        section_label=section_label,
        attachments=list(attachments.items()),
        blocks=blocks,
    )
