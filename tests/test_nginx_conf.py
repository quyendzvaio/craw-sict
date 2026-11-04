"""nginx là cửa duy nhất ra Internet: không được chuyển /metrics của API ra ngoài."""
import re
from pathlib import Path

CONF = (Path(__file__).resolve().parents[1] / "nginx" / "nginx.conf").read_text(encoding="utf-8")


def test_metrics_blocked_at_edge():
    m = re.search(r"location\s*=\s*/metrics\s*\{([^}]*)\}", CONF)
    assert m, "thiếu location = /metrics"
    assert "return 404" in m.group(1) and "proxy_pass" not in m.group(1)


def test_chat_stream_unbuffered():
    m = re.search(r"location\s+/chat\s*\{([^}]*)\}", CONF)
    assert m and "proxy_buffering off" in m.group(1)
