#!/usr/bin/env bash
# Cutover production sang index v2 (D8/D16/D20) — từng bước do người vận hành gọi, KHÔNG gộp tự động.
# Collection v1 (sict_text/sict_image) và cột v1 của Postgres không bị đụng → rollback được.
#
#   (máy dev) ./scripts/cutover_v2.sh pack            đóng gói artifact + HTML snapshot (~140 MB)
#   (VM1)     ./scripts/cutover_v2.sh backup          pg_dump + snapshot Qdrant v1 → ~/backup-v2-<ngày>/
#   (VM1)     ./scripts/cutover_v2.sh load            build image mới (API cũ vẫn chạy), nạp PG + *_v2,
#                                                     KHÔNG upload media
#   (VM1)     ./scripts/cutover_v2.sh verify          bộ câu hỏi vàng qua /query (sau khi API chạy code mới)
#   (máy có snapshot + AWS) CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v2.sh upload-media
#                                                     ~1,9 GB ảnh + frame lên S3 (key media/<sha256>.<ext>)
#   ./scripts/cutover_v2.sh rollback                  in hướng dẫn rollback
# Deploy code (API + worker CÙNG LÚC: worker v2 từ chối ghi collection v1) = workflow CD thủ công.
# Ảnh v2 hiện qua /media/s/<sha256> chỉ sau upload-media → muốn không có ảnh hỏng thì upload trước deploy.
set -euo pipefail

SNAP=${SNAP:-_snapshot/2026-10-12}
ART=${ART:-$SNAP/artifacts/v2-multilingual-e5-small}
PKG=${PKG:-_cutover/v2-load.tgz}
API=${API:-http://localhost:8003}
cd "$(dirname "$0")/.."

case "${1:-}" in
  pack)
    mkdir -p "$(dirname "$PKG")"
    tar czf "$PKG" --exclude="$ART/ocr_cache.jsonl" "$ART" "$SNAP/html"
    ls -lh "$PKG"
    echo "→ chép $PKG + code lên VM1 (~/craw-sict), rồi chạy 'backup' và 'load' trên VM1"
    ;;
  backup)
    d=~/backup-v2-$(date +%Y%m%d-%H%M)
    mkdir -p "$d"
    docker compose exec -T postgres pg_dump -U sict -Fc sict_crawler > "$d/sict_crawler.dump"
    for c in sict_text sict_image; do
      curl -sf -X POST "http://localhost:6333/collections/$c/snapshots" -o "$d/qdrant-$c.json" || \
        echo "cảnh báo: không tạo được snapshot Qdrant $c"
    done
    ls -lh "$d"
    echo "→ snapshot Qdrant nằm trong volume qdata (/qdrant/storage/snapshots); dump PG ở $d"
    ;;
  load)
    [ -f "$PKG" ] && tar xzf "$PKG"
    docker compose build api   # chỉ build — container API đang chạy giữ image cũ tới lúc deploy
    docker compose run --rm --no-deps -v "$PWD/_snapshot:/app/_snapshot:ro" api \
      python -m sict_crawler.pipeline.loader --artifacts "$ART" --skip-media-upload
    ;;
  verify)
    python3 scripts/eval_api.py --api "$API" --golden eval/golden_v1.jsonl
    ;;
  upload-media)
    if [ "${CONFIRM_S3_UPLOAD:-}" != "yes" ]; then
      echo "Từ chối: upload S3 cần CONFIRM_S3_UPLOAD=yes (chỉ chạy khi đã được cho phép)." >&2
      exit 2
    fi
    python -m sict_crawler.pipeline.loader --artifacts "$ART" --snapshot "$SNAP" --media-only --upload-media
    ;;
  rollback)
    cat <<'EOF'
Rollback API về index v1 (không mất dữ liệu: collection v1 + cột v1 còn nguyên):
  1. .env trên VM1: QDRANT_COLLECTION_TEXT=sict_text  QDRANT_COLLECTION_IMAGE=sict_image
     (hoặc deploy lại commit cũ) → docker compose up -d api
  2. VM2: dừng worker code mới (nó từ chối ghi collection v1), chạy lại image cũ.
  3. Postgres: bảng/cột v2 là thêm mới, code cũ bỏ qua. Cần khôi phục hẳn: pg_restore bản dump ở bước backup.
EOF
    ;;
  *)
    sed -n '2,15p' "$0"; exit 1 ;;
esac
