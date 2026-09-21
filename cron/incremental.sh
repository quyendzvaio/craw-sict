#!/usr/bin/env bash
# Cron incremental: chỉ thêm/sửa/xóa, không crawl lại đồ cũ.
# Đặt trên node 8GB, chạy mỗi 6h (site trường ít đổi trong ngày):
#   0 */6 * * * /home/quyen/craw-sict/cron/incremental.sh >>/var/log/sict-incremental.log 2>&1
# S3 AWS: mặc định dùng AWS_PROFILE (key rời qua AWS_ACCESS_KEY_ID/SECRET
# vẫn được ưu tiên). MinIO: export S3_ENDPOINT_URL=http://minio:9000.
export AWS_PROFILE="${AWS_PROFILE:-quyen-admin}"
set -euo pipefail
cd /home/quyen/craw-sict || exit 1
exec 9>.lock-incremental
flock -n 9 || { echo "$(date '+%F %T') skip: incremental khác đang chạy"; exit 0; }

PY=.venv/bin/python
LOG=incremental_cron.log
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

say "== incremental start =="
$PY -m sict_crawler.main --incremental \
  --check-edits "${CHECK_EDITS:-100}" \
  --crawl-limit "${CRAWL_LIMIT:-200}" \
  --annotate-ocr-n "${ANNOTATE_OCR_N:-500}" >>"$LOG" 2>&1
say "== incremental done =="
