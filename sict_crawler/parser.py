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
    video_urls: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


# DNN content container heuristics — main body lives in these
_CONTENT_SELECTORS = [
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
    "div[class*='sidebar']", "div[class*='footer']", "div[class*='banner']",
    "div[class*='carousel']", "div[class*='slide']",
]


def _extract_content(soup: BeautifulSoup) -> BeautifulSoup:
    for sel in _CONTENT_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    return soup


def parse_html(url: str, html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "") or url

    content = _extract_content(soup)
    for sel in _STRIP_SELECTORS:
        for node in content.select(sel):
            node.decompose()

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
    for img in content.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or "no-image" in src.lower():
            continue
        u = urljoin(url, src)
        if u not in seen:
            seen[u] = (img.get("alt") or img.get("title") or "").strip()[:500]

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
        if re.search(r"(youtube|vimeo|facebook)\.com|\.mp4", src):
            video_urls.append(urljoin(url, src))

    # internal links for frontier
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        links.append(urljoin(url, href))

    markdown = md(str(content), heading_style="ATX", strip=["img"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return ParsedPage(
        url=url, title=title, markdown=markdown, tables=tables,
        image_urls=list(seen.keys()),
        image_alts=[seen[u] for u in seen],
        video_urls=list(dict.fromkeys(video_urls)),
        links=list(dict.fromkeys(links)),
    )
