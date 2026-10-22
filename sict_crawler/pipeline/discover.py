"""Discover-only: fetch seed listing pages (httpx) → parser links → frontier.push.

Nhẹ, không torch: chỉ import httpx/parser/frontier/config.
Dùng để làm ấm queue trước khi worker chạy incremental full. Idempotent: URL cũ đã có trong
frontier:seen (Redis SET) thì push() tự skip.

Usage:
  python -m sict_crawler.pipeline.discover
  CHECK_ONLY=1 python -m sict_crawler.pipeline.discover  # log số new URL, exit 0/1
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

from sict_crawler.core.config import get_settings
from sict_crawler.repository.frontier import Frontier
from sict_crawler.service.crawl.parser import parse_html


async def discover_new() -> int:
    cfg = get_settings()
    frontier = Frontier()
    before = frontier.seen_count()
    async with httpx.AsyncClient(
        timeout=cfg.crawl.timeout_seconds, follow_redirects=True,
        headers={"User-Agent": cfg.crawl.user_agent},
    ) as client:
        for seed in cfg.site.seed_urls:
            try:
                r = await client.get(seed)
                if r.status_code != 200 or not r.text:
                    print(f"[discover] skip {seed}: http={r.status_code}", flush=True)
                    continue
                for link in parse_html(seed, r.text).links:
                    frontier.push(link, 1)
            except Exception as e:
                print(f"[discover] skip {seed}: {e}", flush=True)
    new = frontier.seen_count() - before
    print(f"[discover] {new} new URL(s) queued", flush=True)
    return new


def main() -> None:
    new = asyncio.run(discover_new())
    if os.environ.get("CHECK_ONLY") == "1" and new == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
