#!/usr/bin/env bash
# Cấu hình Alloy ĐÃ CÀI trên máy này (systemd) gửi lên Grafana Cloud, giá trị từ .env — chạy bằng user thường,
# script tự gọi sudo cho phần cần root (scripts/alloy_apply.sh). Không in token.
#
#   scripts/configure_alloy_local.sh                        # role vm1, instance sict-vm1-local, có log Docker
#   ROLE=vm2 INSTANCE=sict-vm2-local scripts/configure_alloy_local.sh
#   NO_DOCKER_LOGS=1 scripts/configure_alloy_local.sh       # không cho user alloy vào nhóm docker
#
# Máy dev đang chạy Alloy giả lập vm1 trong Docker (scripts/monitor_sim.sh) cũng nghe 127.0.0.1:12345 → script dừng
# container đó trước (vm2 giả lập giữ nguyên, cổng 12346).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
ROLE="${ROLE:-vm1}"
INSTANCE="${INSTANCE:-sict-$ROLE-local}"
OUT="$ROOT/monitor/rendered/$INSTANCE/config.alloy"
EXTRA=(); DOCKER_FLAG=(--docker-logs)
[ "${NO_DOCKER_LOGS:-0}" = 1 ] && { EXTRA+=(--no-docker-logs); DOCKER_FLAG=(); }

# journal: /var/log/journal (lưu bền) nếu thật sự là thư mục, không thì /run/log/journal (máy dev: symlink hỏng → /data/journal)
JOURNAL=/var/log/journal; [ -d "$(readlink -f /var/log/journal)" ] || JOURNAL=/run/log/journal
python3 "$ROOT/scripts/render_alloy.py" --env-file "$ENV_FILE" --role "$ROLE" --instance "$INSTANCE" "${EXTRA[@]}" \
  --journal-path "$JOURNAL" --out "$OUT"

if docker inspect -f '{{.State.Running}}' sict-alloy-vm1-sim 2>/dev/null | grep -q true; then
  echo "dừng Alloy giả lập vm1 (trùng cổng 12345)"; docker stop sict-alloy-vm1-sim >/dev/null
  docker update --restart=no sict-alloy-vm1-sim >/dev/null
fi

# dòng bí mật đi qua pipe → stdin của alloy_apply.sh (không argv, không file tạm ngoài /etc/alloy)
{ grep -E '^GCLOUD_RW_API_KEY=' "$ENV_FILE" | tail -1 | sed -E "s/^GCLOUD_RW_API_KEY=[\"']?([^\"']*)[\"']?\$/GCLOUD_RW_API_KEY=\1/"
  [ "$ROLE" = vm1 ] && echo "POSTGRES_EXPORTER_DSN=${POSTGRES_EXPORTER_DSN:-postgresql://sict:sict@localhost:5432/sict_crawler?sslmode=disable}"
} | sudo bash "$ROOT/scripts/alloy_apply.sh" --config "$OUT" "${DOCKER_FLAG[@]}"

echo "== kiểm chứng phía Grafana Cloud (đợi ~2 phút cho lượt gửi đầu):"
echo "   python3 scripts/verify_gcloud.py --instance $INSTANCE --instance sict-vm2-sim"
