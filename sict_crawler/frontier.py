"""URL frontier: Redis BFS queue + dedup + per-host politeness.

Callers: crawler.py (push/pop), main.py (seed).
Schemas: Redis keys — frontier:list (RPUSH/LINDEX), frontier:seen (SET), frontier:host_ts (HASH).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse

import redis

from .config import get_settings


def canonicalize(url: str) -> str:
    """Normalize URL: lowercase host, strip fragment, sort query, strip tracking params."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    query = sorted(
        p for p in parsed.query.split("&")
        if p and not re.match(r"^(utm_|fbclid|gclid|_ga)", p)
    )
    return f"{parsed.scheme}://{host}{path}" + (f"?{'&'.join(query)}" if query else "")


class Frontier:
    def __init__(self, redis_url: str | None = None):
        cfg = get_settings()
        self.r = redis.Redis.from_url(redis_url or cfg.redis_url, decode_responses=True)
        self.base_host = urlparse(cfg.site.base_url).netloc.lower()
        self.skip_re = [re.compile(p) for p in cfg.site.skip_patterns]
        self.external_hosts = set(cfg.site.external_hosts)
        self.delay = cfg.crawl.delay_seconds
        self.max_depth = cfg.crawl.max_depth
        self.max_pages = cfg.crawl.max_pages

    def _is_internal(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == self.base_host or host in self.external_hosts

    def should_skip(self, url: str) -> bool:
        if not self._is_internal(url):
            return True
        return any(rx.search(url) for rx in self.skip_re)

    def push(self, url: str, depth: int = 0) -> bool:
        """Add URL if unseen, internal, not skipped, within depth. Returns True if queued."""
        if depth > self.max_depth:
            return False
        url = canonicalize(urljoin(self.base_host, url))
        if self.should_skip(url):
            return False
        if self.r.sismember("frontier:seen", url):
            return False
        pipe = self.r.pipeline()
        pipe.sadd("frontier:seen", url)
        pipe.rpush("frontier:list", f"{depth}|{url}")
        pipe.execute()
        return True

    def pop(self) -> tuple[str, int] | None:
        """Pop next URL respecting per-host delay. Returns (url, depth) or None."""
        host_key = "frontier:host_ts"
        now = time.time()
        for idx in range(self.r.llen("frontier:list")):
            item = self.r.lindex("frontier:list", idx)
            if not item:
                continue
            depth_str, url = item.split("|", 1)
            host = urlparse(url).netloc
            last = float(self.r.hget(host_key, host) or 0)
            if now - last >= self.delay:
                self.r.lset("frontier:list", idx, "__taken__")
                self.r.lrem("frontier:list", 1, "__taken__")
                self.r.hset(host_key, host, str(now))
                return url, int(depth_str)
        return None

    def size(self) -> int:
        return self.r.llen("frontier:list")

    def seen_count(self) -> int:
        return self.r.scard("frontier:seen")

    def reset(self):
        self.r.delete("frontier:list", "frontier:seen", "frontier:host_ts")
