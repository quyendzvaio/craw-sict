#!/usr/bin/env bash
# Áp cấu hình Grafana Alloy đã render lên MÁY ĐANG CHẠY script (chạy bằng root). Alloy phải ĐÃ CÀI (gói deb/rpm,
# alloy.service) — script không cài lại. Dùng chung cho máy dev (scripts/configure_alloy_local.sh) và VM thật
# (scripts/deploy_alloy_vm.sh chép script này lên rồi chạy qua ssh).
#
#   <dòng KEY=VALUE bí mật> | sudo scripts/alloy_apply.sh --config <config.alloy đã render> [--docker-logs]
#
# stdin = nội dung file bí mật (GCLOUD_RW_API_KEY=…, POSTGRES_EXPORTER_DSN=…) → /etc/alloy/secrets.env root:root 600,
# nạp bằng drop-in systemd (EnvironmentFile) — token không qua argv, không ra log. Các bước:
#  1. kiểm tra: root, alloy + alloy.service có sẵn, config không chứa token, `alloy fmt` hợp lệ
#  2. file bí mật + drop-in /etc/systemd/system/alloy.service.d/10-sict-secrets.conf
#  3. sao lưu /etc/alloy/config.alloy → config.alloy.bak.<thời điểm>, cài config mới (không đụng file khác)
#  4. cho user alloy đọc journal + /var/log (nhóm systemd-journal, adm); --docker-logs thêm nhóm docker
#  5. daemon-reload, enable + restart; không active sau 30 s → khôi phục bản sao lưu (rollback)
#  6. kiểm UI chỉ nghe loopback; in trạng thái, cảnh báo/lỗi gần nhất, số mẫu / dòng log đã gửi
set -euo pipefail
CONFIG="" DOCKER_LOGS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --docker-logs) DOCKER_LOGS=1; shift ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done
redact() { sed -E 's/glc_[A-Za-z0-9_=+\/-]+/<REDACTED>/g'; }
step() { echo "== $*"; }

step "1. kiểm tra trước"
[ "$(id -u)" = 0 ] || { echo "BLOCKER: cần chạy bằng root (sudo)" >&2; exit 3; }
[ -r "$CONFIG" ] || { echo "BLOCKER: không đọc được --config $CONFIG" >&2; exit 2; }
command -v alloy >/dev/null || { echo "BLOCKER: chưa cài alloy (script không tự cài)" >&2; exit 3; }
systemctl cat alloy.service >/dev/null 2>&1 || { echo "BLOCKER: không có alloy.service" >&2; exit 3; }
grep -q 'glc_' "$CONFIG" && { echo "BLOCKER: config chứa token — dừng" >&2; exit 3; }
alloy --version 2>&1 | head -1
alloy fmt "$CONFIG" >/dev/null && echo "alloy fmt: OK"
SECRETS="$(cat)"   # stdin
printf '%s\n' "$SECRETS" | grep -qE '^GCLOUD_RW_API_KEY=glc_.{20,}' \
  || { echo "BLOCKER: stdin không có GCLOUD_RW_API_KEY hợp lệ" >&2; exit 2; }

step "2. file bí mật + systemd drop-in"
install -d -m 755 /etc/alloy /etc/systemd/system/alloy.service.d
( umask 077; printf '%s\n' "$SECRETS" > /etc/alloy/secrets.env.new )
chown root:root /etc/alloy/secrets.env.new && chmod 600 /etc/alloy/secrets.env.new
mv /etc/alloy/secrets.env.new /etc/alloy/secrets.env
unset SECRETS
cat > /etc/systemd/system/alloy.service.d/10-sict-secrets.conf <<'EOF'
[Service]
# token Grafana Cloud (+ DSN Postgres ở máy role vm1) — root:root 600, systemd đọc trước khi hạ quyền về user alloy
EnvironmentFile=/etc/alloy/secrets.env
EOF
chmod 644 /etc/systemd/system/alloy.service.d/10-sict-secrets.conf
stat -c '%n %U:%G %a' /etc/alloy/secrets.env /etc/systemd/system/alloy.service.d/10-sict-secrets.conf

step "3. sao lưu + cài config"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP=""
if [ -f /etc/alloy/config.alloy ]; then
  BACKUP="/etc/alloy/config.alloy.bak.$TS"; cp -a /etc/alloy/config.alloy "$BACKUP"; echo "backup: $BACKUP"
fi
install -m 644 -o root -g root "$CONFIG" /etc/alloy/config.alloy

step "4. quyền đọc log cho user alloy"
GROUPS_ADD="adm,systemd-journal"; [ "$DOCKER_LOGS" = 1 ] && GROUPS_ADD="$GROUPS_ADD,docker"
usermod -aG "$GROUPS_ADD" alloy && id alloy

step "5. restart (rollback nếu không lên)"
systemctl daemon-reload
systemctl enable alloy >/dev/null 2>&1 || true
systemctl restart alloy
for _ in $(seq 1 30); do systemctl is-active -q alloy && break; sleep 1; done
if ! systemctl is-active -q alloy; then
  echo "alloy KHÔNG lên — log:"; journalctl -u alloy -n 20 --no-pager | redact
  if [ -n "$BACKUP" ]; then cp -a "$BACKUP" /etc/alloy/config.alloy; echo "đã khôi phục $BACKUP"; systemctl restart alloy || true; fi
  exit 4
fi
systemctl show alloy -p EnvironmentFiles --value

step "6. trạng thái + kiểm tra"
sleep 20
if ss -ltnH | awk '{print $4}' | grep -E ':12345$' | grep -vqE '^(127\.0\.0\.1|\[::1\]):'; then
  echo "CẢNH BÁO: UI Alloy (12345) đang nghe ra ngoài loopback — kiểm tra CUSTOM_ARGS trong /etc/default/alloy"
fi
systemctl --no-pager --lines=0 status alloy | head -4
echo "-- cảnh báo/lỗi 1 phút gần nhất:"
journalctl -u alloy --since "-1min" --no-pager | grep -iE 'level=(error|warn)' | redact | cut -c1-240 | tail -10 || true
echo "-- đã gửi:"
curl -s -m 5 127.0.0.1:12345/metrics | grep -E '^(prometheus_remote_storage_samples_(total|failed_total)|loki_write_sent_entries_total|alloy_component_controller_running_components)' | sed -E 's/\{.*health_type="([a-z]+)".*\}/{\1}/; s/\{[^}]*\}//'
