#!/usr/bin/env bash
# Giả lập 2 VM gửi metric + log lên Grafana Cloud từ máy dev (monitor/docker-compose.sim.yml).
#   scripts/monitor_sim.sh up       render config + file bí mật (600, ngoài repo) + chạy 2 Alloy
#   scripts/monitor_sim.sh restart  render lại + tạo lại 2 container (sau khi sửa monitor/alloy/*.alloy)
#   scripts/monitor_sim.sh status   trạng thái 2 Alloy: component lỗi, số mẫu / dòng log đã gửi thành công
#   scripts/monitor_sim.sh down     dừng + xoá container (giữ file bí mật; xoá bằng tay nếu muốn)
# Không bao giờ in token. Biến lấy từ .env: GCLOUD_HOSTED_{METRICS,LOGS}_{URL,ID}, GCLOUD_SCRAPE_INTERVAL, GCLOUD_RW_API_KEY.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
export ALLOY_SECRETS="${ALLOY_SECRETS:-/DATA/craw-sict-data/monitor-sim/secrets.env}"
COMPOSE=(docker compose -p sict-monitor-sim -f "$ROOT/monitor/docker-compose.sim.yml")
PY="${PYTHON:-python3}"

secrets() {
  # chép token từ .env sang file bí mật mà không đi qua stdout / argv
  mkdir -p "$(dirname "$ALLOY_SECRETS")"
  ( umask 077
    { grep -E '^GCLOUD_RW_API_KEY=' "$ENV_FILE" | tail -1 | sed -E "s/^GCLOUD_RW_API_KEY=[\"']?([^\"']*)[\"']?\$/GCLOUD_RW_API_KEY=\1/"
      echo "POSTGRES_EXPORTER_DSN=${POSTGRES_EXPORTER_DSN:-postgresql://sict:sict@localhost:5432/sict_crawler?sslmode=disable}"
    } > "$ALLOY_SECRETS.tmp" )
  grep -qE '^GCLOUD_RW_API_KEY=glc_.{20,}' "$ALLOY_SECRETS.tmp" || { rm -f "$ALLOY_SECRETS.tmp"; echo "GCLOUD_RW_API_KEY thiếu / sai dạng trong $ENV_FILE" >&2; exit 1; }
  mv "$ALLOY_SECRETS.tmp" "$ALLOY_SECRETS"; chmod 600 "$ALLOY_SECRETS"
  echo "file bí mật: $ALLOY_SECRETS ($(stat -c '%a %U' "$ALLOY_SECRETS"))"
}

render() {
  "$PY" "$ROOT/scripts/render_alloy.py" --env-file "$ENV_FILE" --role vm1 --instance sict-vm1-sim --host-prefix /host \
    --journal-path /run/log/journal --out "$ROOT/monitor/rendered/vm1-sim/config.alloy"
  "$PY" "$ROOT/scripts/render_alloy.py" --env-file "$ENV_FILE" --role vm2 --instance sict-vm2-sim --host-prefix /host \
    --journal-path /run/log/journal --out "$ROOT/monitor/rendered/vm2-sim/config.alloy"
  for v in vm1 vm2; do   # kiểm cú pháp bằng chính binary Alloy (alloy fmt) trước khi chạy
    docker run --rm -v "$ROOT/monitor/rendered/$v-sim/config.alloy:/c.alloy:ro" grafana/alloy:v1.20.1 fmt /c.alloy >/dev/null \
      && echo "alloy fmt $v-sim: OK"
  done
}

status() {
  for p in "vm1 12345" "vm2 12346"; do
    set -- $p
    echo "=== $1-sim (127.0.0.1:$2) — $(docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' sict-alloy-$1-sim 2>/dev/null || echo 'không chạy')"
    curl -sf -m 5 "127.0.0.1:$2/-/ready" >/dev/null && echo "ready" || echo "CHƯA ready"
    curl -s -m 10 "127.0.0.1:$2/metrics" | grep -E '^(prometheus_remote_storage_samples_total|prometheus_remote_storage_samples_failed_total|prometheus_remote_storage_samples_dropped_total|loki_write_sent_entries_total|loki_write_dropped_entries_total|alloy_component_controller_running_components|loki_write_request_duration_seconds_count|prometheus_remote_storage_sent_batch_duration_seconds_count)\b' \
      | sed -E 's/url="[^"]*"//' | sort | head -20
  done
}

case "${1:-}" in
  up) secrets; render; "${COMPOSE[@]}" up -d; sleep 5; status ;;
  render) render ;;
  restart) render; "${COMPOSE[@]}" up -d --force-recreate; sleep 5; status ;;   # sau khi sửa template
  status) status ;;
  down) "${COMPOSE[@]}" down ;;
  *) echo "dùng: $0 up|render|restart|status|down" >&2; exit 2 ;;
esac
