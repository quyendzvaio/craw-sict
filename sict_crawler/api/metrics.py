"""Metric Prometheus cho Grafana Cloud (D13): HTTP của API + trạng thái pipeline, scrape bởi Alloy (job sict/api).

Callers: api.py (`instrument(app)` thêm middleware + GET /metrics).
HTTP: nhãn `route` = mẫu đường dẫn của FastAPI (`/media/s/{key}`), không phải URL thật → số chuỗi cố định.
Pipeline (tính lúc scrape, cache 30 s — Alloy scrape 60 s/lần):
  sict_incremental_last_{ok,error}_timestamp_seconds   heartbeat Redis `incremental:last_ok/last_error` (ts trong JSON)
  sict_incremental_last_run{field}                      số liệu lượt incremental gần nhất (discovered, drained, changed…)
  sict_documents_total                                  số bài trong PG (tăng trong 24 h = bài mới)
  sict_frontier_{queue,retry,failed}                    hàng đợi / URL chờ thử lại / URL tải lỗi tạm thời
Dashboard: tuổi heartbeat = time() - sict_incremental_last_ok_timestamp_seconds (> 13 h là có vấn đề: cron 12h/lần, plan.md).
"""
from __future__ import annotations

import datetime as dt
import json
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)

disable_created_metrics()   # bỏ chuỗi *_created (không dùng, nhân đôi số chuỗi bị tính hạn mức Grafana Cloud)
REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter("sict_http_requests_total", "Số request HTTP của API", ["route", "method", "status"],
                        registry=REGISTRY)
HTTP_LATENCY = Histogram("sict_http_request_duration_seconds", "Thời gian xử lý request HTTP", ["route", "method"],
                         buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 60, 120), registry=REGISTRY)
LAST_OK = Gauge("sict_incremental_last_ok_timestamp_seconds", "Lượt incremental thành công gần nhất (unix)",
                registry=REGISTRY)
LAST_ERROR = Gauge("sict_incremental_last_error_timestamp_seconds", "Lượt incremental lỗi gần nhất (unix)",
                   registry=REGISTRY)
LAST_RUN = Gauge("sict_incremental_last_run", "Số liệu lượt incremental thành công gần nhất", ["field"],
                 registry=REGISTRY)
DOCS = Gauge("sict_documents_total", "Số bài trong Postgres", registry=REGISTRY)
FRONTIER = Gauge("sict_frontier", "Frontier: queue = chờ tải, retry = chờ thử lại, failed = URL đang đếm lỗi",
                 ["state"], registry=REGISTRY)
PIPELINE_UP = Gauge("sict_pipeline_metrics_up", "1 = đọc được Redis + Postgres ở lần scrape gần nhất",
                    registry=REGISTRY)

_RUN_FIELDS = ("discovered", "checked", "changed", "deleted", "errors", "drained")
_cache = {"at": 0.0}


def _ts(raw: bytes | str | None) -> float | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(json.loads(raw)["ts"]).timestamp()
    except (ValueError, KeyError, TypeError):
        return None


def heartbeat_fields(raw: bytes | str | None) -> dict[str, float]:
    """JSON heartbeat → {field: số}. Trường lồng: listings.new → listing_new, drain.failed → drain_failed."""
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    out = {k: float(d[k]) for k in _RUN_FIELDS if isinstance(d.get(k), (int, float))}
    for parent, keys in (("listings", ("new", "errors")), ("drain", ("failed", "gave_up", "requeued"))):
        sub = d.get(parent) or {}
        for k in keys:
            if isinstance(sub.get(k), (int, float)):
                out[f"{parent.rstrip('s')}_{k}"] = float(sub[k])
    return out


async def refresh_pipeline(ttl: float = 30.0) -> None:
    """Đọc Redis (heartbeat, frontier) + Postgres (số bài); lỗi → sict_pipeline_metrics_up 0, không làm hỏng /metrics."""
    if time.monotonic() - _cache["at"] < ttl:
        return
    _cache["at"] = time.monotonic()
    try:
        import redis
        from sqlalchemy import func, select

        from sict_crawler.core.config import get_settings
        from sict_crawler.model.orm import Document
        from sict_crawler.repository import db
        r = redis.Redis.from_url(get_settings().redis_url, socket_timeout=3)
        ok, err = r.get("incremental:last_ok"), r.get("incremental:last_error")
        for g, raw in ((LAST_OK, ok), (LAST_ERROR, err)):
            t = _ts(raw)
            if t is not None:
                g.set(t)
        for k, v in heartbeat_fields(ok).items():
            LAST_RUN.labels(k).set(v)
        FRONTIER.labels("queue").set(r.llen("frontier:list"))
        FRONTIER.labels("retry").set(r.hlen("frontier:retry"))
        FRONTIER.labels("failed").set(r.hlen("frontier:fails"))
        async with db.get_session() as s:
            DOCS.set(await s.scalar(select(func.count()).select_from(Document)) or 0)
        PIPELINE_UP.set(1)
    except Exception as e:   # noqa: BLE001 — Redis/PG tạm lỗi: báo qua metric, không ném ra
        PIPELINE_UP.set(0)
        print(f"[metrics] pipeline refresh lỗi: {type(e).__name__}: {e}")


def instrument(app) -> None:
    """Middleware đo mọi request (trừ /metrics) + endpoint GET /metrics."""
    from fastapi import Request, Response

    @app.middleware("http")
    async def _measure(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        t0 = time.perf_counter()
        status = 500
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        finally:
            route = getattr(request.scope.get("route"), "path", None) or "other"
            HTTP_REQUESTS.labels(route, request.method, str(status)).inc()
            HTTP_LATENCY.labels(route, request.method).observe(time.perf_counter() - t0)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        await refresh_pipeline()
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
