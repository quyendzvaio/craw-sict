#!/usr/bin/env bash
# VM1 (infra + serve): cài docker + swap + dựng postgres/redis/qdrant/api.
# Yêu cầu: code đã có ở ~/craw-sict (git clone hoặc scp), file .env đã điền keys.
# Chạy: bash ~/craw-sict/scripts/vm1-init.sh
set -euo pipefail
cd ~/craw-sict

# 1. swap 2GB (T-series burstable cần để khỏi OOM khi API load model)
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \
    && sudo mkswap /swapfile && sudo swapon /swapfile \
    && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# 2. docker + compose plugin
if ! command -v docker >/dev/null; then
  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
  sudo usermod -aG docker "$USER" || true
  # tiếp tục bằng sudo docker, không cần login lại
fi

# 3. infra
sudo docker compose up -d postgres redis qdrant
echo "-- chờ postgres 20s --"; sleep 20
sudo docker logs craw-sict-postgres-1 2>&1 | tail -2

# 4. api (build 1 lần với code hiện tại)
sudo docker compose build api
sudo docker compose up -d api
# hfcache volume root-owned -> api (uid 10000) không tải được model nếu không chown
sudo docker run --rm -v craw-sict_hfcache:/cache/hf alpine chown -R 10000:10000 /cache/hf
sudo docker restart craw-sict-api-1
echo "-- chờ api load model 60s --"; sleep 60

# 5. tạo collections Qdrant (đang trống sau restore)
sudo docker exec craw-sict-api-1 python -c \
  "from sict_crawler import vector_store; vector_store.ensure_collections(); print('collections ok')"

# 6. kiểm tra
bash scripts/monitor_2vm.sh infra
