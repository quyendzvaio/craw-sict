"""GET /metrics (D13): nhãn route theo mẫu đường dẫn (số chuỗi cố định), heartbeat incremental, pipeline lỗi
không làm hỏng /metrics."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sict_crawler import metrics as M


def test_heartbeat_fields_flatten_nested():
    raw = json.dumps({"ts": "2026-10-18T05:24:47+00:00", "discovered": 3, "changed": 2, "drained": 300,
                      "listings": {"roots": 17, "new": 4, "errors": 1}, "drain": {"failed": 2, "gave_up": 0},
                      "stage_errors": []})
    f = M.heartbeat_fields(raw)
    assert f["discovered"] == 3 and f["drained"] == 300 and f["listing_new"] == 4 and f["drain_failed"] == 2
    assert "roots" not in f and M._ts(raw) == 1792301087.0
    assert M.heartbeat_fields(None) == {} and M.heartbeat_fields("không phải json") == {} and M._ts(b"{}") is None


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()

    @app.get("/media/s/{key}")
    def media(key: str):
        return {"k": key}

    @app.get("/boom")
    def boom():
        raise RuntimeError("x")

    M.instrument(app)

    async def no_pipeline(ttl=30.0):   # không chạm Redis/PG thật
        M.PIPELINE_UP.set(0)
    monkeypatch.setattr(M, "refresh_pipeline", no_pipeline)
    return TestClient(app, raise_server_exceptions=False)


def _value(text: str, line_start: str) -> float:
    return sum(float(l.rsplit(" ", 1)[1]) for l in text.splitlines() if l.startswith(line_start))


def test_route_label_is_template_and_errors_counted(client):
    before = _value(client.get("/metrics").text, 'sict_http_requests_total{method="GET",route="/media/s/{key}",status="200"}')
    for k in ("a" * 64, "b" * 64, "c" * 64):
        assert client.get(f"/media/s/{k}").status_code == 200
    assert client.get("/boom").status_code == 500
    text = client.get("/metrics").text
    assert _value(text, 'sict_http_requests_total{method="GET",route="/media/s/{key}",status="200"}') == before + 3
    assert 'route="/boom",status="500"' in text and "a" * 64 not in text       # không có URL thật trong nhãn
    assert 'route="/metrics"' not in text and "sict_pipeline_metrics_up 0.0" in text
