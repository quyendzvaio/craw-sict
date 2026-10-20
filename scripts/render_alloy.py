"""Render cấu hình Grafana Alloy (monitor/alloy/*.alloy) cho 1 máy → 1 file config.alloy (D13, Grafana Cloud).

Giá trị lấy từ .env (KHÔNG đọc token: file render dùng sys.env("GCLOUD_RW_API_KEY") lúc Alloy chạy):
  GCLOUD_HOSTED_METRICS_URL, GCLOUD_HOSTED_METRICS_ID, GCLOUD_SCRAPE_INTERVAL, GCLOUD_HOSTED_LOGS_URL, GCLOUD_HOSTED_LOGS_ID
Role: vm1 = base + Postgres/Redis/Qdrant/API (monitor/alloy/vm1-services.alloy); vm2 = base + embed (vm2-services.alloy).
`--host-prefix /host` khi Alloy chạy trong Docker và /proc, /sys, / của máy được mount vào /host/… (giả lập local).

    python scripts/render_alloy.py --role vm1 --instance sict-vm1 --out /tmp/vm1/config.alloy
    python scripts/render_alloy.py --role vm1 --instance sict-vm1-sim --host-prefix /host --out monitor/rendered/vm1-sim/config.alloy
"""
import argparse
import re
import sys
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["GCLOUD_HOSTED_METRICS_URL", "GCLOUD_HOSTED_METRICS_ID", "GCLOUD_SCRAPE_INTERVAL",
            "GCLOUD_HOSTED_LOGS_URL", "GCLOUD_HOSTED_LOGS_ID"]
PARTS = {"vm1": ["base.alloy", "vm1-services.alloy"], "vm2": ["base.alloy", "vm2-services.alloy"]}
DOCKER_PART = "docker-logs.alloy"   # chỉ thêm khi docker_logs=True (cần quyền đọc docker.sock)
DOCKER_NAMES = {"vm1": ["craw-sict-"],                                   # postgres/redis/qdrant/api/nginx
                "vm2": ["craw-sict-cron", "craw-sict-svc-ocr", "craw-sict-obscura", "craw-sict-embed",
                        "sict-worker", "sict-embed"]}


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE (bỏ comment, nháy). Chỉ trả các khoá REQUIRED — không giữ giá trị khác (token, mật khẩu) trong bộ nhớ."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1) in REQUIRED:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def render(role: str, instance: str, env: dict[str, str], host_prefix: str = "", journal_path: str = "/var/log/journal",
           log_glob: str = "/var/log/{syslog,messages,*.log}", docker_names: list[str] | None = None,
           redis_addr: str = "localhost:6379", qdrant_addr: str = "localhost:6333", docker_logs: bool = True,
           api_addr: str = "localhost:8003", embed_addr: str = "localhost:8090") -> str:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit(f"thiếu biến trong .env: {', '.join(missing)}")
    if not re.fullmatch(r"\d+[smh]", env["GCLOUD_SCRAPE_INTERVAL"]):
        raise SystemExit("GCLOUD_SCRAPE_INTERVAL phải dạng 60s / 1m")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", instance):
        raise SystemExit("instance chỉ gồm chữ, số, . _ -")
    for k in ("GCLOUD_HOSTED_METRICS_URL", "GCLOUD_HOSTED_LOGS_URL"):
        if not env[k].startswith("https://"):
            raise SystemExit(f"{k} phải là https://")
    names = docker_names if docker_names is not None else DOCKER_NAMES[role]
    values = {**env, "ROLE": role, "INSTANCE": instance,
              "HOST_PROC": f"{host_prefix}/proc", "HOST_SYS": f"{host_prefix}/sys", "HOST_ROOT": f"{host_prefix}/root" if host_prefix else "/",
              "JOURNAL_PATH": journal_path, "LOG_GLOB": log_glob,
              "DOCKER_NAMES": ", ".join(f'"{n}"' for n in names),
              "REDIS_ADDR": redis_addr, "QDRANT_ADDR": qdrant_addr, "API_ADDR": api_addr, "EMBED_ADDR": embed_addr}
    head = f"// RENDERED bởi scripts/render_alloy.py — role={role} instance={instance}. Sửa template trong monitor/alloy/.\n\n"
    parts = PARTS[role] + ([DOCKER_PART] if docker_logs else [])
    return head + "\n".join(Template((ROOT / "monitor" / "alloy" / p).read_text(encoding="utf-8")).substitute(values)
                            for p in parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--role", required=True, choices=sorted(PARTS))
    ap.add_argument("--instance", required=True, help="nhãn instance duy nhất trong Grafana Cloud, VD sict-vm1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--host-prefix", default="", help="'/host' khi chạy trong Docker có mount /proc /sys / của máy")
    ap.add_argument("--journal-path", default="/var/log/journal")
    ap.add_argument("--log-glob", default="/var/log/{syslog,messages,*.log}")
    ap.add_argument("--docker-names", default=None, help="danh sách tên container (khớp chuỗi con), phẩy ngăn cách")
    ap.add_argument("--redis-addr", default="localhost:6379")
    ap.add_argument("--qdrant-addr", default="localhost:6333")
    ap.add_argument("--no-docker-logs", action="store_true", help="bỏ phần log container (không cần quyền docker.sock)")
    ap.add_argument("--api-addr", default="localhost:8003", help="VM1: API (GET /metrics)")
    ap.add_argument("--embed-addr", default="localhost:8090", help="VM2: dịch vụ embed (= EMBED_BIND:8090)")
    a = ap.parse_args()
    text = render(a.role, a.instance, read_env(Path(a.env_file)), a.host_prefix, a.journal_path, a.log_glob,
                  a.docker_names.split(",") if a.docker_names else None, a.redis_addr, a.qdrant_addr,
                  docker_logs=not a.no_docker_logs, api_addr=a.api_addr, embed_addr=a.embed_addr)
    if "glc_" in text:   # phòng thủ: token không bao giờ được nằm trong file render
        raise SystemExit("phát hiện chuỗi token trong cấu hình render — dừng")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"rendered {out} (role={a.role}, instance={a.instance})", file=sys.stderr)


if __name__ == "__main__":
    main()
