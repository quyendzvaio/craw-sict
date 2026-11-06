"""Fingerprint index vào /metrics: 1 info-series cố định (không nhân series theo request)."""
from prometheus_client import generate_latest

from sict_crawler.api import metrics
from sict_crawler.core.config import get_settings
from sict_crawler.core.fingerprint import index_fingerprint


def test_metrics_registers_index_fingerprint():
    text = generate_latest(metrics.REGISTRY).decode()
    line = next(ln for ln in text.splitlines() if ln.startswith("sict_index_fingerprint_info{"))
    assert index_fingerprint() in line
    assert get_settings().qdrant.collection_text in line
    assert line.endswith(" 1.0")
    assert text.count("sict_index_fingerprint_info{") == 1
