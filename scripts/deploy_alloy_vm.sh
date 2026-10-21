#!/usr/bin/env bash
# Cấu hình Grafana Alloy (ĐÃ CÀI SẴN, systemd) trên 1 VM thật → Grafana Cloud (D13). Phần chạy bằng root trên VM là
# scripts/alloy_apply.sh (cùng script với máy dev): sao lưu config, /etc/alloy/secrets.env 600 + drop-in EnvironmentFile,
# alloy fmt, restart + rollback, kiểm UI chỉ nghe loopback. CHƯA CHẠY trên VM nào (2026-10-21: VM1/VM2 stopped).
#
#   scripts/deploy_alloy_vm.sh --vm vm1 --host <IP public> [--key ~/.ssh/vm1.pem] [--docker-logs] [--dry-run]
#   scripts/deploy_alloy_vm.sh --vm vm2 --host <IP public>
# Sau cả 2 VM: python3 scripts/verify_gcloud.py --instance sict-vm1 --instance sict-vm2
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
VM="" HOST="" KEY="" DOCKER_LOGS=0 DRY=0 USER_SSH="${SSH_USER:-ubuntu}"
while [ $# -gt 0 ]; do
  case "$1" in
    --vm) VM="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --docker-logs) DOCKER_LOGS=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done
case "$VM" in vm1|vm2) ;; *) echo "--vm vm1|vm2" >&2; exit 2 ;; esac
[ -n "$HOST" ] || { echo "BLOCKER: thiếu --host (IP public của $VM; VM đang stopped thì chưa có IP)" >&2; exit 2; }
KEY="${KEY:-$HOME/.ssh/$VM.pem}"
[ -r "$KEY" ] || { echo "BLOCKER: không đọc được khoá SSH $KEY" >&2; exit 2; }
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$USER_SSH@$HOST")
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

EXTRA=(); [ "$DOCKER_LOGS" = 1 ] || EXTRA+=(--no-docker-logs)
python3 "$ROOT/scripts/render_alloy.py" --env-file "$ENV_FILE" --role "$VM" --instance "sict-$VM" "${EXTRA[@]}" --out "$TMP/config.alloy"
"${SSH[@]}" 'mkdir -p /tmp/sict-alloy && cat > /tmp/sict-alloy/config.alloy' < "$TMP/config.alloy"
"${SSH[@]}" 'cat > /tmp/sict-alloy/alloy_apply.sh' < "$ROOT/scripts/alloy_apply.sh"
if [ "$DRY" = 1 ]; then   # chỉ kiểm: có alloy + cú pháp hợp lệ, không đổi gì
  "${SSH[@]}" 'command -v alloy && systemctl cat alloy.service >/dev/null && alloy fmt /tmp/sict-alloy/config.alloy >/dev/null && echo "dry-run OK: alloy có sẵn, config hợp lệ"; rm -rf /tmp/sict-alloy'
  exit 0
fi
FLAGS=""; [ "$DOCKER_LOGS" = 1 ] && FLAGS="--docker-logs"
# bí mật qua stdin của ssh → stdin của alloy_apply.sh (printf là builtin: token không vào danh sách tiến trình)
{ printf 'GCLOUD_RW_API_KEY=%s\n' "$(grep -E '^GCLOUD_RW_API_KEY=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"'")"
  [ "$VM" = vm1 ] && printf 'POSTGRES_EXPORTER_DSN=%s\n' "${POSTGRES_EXPORTER_DSN:-postgresql://sict:sict@localhost:5432/sict_crawler?sslmode=disable}"
} | "${SSH[@]}" "sudo bash /tmp/sict-alloy/alloy_apply.sh --config /tmp/sict-alloy/config.alloy $FLAGS; rc=\$?; rm -rf /tmp/sict-alloy; exit \$rc" \
  | sed -E 's/glc_[A-Za-z0-9_=+\/-]+/<REDACTED>/g'
