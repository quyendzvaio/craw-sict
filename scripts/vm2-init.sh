#!/usr/bin/env bash
# VM2 (worker batch): cài docker + swap + build worker + chạy cron incremental.
# Yêu cầu:
#   - code ở ~/craw-sict (git clone hoặc scp, cùng bản với VM1)
#   - file .env copy từ .env.worker.example, đã điền IP VM1 + S3/AWS
# Chạy: bash ~/craw-sict/scripts/vm2-init.sh
set -euo pipefail
cd ~/craw-sict
[ -f .env ] || { echo "!! thiếu .env (copy từ .env.worker.example và điền IP VM1)"; exit 1; }

if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \
    && sudo mkswap /swapfile && sudo swapon /swapfile \
    && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi
if ! command -v docker >/dev/null; then
  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
  sudo usermod -aG docker "$USER" || true
  # tiếp tục bằng sudo docker, không cần login lại
fi

# build nặng (torch CPU ~800MB, 10-20p) — chỉ lần đầu
sudo docker compose -f docker-compose.worker.yml build svc-ocr
sudo docker run --rm -v craw-sict_hfcache:/cache/hf alpine chown -R 10000:10000 /cache/hf || true

# chạy thử 1 mẻ nhỏ trước khi bật cron 6h (phát hiện lỗi sớm, đỡ tốn tiền chạy vô ích)
sudo docker compose -f docker-compose.worker.yml up -d obscura
sudo docker compose -f docker-compose.worker.yml run --rm svc-ocr \
  python -m sict_crawler.main --incremental --check-edits 5 --crawl-limit 5 --annotate-ocr-n 20
echo "== mẻ thử xong, kiểm tra log trên rồi bật cron =="
# docker compose -f docker-compose.worker.yml up -d cron
bash scripts/monitor_2vm.sh worker
