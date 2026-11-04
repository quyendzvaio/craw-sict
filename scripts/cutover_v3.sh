#!/usr/bin/env bash
# Cutover production sang index v3 (D8/D16/D22: jina-clip-v2 1024-d, 3 collection *_v3, embed int8 tự host trên VM2)
# — từng bước do người vận hành gọi, KHÔNG gộp tự động. Collection cũ (v1/v2) và cột cũ của Postgres không bị đụng
# → rollback = đổi QDRANT_COLLECTION_* trong .env rồi `docker compose up -d api`.
#
#   (máy dev) ./scripts/cutover_v3.sh pack              đóng gói artifact v3 + HTML snapshot (~330 MB) → chép lên VM2
#   (VM1|VM2) ./scripts/cutover_v3.sh env-check vm1|vm2 .env có đủ biến v3 (chỉ in TÊN biến) + store_auth.sh check
#   (VM1)     ./scripts/cutover_v3.sh backup            pg_dump + snapshot Qdrant các collection đang có → ~/backup-v3-<ngày>/
#   (VM2)     ./scripts/cutover_v3.sh load              nạp PG + *_v3 bằng IMAGE WORKER (loader gọi graphsync cần
#                                                       underthesea, chỉ image worker có), KHÔNG build, KHÔNG upload media
#   (dev/VM1) ./scripts/cutover_v3.sh verify            golden_v1 / golden_v2 / list_v2 + accept_api qua HTTP (API=…)
#   (máy có snapshot + AWS) CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v3.sh upload-media
#                                                       ~1,9 GB ảnh + frame lên S3 (key media/<sha256>.<ext>, có rồi thì bỏ qua)
#   (VM1)     ./scripts/cutover_v3.sh rollback          in hướng dẫn; CONFIRM_ROLLBACK=yes [ROLLBACK_TO=v2|v1] thì làm thật
#
# Thứ tự production (docs/deploy_v3.md): CD vm2 + deploy_embed → upload-media → backup (VM1) → load (VM2, cron đang dừng)
# → CD vm1 (API đọc .env v3) → verify → bật cron. Deploy code = workflow CD (D10: VM chỉ pull image GHCR, không build).
# Ảnh v3 hiện qua /media/s/<sha256> chỉ sau upload-media → upload TRƯỚC khi API v3 lên thì không có ảnh hỏng.
# Không bao giờ in giá trị .env (mật khẩu / key) — chỉ tên biến.
set -euo pipefail

SNAP=${SNAP:-_snapshot/2026-10-12}
ART=${ART:-$SNAP/artifacts/v3-jina-clip-v2}
PKG=${PKG:-_cutover/v3-load.tgz}
API=${API:-http://localhost:8003}
ENV_FILE=${ENV_FILE:-.env}
WORKER_COMPOSE=${WORKER_COMPOSE:-docker-compose.worker.yml}
cd "$(dirname "$0")/.."
export ENV_FILE

val() {   # giá trị của khoá trong ENV_FILE (dòng cuối thắng như docker compose); không source file, không in ra
  local v
  v=$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  printf '%s' "$v"
}

say() { printf '\n→ %s\n' "$*"; }
run() { printf '$ %s\n' "$*"; "$@"; }

env_check() {   # chỉ báo TÊN biến thiếu / sai; giá trị không bao giờ in
  local role=$1 fail=0 k
  [ -f "$ENV_FILE" ] || { echo "v3: không thấy $ENV_FILE"; exit 1; }
  expect() {   # expect <biến> <giá trị mong đợi>
    if [ "$(val "$1")" != "$2" ]; then echo "v3: $1 phải là $2"; fail=1; fi
  }
  nonempty() { [ -n "$(val "$1")" ] || { echo "v3: $1 chưa đặt"; fail=1; }; }
  expect EMBED_BACKEND api
  nonempty EMBED_API_BASE
  expect EMBED_MODEL jina-clip-v2
  expect EMBED_DIM 1024
  expect QDRANT_COLLECTION_TEXT sict_text_v3
  expect QDRANT_COLLECTION_IMAGE sict_image_v3
  expect QDRANT_COLLECTION_OCR sict_ocr_v3
  expect QDRANT_TEXT_DIM 1024
  expect QDRANT_IMAGE_DIM 1024
  expect CHUNK_STRUCTURED 1
  case "$role" in
    vm1)
      nonempty EMBED_QUERY_IMAGE_SIZE
      [ "$(val API_MEM_LIMIT)" = 600m ] || echo "v3: gợi ý API_MEM_LIMIT=600m (API backend api chỉ ~200 MiB; trả RAM cho VM1)"
      ;;
    vm2)
      nonempty EMBED_BIND
      ;;
    *) echo "dùng: $0 env-check vm1|vm2"; exit 2 ;;
  esac
  [ $fail = 0 ] && echo "v3: $ENV_FILE $role đủ biến index v3" || exit 1
  # D12: mật khẩu / API key store (store_auth.sh cũng đọc ENV_FILE, không in giá trị)
  bash scripts/store_auth.sh check "$role"
}

qdrant_curl() {   # qdrant_curl <đường dẫn> [tham số curl…]; API key qua header đọc từ ENV_FILE, không lên dòng lệnh
  local path=$1; shift
  local qkey; qkey="$(val QDRANT_API_KEY)"
  curl -sf -H @<(printf 'api-key: %s' "$qkey") "http://localhost:6333$path" "$@"
}

case "${1:-}" in
  pack)
    [ -d "$ART" ] || { echo "không thấy artifact $ART (ART=…)"; exit 1; }
    mkdir -p "$(dirname "$PKG")"
    run tar czf "$PKG" --exclude="$ART/ocr_cache.jsonl" "$ART" "$SNAP/html"
    ls -lh "$PKG"
    say "chép $PKG lên VM2 (~/craw-sict/, nơi chạy loader bằng image worker): scp $PKG ubuntu@<IP-VM2>:craw-sict/"
    say "trên VM2: tar xzf $(basename "$PKG") && ./scripts/cutover_v3.sh env-check vm2 && ./scripts/cutover_v3.sh load"
    ;;
  env-check)
    env_check "${2:-}"
    ;;
  backup)
    d=~/backup-v3-$(date +%Y%m%d-%H%M)
    mkdir -p "$d"
    say "pg_dump → $d/sict_crawler.dump"
    docker compose exec -T postgres pg_dump -U sict -Fc sict_crawler > "$d/sict_crawler.dump"
    say "snapshot Qdrant mọi collection đang có (v1 / v2 / v3 nếu đã nạp) → volume qdata (/qdrant/storage/snapshots)"
    cols="$(qdrant_curl /collections | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin)["result"]["collections"]))' 2>/dev/null || true)"
    [ -n "$cols" ] || echo "cảnh báo: không liệt kê được collection (Qdrant chưa chạy hoặc sai QDRANT_API_KEY trong $ENV_FILE)"
    for c in $cols; do
      qdrant_curl "/collections/$c/snapshots" -X POST -o "$d/qdrant-$c.json" \
        && echo "  snapshot $c: ok" || echo "cảnh báo: không tạo được snapshot Qdrant $c"
    done
    ls -lh "$d"
    say "kiểm: pg_restore --list $d/sict_crawler.dump | head; chép $d ra ngoài VM1 (máy dev / S3 khi được phép)"
    ;;
  load)
    # Chạy trên VM2: image worker (group graph: underthesea cho graphsync.rebuild mà loader gọi sau khi ghi point).
    # Không build trên VM (D10): image = WORKER_IMAGE trong .env do CD ghi (ghcr.io/…:<sha>); chưa có thì pull.
    [ -f "$PKG" ] && { say "giải nén $PKG"; run tar xzf "$PKG"; }
    [ -d "$ART" ] || { echo "không thấy artifact $ART (chạy pack ở máy dev rồi chép lên, hoặc ART=…)"; exit 1; }
    env_check vm2
    img="$(val WORKER_IMAGE)"
    if [ -z "$img" ]; then
      echo "WORKER_IMAGE chưa có trong $ENV_FILE (CD vm2 ghi vào, D10) — nạp bằng image local sict-worker:latest? Đặt WORKER_IMAGE=… trong $ENV_FILE để chắc chắn." >&2
      img=sict-worker:latest
    fi
    docker image inspect "$img" >/dev/null 2>&1 || run docker compose -f "$WORKER_COMPOSE" pull cron
    if docker compose -f "$WORKER_COMPOSE" ps --status running cron 2>/dev/null | grep -q cron; then
      if [ "${ALLOW_CRON_RUNNING:-}" != "yes" ]; then
        echo "cron đang chạy — dừng trước để không incremental song song với loader: docker compose -f $WORKER_COMPOSE stop cron (hoặc ALLOW_CRON_RUNNING=yes)" >&2
        exit 2
      fi
    fi
    say "nạp PG + Qdrant *_v3 bằng image worker ($img); loader so manifest với EMBED_*/QDRANT_* trong $ENV_FILE, lệch → SystemExit"
    say "thời gian ước lượng: nạp ~3 phút + context ảnh 1–2 phút + graph khái niệm lần đầu ~13 phút / 1 lõi"
    extra=()
    [ "${LOAD_RECREATE:-}" = "yes" ] && extra+=(--recreate)
    run docker compose -f "$WORKER_COMPOSE" run --rm --no-deps -v "$PWD/_snapshot:/app/_snapshot:ro" cron \
      python -m sict_crawler.pipeline.loader --artifacts "$ART" --skip-media-upload "${extra[@]}"
    say "kiểm: log loader 'ok: true', points = expected; rồi CD vm1 (API đọc .env v3) → verify → bật cron"
    ;;
  verify)
    say "golden_v1 (mong: recall@5 ≥ 0,985, MRR ≥ 0,93)"
    run python3 scripts/eval_api.py --api "$API" --golden eval/golden_v1.jsonl
    say "golden_v2 (mong: url_hit ≥ 70/80)"
    run python3 scripts/eval_api.py --api "$API" --golden eval/golden_v2.jsonl
    say "list_v2 (mong: coverage ≥ 0,90, sufficient_acc ≥ 0,94)"
    run python3 scripts/eval_api.py --api "$API" --list eval/list_v2.jsonl
    say "nghiệm thu U1–U6 + trích dẫn + /metrics (exit 1 khi FAIL)"
    run python3 scripts/accept_api.py --api "$API"
    ;;
  upload-media)
    if [ "${CONFIRM_S3_UPLOAD:-}" != "yes" ]; then
      echo "Từ chối: upload S3 cần CONFIRM_S3_UPLOAD=yes (chỉ chạy khi chủ dự án đã cho phép)." >&2
      exit 2
    fi
    run python -m sict_crawler.pipeline.loader --artifacts "$ART" --snapshot "$SNAP" --media-only --upload-media
    ;;
  rollback)
    to=${ROLLBACK_TO:-v2}
    case "$to" in
      v2) ct=sict_text_v2; ci=sict_image_v2 ;;
      v1) ct=sict_text;    ci=sict_image ;;
      *) echo "ROLLBACK_TO=v2|v1"; exit 2 ;;
    esac
    cat <<EOF
Rollback API về index $to (không mất dữ liệu: collection $ct/$ci + cột cũ của Postgres còn nguyên; chỉ đổi cấu hình):
  1. VM1 $ENV_FILE: QDRANT_COLLECTION_TEXT=$ct  QDRANT_COLLECTION_IMAGE=$ci  QDRANT_COLLECTION_OCR=  (trống)
     QDRANT_TEXT_DIM=384  QDRANT_IMAGE_DIM=512  EMBED_BACKEND=torch  API_MEM_LIMIT=1500m (API tự nạp E5 + CLIP)
     → docker compose up -d api      (image giữ nguyên; code v3 đọc được collection cũ)
  2. VM2: docker compose -f $WORKER_COMPOSE stop cron   (worker v3 từ chối ghi collection v1; với v2 thì sửa .env VM2 giống bước 1)
  3. Image cũ (nếu cần): CD với tag cũ, hoặc API_IMAGE=\$API_IMAGE_PREV trong .env rồi docker compose up -d api.
  4. Postgres: bảng / cột v3 chỉ thêm mới, code cũ bỏ qua. Khôi phục hẳn: pg_restore bản dump ở bước backup.
Làm thật bước 1 ngay: CONFIRM_ROLLBACK=yes ROLLBACK_TO=$to ./scripts/cutover_v3.sh rollback
EOF
    [ "${CONFIRM_ROLLBACK:-}" = "yes" ] || exit 0
    upsert() { if grep -q "^$1=" "$ENV_FILE"; then sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi; }
    cp "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%d-%H%M)"
    upsert QDRANT_COLLECTION_TEXT "$ct"; upsert QDRANT_COLLECTION_IMAGE "$ci"; upsert QDRANT_COLLECTION_OCR ""
    upsert QDRANT_TEXT_DIM 384; upsert QDRANT_IMAGE_DIM 512; upsert EMBED_BACKEND torch; upsert API_MEM_LIMIT 1500m
    say "đã sửa $ENV_FILE (bản cũ: $ENV_FILE.bak-*) → recreate API"
    run docker compose up -d api
    for i in $(seq 1 30); do curl -sf -m 10 "$API/app.js" -o /dev/null && break; sleep 10; done
    run curl -sf -m 10 "$API/app.js" -o /dev/null && echo "API lên lại với index $to"
    ;;
  *)
    sed -n '2,20p' "$0"; exit 1 ;;
esac
