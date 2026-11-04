#!/usr/bin/env bash
# VM1 (infra + serve) — dựng VM MỚI từ đầu: swap + docker + bí mật store (D12) + postgres/redis/qdrant.
# API / nginx KHÔNG dựng ở đây: D10 — VM không build, image kéo từ GHCR bằng workflow CD (`cd.yml`, target vm1).
# Yêu cầu: file compose/config ở ~/craw-sict (git clone hoặc scp: docker-compose.yml, config.yaml, nginx/, scripts/),
#          .env đã điền LLM_* / AWS_* + bộ biến index v3 (.env.example, kiểm: scripts/cutover_v3.sh env-check vm1).
# Chạy: bash ~/craw-sict/scripts/vm1-init.sh
set -euo pipefail
cd ~/craw-sict

# 1. swap 2GB (D11: burstable cần để khỏi OOM)
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

# 3. D12: mật khẩu Postgres / Redis / khoá Qdrant ngẫu nhiên + STORE_BIND=0.0.0.0 (không in giá trị)
bash scripts/store_auth.sh init-env
bash scripts/store_auth.sh check vm1

# 4. infra (collection Qdrant do loader tạo khi nạp artifact: scripts/cutover_v3.sh load trên VM2)
sudo docker compose up -d postgres redis qdrant
echo "-- chờ postgres --"
for i in $(seq 1 30); do sudo docker compose exec -T postgres pg_isready -U sict -q && break; sleep 2; done
bash scripts/store_auth.sh sync-pg
sudo docker logs craw-sict-postgres-1 2>&1 | tail -2

# 5. kiểm tra; API + nginx lên bằng CD (Actions → CD → target vm1, tag = SHA trên main)
bash scripts/monitor_2vm.sh infra
echo "== xong hạ tầng VM1; tiếp: GitHub Actions → CD (target vm1) để kéo image API + nginx =="
