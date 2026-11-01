#!/usr/bin/env bash
# D12: mật khẩu / API key của Postgres, Redis, Qdrant trong ./.env của VM. Không bao giờ in giá trị bí mật.
#
#   scripts/store_auth.sh init-env        # thêm POSTGRES_PASSWORD / REDIS_PASSWORD / QDRANT_API_KEY ngẫu nhiên nếu
#                                         # .env chưa có (VM1); chép đúng các giá trị đó sang .env VM2
#   scripts/store_auth.sh check vm1|vm2   # CD chạy trước `up`: thiếu / yếu → exit 1
#   scripts/store_auth.sh sync-pg         # VM1: đặt mật khẩu user sict = POSTGRES_PASSWORD trong DB đang chạy
#                                         # (POSTGRES_PASSWORD của image chỉ dùng lúc khởi tạo data dir)
#
# Bí mật chỉ gồm [A-Za-z0-9_-], dài ≥ 24 ký tự: nằm được trong POSTGRES_DSN / REDIS_URL mà không phải mã hoá URL.
set -euo pipefail

ENV_FILE=${ENV_FILE:-.env}

val() {   # giá trị của khoá trong .env (dòng cuối thắng như docker compose), bỏ nháy bao ngoài; không source file
  local v
  v=$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  printf '%s' "$v"
}

strong() { [[ "$1" =~ ^[A-Za-z0-9_-]{24,}$ ]]; }

url_password() { [[ "$1" =~ ://[^:/@]*:([^@]*)@ ]] && printf '%s' "${BASH_REMATCH[1]}" || true; }

fail=0
need() {   # need <tên hiển thị> <giá trị>
  if ! strong "$2"; then
    echo "D12: $1 thiếu hoặc yếu (cần ≥ 24 ký tự A-Z a-z 0-9 _ -)"; fail=1
  fi
}

check() {
  [ -f "$ENV_FILE" ] || { echo "D12: không thấy $ENV_FILE"; exit 1; }
  case "${1:-}" in
    vm1)
      need POSTGRES_PASSWORD "$(val POSTGRES_PASSWORD)"
      need REDIS_PASSWORD "$(val REDIS_PASSWORD)"
      need QDRANT_API_KEY "$(val QDRANT_API_KEY)"
      local bind; bind=$(val STORE_BIND)
      if [ -z "$bind" ] || [ "$bind" = 127.0.0.1 ]; then
        echo "D12: STORE_BIND chưa đặt → store chỉ nghe 127.0.0.1, VM2 không tới được (đặt 0.0.0.0 + SG chỉ cho VM2)"; fail=1
      fi ;;
    vm2)
      need "mật khẩu trong POSTGRES_DSN" "$(url_password "$(val POSTGRES_DSN)")"
      need "mật khẩu trong REDIS_URL" "$(url_password "$(val REDIS_URL)")"
      need QDRANT_API_KEY "$(val QDRANT_API_KEY)"
      [ -n "$(val QDRANT_HOST)" ] || { echo "D12: QDRANT_HOST chưa trỏ về VM1"; fail=1; } ;;
    *) echo "dùng: $0 check vm1|vm2"; exit 2 ;;
  esac
  [ $fail = 0 ] && echo "D12: .env $1 đủ mật khẩu / API key" || exit 1
}

init_env() {
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  local k added=""
  for k in POSTGRES_PASSWORD REDIS_PASSWORD QDRANT_API_KEY; do
    if ! strong "$(val "$k")"; then
      grep -vE "^$k=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
      printf '%s=%s\n' "$k" "$(openssl rand -hex 24)" >> "$ENV_FILE.tmp"
      cat "$ENV_FILE.tmp" > "$ENV_FILE"; rm -f "$ENV_FILE.tmp"
      added="$added $k"
    fi
  done
  grep -qE '^STORE_BIND=' "$ENV_FILE" || { echo "STORE_BIND=0.0.0.0" >> "$ENV_FILE"; added="$added STORE_BIND"; }
  echo "D12: đã thêm vào $ENV_FILE:${added:- (không có gì, đã đủ)}"
  echo "Sau đó: VM1 \`scripts/store_auth.sh sync-pg\` rồi \`docker compose up -d\`; .env VM2 cần cùng giá trị trong"
  echo "POSTGRES_DSN (sict:<POSTGRES_PASSWORD>@), REDIS_URL (redis://:<REDIS_PASSWORD>@) và QDRANT_API_KEY."
}

sync_pg() {
  local pw; pw=$(val POSTGRES_PASSWORD)
  strong "$pw" || { echo "D12: POSTGRES_PASSWORD thiếu hoặc yếu trong $ENV_FILE"; exit 1; }
  # socket trong container dùng trust → đổi được mà không cần mật khẩu cũ. Mật khẩu không nằm trên dòng lệnh của
  # tiến trình nào (ps của máy thấy cả tiến trình trong container): `-e PGPW` không kèm giá trị lấy từ môi trường,
  # trong container `printf` (lệnh có sẵn của sh) đưa `\set pw …` qua stdin cho psql; :'pw' tự đặt nháy SQL.
  export PGPW="$pw"
  docker compose exec -T -e PGPW postgres sh -c \
    'printf "\\set pw %s\nALTER USER sict PASSWORD :'"'"'pw'"'"';\n" "$PGPW" | psql -X -q -v ON_ERROR_STOP=1 -U sict -d sict_crawler'
  echo "D12: đã đặt mật khẩu user sict theo POSTGRES_PASSWORD"
}

case "${1:-}" in
  check) check "${2:-}" ;;
  init-env) init_env ;;
  sync-pg) sync_pg ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
