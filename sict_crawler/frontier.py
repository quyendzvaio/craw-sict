"""URL frontier: Redis BFS queue + dedup + per-host politeness + thử lại khi tải lỗi tạm thời.

Callers: crawler.py (push/pop), main.py (seed), incremental.py.
Schemas: Redis keys — frontier:list (RPUSH/LINDEX), frontier:seen (SET), frontier:host_ts (HASH),
frontier:retry (HASH url → depth: chờ thử lại ở lượt sau), frontier:fails (HASH url → số lần lỗi liên tiếp).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse

import redis

from .config import get_settings


def canonicalize(url: str) -> str:
    """Normalize URL: lowercase host, http→https, bỏ "/" cuối (trừ gốc), strip fragment,
    sort query, strip tracking params.

    Site link trộn http/https và có/không "/" cuối → không chuẩn hoá thì 1 trang bị crawl 2-4 lần
    (snapshot 2026-10-12: 139 URL trùng chỉ vì scheme).
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = sorted(
        p for p in parsed.query.split("&")
        if p and not re.match(r"^(utm_|fbclid|gclid|_ga)", p)
    )
    return f"{scheme}://{host}{path}" + (f"?{'&'.join(query)}" if query else "")


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

    # ---- thử lại URL tải lỗi tạm thời. URL đã pop khỏi hàng đợi nhưng vẫn nằm trong frontier:seen → nếu
    # không ghi lại thì push() về sau luôn bỏ qua nó (mất vĩnh viễn).
    def fail(self, url: str, depth: int, max_tries: int = 3) -> bool:
        """Ghi 1 lần tải lỗi. True = còn lượt (chờ requeue_failed ở lượt sau); False = đã lỗi max_tries lần
        liên tiếp → bỏ (vẫn giữ trong seen để không lặp vô hạn; số lần lỗi giữ lại trong frontier:fails)."""
        url = canonicalize(url)
        n = self.r.hincrby("frontier:fails", url, 1)
        if n < max_tries:
            self.r.hset("frontier:retry", url, str(depth))
            return True
        self.r.hdel("frontier:retry", url)
        return False

    def succeeded(self, url: str) -> None:
        """Tải được (hoặc 404/410 dứt điểm) → xoá bộ đếm lỗi."""
        url = canonicalize(url)
        self.r.hdel("frontier:fails", url)
        self.r.hdel("frontier:retry", url)

    def requeue_failed(self) -> int:
        """Đưa URL đang chờ thử lại vào cuối hàng đợi (bỏ qua kiểm tra seen). Gọi đầu mỗi lượt drain."""
        items = self.r.hgetall("frontier:retry")
        for url, depth in items.items():
            self.r.rpush("frontier:list", f"{depth}|{url}")
            self.r.hdel("frontier:retry", url)
        return len(items)

    def is_seen(self, url: str) -> bool:
        return bool(self.r.sismember("frontier:seen", canonicalize(url)))

    def seen_urls(self):
        return self.r.sscan_iter("frontier:seen", count=1000)

    def size(self) -> int:
        return self.r.llen("frontier:list")

    def seen_count(self) -> int:
        return self.r.scard("frontier:seen")

    def reset(self):
        self.r.delete("frontier:list", "frontier:seen", "frontier:host_ts", "frontier:retry", "frontier:fails")
