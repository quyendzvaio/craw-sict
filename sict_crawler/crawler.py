"""Fetch URL → HTML. Backend cascade: httpx → obscura → crawl4ai (legacy).

- httpx: đủ cho ~90% trang DNN SICT (server-rendered), ~50ms, ~0 RAM thêm.
- obscura (Rust ~70MiB, ~30MB RAM): fallback khi gặp vỏ JS / cần render.
  CLI 1-shot `obscura fetch --dump html` hoặc sidecar CDP `obscura serve`.
- crawl4ai/Playwright/Chromium: legacy, chỉ dùng khi CRAWLER_BACKEND=crawl4ai
  (tốn ~200MB+ RAM, ~300MB binary — lý do cần thay).

Env: CRAWLER_BACKEND=auto (mặc định) | httpx | obscura | crawl4ai
     OBSCURA_BIN, OBSCURA_TIMEOUT, OBSCURA_CDP (xem crawler_obscura.py)

Callers: main.py, incremental.py.
Schemas: fetch(url) → (html: str, status: int).
"""
from __future__ import annotations

import asyncio
import os

import httpx

from .config import get_settings


async def _fetch_httpx(url: str) -> tuple[str, int]:
    cfg = get_settings().crawl
    try:
        async with httpx.AsyncClient(
            timeout=cfg.timeout_seconds, follow_redirects=True,
            headers={"User-Agent": cfg.user_agent},
        ) as c:
            r = await c.get(url)
            if r.status_code in (404, 410):
                return "", r.status_code
            if r.status_code != 200 or not r.text:
                return "", 0
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and len(r.text) < 500:
                return "", 0
            return r.text, 200
    except Exception:
        return "", 0


async def _fetch_crawl4ai(url: str) -> tuple[str, int]:
    """Legacy Playwright/Chromium path. Import lazy để không bắt buộc cài."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    except ImportError:
        return "", 0
    cfg = get_settings().crawl
    browser_cfg = BrowserConfig(headless=True, user_agent=cfg.user_agent)
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=cfg.timeout_seconds * 1000,
        wait_until="domcontentloaded",
    )
    try:
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)
            if not result.success:
                return "", result.status_code or 0
            return result.html, 200
    except Exception:
        return "", 0


async def fetch(url: str) -> tuple[str, int]:
    backend = os.environ.get("CRAWLER_BACKEND", "auto").lower()
    if backend == "httpx":
        return await _fetch_httpx(url)
    if backend == "obscura":
        from . import crawler_obscura as ob
        return await ob.fetch_cli(url)
    if backend == "crawl4ai":
        return await _fetch_crawl4ai(url)

    # auto: httpx trước (rẻ), obscura fallback khi vỏ JS, crawl4ai cuối
    html, status = await _fetch_httpx(url)
    if status == 200 and html:
        from . import crawler_obscura as ob
        if not ob._looks_like_js_shell(html):
            return html, 200
    else:
        if status in (404, 410):
            return "", status
    from . import crawler_obscura as ob
    html, status = await ob.fetch_cli(url)
    if status == 200 and html:
        return html, 200
    # không có obscura binary hoặc vẫn fail → thử crawl4ai nếu còn cài
    return await _fetch_crawl4ai(url)


def fetch_sync(url: str) -> tuple[str, int]:
    return asyncio.run(fetch(url))
