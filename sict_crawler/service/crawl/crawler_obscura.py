"""Obscura fetcher: thay Playwright/Chromium bằng headless Rust (~70MiB, ~30MB RAM).

Obscura chạy 2 kiểu:
  1. CLI 1-shot (khuyên dùng cho cron): `obscura fetch <url> --dump html --timeout 30`
     → không cần giữ browser resident, mỗi page ~85ms, RAM transient.
  2. Sidecar CDP: `obscura serve --port 9222` + playwright-core connectOverCDP
     → hợp khi cần cookies/session/login, hoặc parallelism cao.

Env:
  OBSCURA_BIN=obscura            # path binary (mặc định tìm trong PATH)
  OBSCURA_TIMEOUT=30             # giây/navigation
  OBSCURA_DUMP=html              # html | text | markdown
  OBSCURA_ALLOW_PRIVATE=0        # 1 → thêm --allow-private-network (dev localhost)
  CRAWLER_BACKEND=auto           # auto | httpx | obscura | crawl4ai
                                 # auto = httpx trước, obscura fallback JS
"""
from __future__ import annotations

import asyncio
import os
import shutil

from sict_crawler.core.config import get_settings


def is_available() -> bool:
    return shutil.which(os.environ.get("OBSCURA_BIN", "obscura")) is not None


def _looks_like_js_shell(html: str) -> bool:
    """DNN SICT render server-side nên httpx thường đủ. Chỉ fallback Obscura
    khi HTML là vỏ JS rỗng (SPA shell) hoặc quá ngắn."""
    if not html or len(html) < 1500:
        return True
    low = html.lower()
    markers = ("id=\"root\"", "id=\"app\"", "__next_data__", "ng-app",
               "data-reactroot", "enable javascript", "please enable js")
    if any(m in low for m in markers) and len(html) < 8000:
        return True
    return False


async def fetch_cli(url: str, timeout: int | None = None,
                    dump: str | None = None) -> tuple[str, int]:
    """1-shot qua binary: obscura fetch --dump html. Returns (html, 200|0)."""
    bin_path = os.environ.get("OBSCURA_BIN", "obscura")
    if not shutil.which(bin_path):
        return "", 0
    cfg = get_settings().crawl
    timeout = timeout or int(os.environ.get("OBSCURA_TIMEOUT", "30"))
    dump = dump or os.environ.get("OBSCURA_DUMP", "html")
    cmd = [bin_path, "fetch", url, "--dump", dump, "--timeout", str(timeout)]
    if os.environ.get("OBSCURA_ALLOW_PRIVATE", "0") == "1":
        cmd.append("--allow-private-network")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 10)
        except asyncio.TimeoutError:
            proc.kill()
            return "", 0
        if proc.returncode != 0 or not out:
            return "", 0
        html = out.decode(errors="replace")
        return (html, 200) if len(html) > 200 else ("", 0)
    except (FileNotFoundError, OSError):
        return "", 0


async def fetch_cdp(url: str, cdp_endpoint: str | None = None) -> tuple[str, int]:
    """Qua sidecar `obscura serve --port 9222` bằng playwright (không tải Chromium).

    Chỉ dùng khi cần session/cookies. Cần pip install playwright (chỉ lib client,
    đừng chạy `playwright install`). Sidecar chạy: docker run -p 9222:9222 h4ckf0r0day/obscura
    """
    endpoint = cdp_endpoint or os.environ.get(
        "OBSCURA_CDP", "ws://127.0.0.1:9222")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "", 0
    cfg = get_settings().crawl
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(endpoint)
            ctx = await browser.new_context(user_agent=cfg.user_agent)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=cfg.timeout_seconds * 1000)
                html = await page.content()
            finally:
                await ctx.close()
                await browser.close()
        return (html, 200) if html else ("", 0)
    except Exception:
        return "", 0
