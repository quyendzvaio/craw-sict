#!/usr/bin/env bash
# VM2 (worker + embed) — dựng VM MỚI từ đầu: swap + docker + kiểm .env. Image KHÔNG build trên VM (D10): workflow CD
# (target vm2, deploy_embed=true) kéo sict-worker / sict-embed từ GHCR, ghi WORKER_IMAGE / EMBED_IMAGE vào .env.
# Yêu cầu:
#   - file compose/config ở ~/craw-sict (docker-compose.worker.yml, docker-compose.embed.yml, config.yaml, scripts/)
#   - .env copy từ .env.worker.example: IP private VM1 + mật khẩu store (D12) + bộ biến index v3 + EMBED_BIND + AWS
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

# .env đủ biến v3 + mật khẩu store (chỉ in tên biến)
bash scripts/cutover_v3.sh env-check vm2
# compose render được (chưa up gì)
sudo docker compose -f docker-compose.worker.yml -f docker-compose.embed.yml config -q
# volume HF cache của worker: root-owned → user 10000 trong image không ghi được
sudo docker volume create craw-sict_hfcache >/dev/null
sudo docker run --rm -v craw-sict_hfcache:/cache/hf alpine chown -R 10000:10000 /cache/hf || true

echo "== xong chuẩn bị VM2; tiếp: GitHub Actions → CD (target vm2, deploy_embed=true), rồi scripts/cutover_v3.sh load =="
