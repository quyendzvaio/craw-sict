"""Kiểm chứng dữ liệu ĐÃ VÀO Grafana Cloud (không chỉ "Alloy đã gửi"): truy vấn Prometheus (Mimir) + Loki của stack
theo nhãn instance. Đọc .env (URL, ID, token) trong tiến trình, KHÔNG in token. Exit 0 chỉ khi mọi instance có cả
metric lẫn log trong `--window`.

    python scripts/verify_gcloud.py --instance sict-vm1-sim --instance sict-vm2-sim
Token cần scope metrics:read + logs:read (GCLOUD_READ_API_KEY nếu có, không thì thử GCLOUD_RW_API_KEY).
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


def env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1).startswith("GCLOUD_"):
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def get(url: str, user: str, token: str, params: dict) -> tuple[int, dict | str]:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={
        "Authorization": "Basic " + b64encode(f"{user}:{token}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        return e.code, body.replace(token, "<REDACTED>")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--instance", action="append", required=True)
    ap.add_argument("--window", default="10m")
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    a = ap.parse_args()
    e = env(Path(a.env_file))
    token = e.get("GCLOUD_READ_API_KEY") or e.get("GCLOUD_RW_API_KEY", "")
    if not token:
        sys.exit("thiếu token trong .env")
    prom = re.sub(r"/push$", "", e["GCLOUD_HOSTED_METRICS_URL"]) + "/api/v1/query"
    loki = re.sub(r"/loki/api/v1/push$", "", e["GCLOUD_HOSTED_LOGS_URL"]) + "/loki/api/v1/query"
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in a.instance):   # đưa thẳng vào regex PromQL/LogQL
        sys.exit("instance chỉ gồm chữ, số, _ -")
    sel = "|".join(a.instance)
    ok = True
    report: dict = {"metrics": {}, "logs": {}}

    q = f'count by (instance, job) ({{instance=~"{sel}"}})'
    code, body = get(prom, e["GCLOUD_HOSTED_METRICS_ID"], token, {"query": q})
    if code != 200:
        print(f"Prometheus query HTTP {code}: {body}")
        ok = False
    else:
        for r in body["data"]["result"]:
            report["metrics"].setdefault(r["metric"].get("instance"), {})[r["metric"].get("job")] = int(float(r["value"][1]))
        code2, up = get(prom, e["GCLOUD_HOSTED_METRICS_ID"], token, {"query": f'up{{instance=~"{sel}"}}'})
        report["up"] = {f'{r["metric"]["instance"]}/{r["metric"]["job"]}': r["value"][1]
                        for r in (up["data"]["result"] if code2 == 200 else [])}

    q = f'sum by (instance, job) (count_over_time({{instance=~"{sel}"}}[{a.window}]))'
    code, body = get(loki, e["GCLOUD_HOSTED_LOGS_ID"], token, {"query": q})
    if code != 200:
        print(f"Loki query HTTP {code}: {body}")
        ok = False
    else:
        for r in body["data"]["result"]:
            report["logs"].setdefault(r["metric"].get("instance"), {})[r["metric"].get("job")] = int(float(r["value"][1]))

    print(json.dumps(report, ensure_ascii=False, indent=1))
    for i in a.instance:
        has_m, has_l = bool(report["metrics"].get(i)), bool(report["logs"].get(i))
        print(f"{i}: metrics {'OK' if has_m else 'KHÔNG CÓ'} ({sum(report['metrics'].get(i, {}).values())} chuỗi), "
              f"logs {'OK' if has_l else 'KHÔNG CÓ'} ({sum(report['logs'].get(i, {}).values())} dòng/{a.window})")
        ok &= has_m and has_l
    print("VERIFIED" if ok else "CHƯA XÁC NHẬN")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
