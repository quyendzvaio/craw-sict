"""Self-check cho cầu chì xóa + cache key + routes tĩnh (không cần services).

Run: python -m sict_crawler.tests.test_safety  |  pytest sict_crawler/tests/test_safety.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler.api import _query_cache_key
from sict_crawler.incremental import mass_delete_abort, should_delete


def test_only_404_410_delete():
    assert should_delete(404) and should_delete(410)
    assert not should_delete(200)
    assert not should_delete(403)   # chặn bot → giữ doc
    assert not should_delete(429)   # rate-limit → giữ doc
    assert not should_delete(500)
    assert not should_delete(None)


def test_mass_delete_guard():
    assert not mass_delete_abort(0, 500)
    assert not mass_delete_abort(19, 500)   # dưới ngưỡng tuyệt đối
    assert not mass_delete_abort(5, 500)    # dưới 20% tương đối
    assert mass_delete_abort(500, 500)      # xóa sạch → abort
    assert mass_delete_abort(167, 500)
    assert not mass_delete_abort(0, 0)


def test_cache_key_stable_and_scoped():
    a = _query_cache_key("tuyển sinh", 5)
    assert a == _query_cache_key("tuyển sinh", 5)
    assert a.startswith("q:v1:")
    assert a != _query_cache_key("tuyển sinh", 8)
    assert a != _query_cache_key("tuyển sinh?", 5)


class _NoRow:
    """DB giả: media không tồn tại → 404, không cần PG thật."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return None


def _check_routes():
    from fastapi.testclient import TestClient

    from sict_crawler.api import app

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/")
    assert r.status_code == 200 and "SICT" in r.text
    assert "no-store" in r.headers.get("cache-control", "")
    js = c.get("/app.js")
    assert js.status_code == 200 and "addEventListener" in js.text
    assert "max-age" in js.headers.get("cache-control", "")
    assert c.get("/media/999999999").status_code == 404


def test_static_routes(monkeypatch):
    from sict_crawler import api as _api

    monkeypatch.setattr(_api.db, "get_session", lambda: _NoRow())
    _check_routes()


if __name__ == "__main__":
    test_only_404_410_delete()
    print("OK — chỉ 404/410 mới xóa")
    test_mass_delete_guard()
    print("OK — cầu chì mass-delete")
    test_cache_key_stable_and_scoped()
    print("OK — cache key ổn định, phân biệt top_k")
    from sict_crawler import api as _api

    _orig = _api.db.get_session
    _api.db.get_session = lambda: _NoRow()
    try:
        _check_routes()
    finally:
        _api.db.get_session = _orig
    print("OK — routes /, /app.js, /media 404")
