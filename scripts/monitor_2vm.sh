#!/usr/bin/env bash
# Monitor nhanh cho 2-VM compose (chạy trên từng VM, không cần Prometheus để đỡ tốn RAM).
# VM1: ./scripts/monitor_2vm.sh infra  |  VM2: ./scripts/monitor_2vm.sh worker
set -u
ROLE="${1:-infra}"
echo "=== [$(date '+%F %T')] role=$ROLE host=$(hostname) ==="
echo "--- docker ps ---"
docker ps --format 'table {{.Names}}\t{{.Status}}' 2>&1 | head -15
echo "--- disk / ---"
df -h / | tail -1
echo "--- mem ---"
free -h | head -2
if [ "$ROLE" = "infra" ]; then
  echo "--- heartbeat incremental (redis) ---"
  docker exec "$(docker ps -q --filter name=redis | head -1)" \
    redis-cli --raw GET "incremental:last_ok" 2>&1 | head -c 500; echo
  docker exec "$(docker ps -q --filter name=redis | head -1)" \
    redis-cli --raw GET "incremental:last_error" 2>&1 | head -c 500; echo
  echo "--- postgres counts ---"
  docker exec "$(docker ps -q --filter name=postgres | head -1)" \
    psql -U sict -d sict_crawler -c \
    "SELECT (SELECT count(*) FROM documents) docs, (SELECT count(*) FROM media_assets) media, (SELECT count(*) FROM media_assets WHERE caption='') no_cap;" 2>&1 | head -8
  echo "--- qdrant collections ---"
  curl -s localhost:6333/collections 2>&1 | head -c 500; echo
  echo "--- api health ---"
  curl -s -m 5 localhost:8003/health 2>&1 | head -c 300; echo
else
  echo "--- cron log tail ---"
  docker logs --tail 5 "$(docker ps -q --filter name=cron | head -1)" 2>&1 | tail -5
  echo "--- redis heartbeat via VM1 (cần REDIS_URL trỏ VM1) ---"
  python3 -c "
import os,redis
try:
    r=redis.Redis.from_url(os.environ.get('REDIS_URL','redis://localhost:6379/0'),socket_timeout=5)
    print('last_ok:',(r.get('incremental:last_ok')or b'')[:300])
except Exception as e: print('redis skip:',e)
" 2>&1 | head -5
fi
