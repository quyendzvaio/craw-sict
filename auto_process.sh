#!/usr/bin/env bash
# Wrapper tương thích crontab cũ (*/30 * * * * /home/quyen/craw-sict/auto_process.sh).
# Logic duy nhất nằm ở cron/incremental.sh — sửa ở đó, không sửa ở đây.
# Giữ nguyên sản lượng cũ của job 30p (CRAWL_LIMIT 300 / ANNOTATE_OCR_N 2000).
set -euo pipefail
export CHECK_EDITS="${CHECK_EDITS:-100}"
export CRAWL_LIMIT="${CRAWL_LIMIT:-300}"
export ANNOTATE_OCR_N="${ANNOTATE_OCR_N:-2000}"
exec /home/quyen/craw-sict/cron/incremental.sh
