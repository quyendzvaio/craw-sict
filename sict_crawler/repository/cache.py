"""Cache BE (Redis db 3, best-effort): kết quả truy vấn + bytes media. Mọi key đều có TTL nên an toàn với
chính sách volatile-ttl — frontier (không TTL) không bao giờ bị đuổi. Redis lỗi → chạy tiếp không cache."""
from __future__ import annotations

from sict_crawler.core.config import get_settings

_client = None


def client():
    """Redis db 3 lazy. Lỗi kết nối → None (chạy tiếp không cache)."""
    global _client
    if _client is None:
        try:
            from urllib.parse import urlsplit

            import redis

            # from_url bỏ qua db= kwarg (path /0 thắng) → parse tay để chắc db 3
            u = urlsplit(get_settings().redis_url)
            _client = redis.Redis(
                host=u.hostname or "localhost", port=u.port or 6379,
                username=u.username or None,
                password=u.password or None, db=3,
                socket_connect_timeout=2, socket_timeout=2)
            _client.ping()
        except Exception:
            _client = False
    return _client or None


def get(key: str) -> bytes | None:
    try:
        c = client()
        return c.get(key) if c else None
    except Exception:
        return None


def put(key: str, val: bytes, ttl: int) -> None:
    try:
        c = client()
        if c:
            c.setex(key, ttl, val)
    except Exception:
        pass
