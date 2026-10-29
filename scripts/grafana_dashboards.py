"""Dashboard "SICT — hệ thống" cho Grafana Cloud (D13) dựng bằng code → monitor/dashboards/sict-overview.json.

    python scripts/grafana_dashboards.py                 # ghi JSON (import tay: Dashboards → New → Import)
    python scripts/grafana_dashboards.py --check         # chạy từng truy vấn trên Prometheus của stack (token .env)
    python scripts/grafana_dashboards.py --push          # nạp lên stack: GRAFANA_URL + GRAFANA_SA_TOKEN (Editor) trong .env

Dashboard máy chủ (CPU/RAM/đĩa) đã có sẵn từ integration Linux Server — ở đây chỉ phần riêng của hệ thống:
tổng quan up, API (/metrics), pipeline (heartbeat incremental > 13 h là có vấn đề: cron 12h/lần — plan.md), Postgres, Redis, Qdrant,
dịch vụ embed. Biến: datasource Prometheus, instance (sict-vm1, sict-vm2, sict-vm1-local…). Không in token.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "monitor" / "dashboards" / "sict-overview.json"
UID = "sict-overview"
I = 'instance=~"$instance"'
DS = {"type": "prometheus", "uid": "${datasource}"}

# (hàng, [(tiêu đề, loại, đơn vị, [(legend, expr)], ngưỡng?)])
ROWS = [
    ("Tổng quan", [
        ("Thành phần đang chạy (up)", "stat", "none", [("{{job}}", f"max by (job) (up{{{I}}})")], [(0, "red"), (1, "green")]),
        ("Heartbeat incremental (tuổi)", "stat", "s",
         [("tuổi", f"time() - max(sict_incremental_last_ok_timestamp_seconds{{{I}}})")], [(0, "green"), (46800, "red")]),
        ("Lượt incremental gần nhất lỗi?", "stat", "none",
         [("lỗi mới hơn OK", f"max(sict_incremental_last_error_timestamp_seconds{{{I}}}) > bool max(sict_incremental_last_ok_timestamp_seconds{{{I}}})")],
         [(0, "green"), (1, "red")]),
        ("Bài trong DB / mới 24 h", "stat", "none",
         [("tổng", f"max(sict_documents_total{{{I}}})"), ("mới 24 h", f"max(delta(sict_documents_total{{{I}}}[24h]))")], None),
    ]),
    ("API", [
        ("Request/giây theo route", "timeseries", "reqps",
         [("{{route}}", f"sum by (route) (rate(sict_http_requests_total{{{I}}}[$__rate_interval]))")], None),
        ("Độ trễ p95 theo route", "timeseries", "s",
         [("{{route}}", f"histogram_quantile(0.95, sum by (le, route) (rate(sict_http_request_duration_seconds_bucket{{{I}}}[$__rate_interval])))")], None),
        ("Tỉ lệ lỗi 5xx", "timeseries", "percentunit",
         [("5xx", f'sum(rate(sict_http_requests_total{{{I},status=~"5.."}}[$__rate_interval])) / sum(rate(sict_http_requests_total{{{I}}}[$__rate_interval]))')], None),
    ]),
    ("Pipeline (crawl incremental)", [
        ("Lượt gần nhất", "bargauge", "none", [("{{field}}", f"max by (field) (sict_incremental_last_run{{{I}}})")], None),
        ("Frontier", "timeseries", "none", [("{{state}}", f"max by (state) (sict_frontier{{{I}}})")], None),
        ("Đọc được Redis + PG", "stat", "none", [("pipeline", f"min(sict_pipeline_metrics_up{{{I}}})")], [(0, "red"), (1, "green")]),
    ]),
    ("Postgres", [
        ("Kết nối / tối đa", "timeseries", "none",
         [("đang dùng", f"sum(pg_stat_activity_count{{{I}}})"), ("tối đa", f"max(pg_settings_max_connections{{{I}}})")], None),
        ("Kích thước DB", "timeseries", "bytes", [("{{datname}}", f'pg_database_size_bytes{{{I},datname!~"template.*"}}')], None),
        ("Giao dịch/giây", "timeseries", "ops",
         [("commit", f'sum(rate(pg_stat_database_xact_commit{{{I}}}[$__rate_interval]))'),
          ("rollback", f'sum(rate(pg_stat_database_xact_rollback{{{I}}}[$__rate_interval]))')], None),
        ("Cache hit", "stat", "percentunit",
         [("hit", f"sum(rate(pg_stat_database_blks_hit{{{I}}}[1h])) / (sum(rate(pg_stat_database_blks_hit{{{I}}}[1h])) + sum(rate(pg_stat_database_blks_read{{{I}}}[1h])))")],
         [(0, "red"), (0.9, "green")]),
    ]),
    ("Redis", [
        ("Bộ nhớ / maxmemory", "timeseries", "bytes",
         [("dùng", f"max(redis_memory_used_bytes{{{I}}})"), ("tối đa", f"max(redis_memory_max_bytes{{{I}}})")], None),
        ("Key theo db", "timeseries", "none", [("{{db}}", f"sum by (db) (redis_db_keys{{{I}}})")], None),
        ("Key bị đẩy ra / giây (volatile-ttl)", "timeseries", "ops",
         [("evicted", f"sum(rate(redis_evicted_keys_total{{{I}}}[$__rate_interval]))")], None),
        ("Client", "stat", "none", [("client", f"max(redis_connected_clients{{{I}}})")], None),
    ]),
    ("Qdrant", [
        ("Điểm theo collection", "timeseries", "none",
         [("{{id}}", f'max by (id) (collection_points{{{I},id=~"sict_.*"}})')], None),
        ("RAM Qdrant", "timeseries", "bytes", [("resident", f"max(memory_resident_bytes{{{I},job=\"sict/qdrant\"}})")], None),
        ("Hàng đợi cập nhật / tối ưu", "timeseries", "none",
         [("queue", f"sum(collection_update_queue_length{{{I}}})"), ("optimizing", f"sum(collection_running_optimizations{{{I}}})")], None),
    ]),
    ("Dịch vụ embed (ONNX int8)", [
        ("Độ trễ p95 theo loại", "timeseries", "s",
         [("{{kind}}", f"histogram_quantile(0.95, sum by (le, kind) (rate(sict_embed_request_duration_seconds_bucket{{{I}}}[$__rate_interval])))")], None),
        ("Mục nhúng/giây", "timeseries", "ops", [("{{kind}}", f"sum by (kind) (rate(sict_embed_items_total{{{I}}}[$__rate_interval]))")], None),
        ("Lỗi 1 giờ", "stat", "none", [("{{code}}", f"sum by (code) (increase(sict_embed_errors_total{{{I}}}[1h]))")], [(0, "green"), (1, "red")]),
    ]),
]


def panel(pid: int, title: str, kind: str, unit: str, targets, steps, x: int, y: int, w: int) -> dict:
    p = {"id": pid, "type": kind, "title": title, "datasource": DS, "gridPos": {"h": 7, "w": w, "x": x, "y": y},
         "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lg} for i, (lg, e) in enumerate(targets)],
         "fieldConfig": {"defaults": {"unit": unit}, "overrides": []}, "options": {}}
    if steps:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
            {"value": None if i == 0 else v, "color": c} for i, (v, c) in enumerate(steps)]}
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none"}
    return p


def build() -> dict:
    panels, pid, y = [], 1, 0
    for row_title, items in ROWS:
        panels.append({"id": pid, "type": "row", "title": row_title, "collapsed": False, "panels": [],
                       "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}})
        pid, y = pid + 1, y + 1
        w = 24 // len(items)
        for i, (title, kind, unit, targets, steps) in enumerate(items):
            panels.append(panel(pid, title, kind, unit, targets, steps, i * w, y, w))
            pid += 1
        y += 7
    return {
        "uid": UID, "title": "SICT — hệ thống", "tags": ["sict"], "timezone": "browser", "schemaVersion": 39,
        "refresh": "1m", "time": {"from": "now-24h", "to": "now"}, "panels": panels,
        "templating": {"list": [
            {"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Prometheus",
             "current": {}, "hide": 0},
            {"name": "instance", "type": "query", "datasource": DS, "label": "Máy", "multi": True, "includeAll": True,
             "query": {"query": 'label_values(up{job=~"sict/.*|integrations/node_exporter"}, instance)', "refId": "inst"},
             "refresh": 2, "current": {}, "hide": 0},
        ]},
        "description": "Sinh bởi scripts/grafana_dashboards.py — sửa script, không sửa trên giao diện (lần push sau ghi đè).",
    }


def env(path: Path, prefix: tuple[str, ...]) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1).startswith(prefix):
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def http(method: str, url: str, auth: str, body: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def check(e: dict[str, str], instances: str) -> int:
    """Chạy mọi truy vấn (thay $instance, $__rate_interval) trên Prometheus của stack: lỗi cú pháp = fail;
    rỗng = báo (VD embed chưa chạy), không fail."""
    tok = e.get("GCLOUD_READ_API_KEY") or e["GCLOUD_RW_API_KEY"]
    auth = "Basic " + b64encode(f"{e['GCLOUD_HOSTED_METRICS_ID']}:{tok}".encode()).decode()
    base = re.sub(r"/push$", "", e["GCLOUD_HOSTED_METRICS_URL"]) + "/api/v1/query?"
    bad = 0
    for row, items in ROWS:
        for title, _, _, targets, _ in items:
            for lg, expr in targets:
                q = expr.replace("$instance", instances).replace("$__rate_interval", "5m")
                code, body = http("GET", base + urllib.parse.urlencode({"query": q}), auth)
                if code != 200:
                    bad += 1
                    print(f"LỖI  {row} / {title}: HTTP {code} {str(body).replace(tok, '<REDACTED>')[:160]}")
                else:
                    n = len(body["data"]["result"])
                    print(f"{'ok ' if n else 'rỗng'} {row} / {title} [{lg}]: {n} chuỗi")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--instances", default=".+", help="regex instance cho --check")
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    a = ap.parse_args()
    dash = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dash, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"ghi {OUT.relative_to(ROOT)}: {sum(p['type'] != 'row' for p in dash['panels'])} panel")
    e = env(Path(a.env_file), ("GCLOUD_", "GRAFANA_"))
    if a.check and check(e, a.instances):
        sys.exit(1)
    if a.push:
        if not e.get("GRAFANA_URL") or not e.get("GRAFANA_SA_TOKEN", "").startswith("glsa_"):
            sys.exit("BLOCKER: cần GRAFANA_URL + GRAFANA_SA_TOKEN (service account Editor, glsa_…) trong .env")
        auth = "Bearer " + e["GRAFANA_SA_TOKEN"]
        base = e["GRAFANA_URL"].rstrip("/")
        code, folders = http("GET", base + "/api/folders", auth)
        if code != 200:
            sys.exit(f"không đọc được thư mục: HTTP {code}")
        fid = next((f["uid"] for f in folders if f["title"] == "SICT"), None)
        if not fid:
            code, f = http("POST", base + "/api/folders", auth, {"title": "SICT"})
            if code not in (200, 201):
                sys.exit(f"không tạo được thư mục SICT: HTTP {code} {f}")
            fid = f["uid"]
        code, r = http("POST", base + "/api/dashboards/db", auth,
                       {"dashboard": dash, "folderUid": fid, "overwrite": True, "message": "scripts/grafana_dashboards.py"})
        if code != 200:
            sys.exit(f"nạp dashboard lỗi: HTTP {code} {r}")
        print(f"đã nạp: {base}{r.get('url', '')}")


if __name__ == "__main__":
    main()
