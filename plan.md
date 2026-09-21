# Plan chỉnh sửa — microservice SICT (k8s + Alpine/slim)

Ngày: 2026-09-15. Nguyên tắc: sửa theo thứ tự phụ thuộc, mỗi bước verify được.
Repo không phải git → backup tar trước khi đụng (`/tmp/sict-backup-<date>.tar.gz`).

## P0 — `config.py` đọc env (12-factor cho k8s)
Vấn đề: `postgres_dsn`, `redis_url`, `qdrant host/port` chỉ đọc từ yaml →
k8s Secret/Service không inject được, đang hardcode dsn vào ConfigMap.
- Sửa `sict_crawler/config.py`: `POSTGRES_DSN`, `REDIS_URL`, `QDRANT_HOST`,
  `QDRANT_PORT`, `QDRANT_GRPC_PORT` override giá trị yaml. Không set env →
  giữ nguyên hành vi cũ (yaml local).
- Sửa kèm `k8s/api.yaml`, `k8s/cronjob-incremental.yaml`, `k8s/job-backfill.yaml`:
  thêm env (postgres từ Secret `sict-postgres`, redis/qdrant từ Service DNS,
  giữ S3 keys như cũ).
- Verify: script nhỏ assert override đúng + regression không env.

## P1 — Dọn file legacy trùng lặp
- Xóa `Dockerfile.svc-ocr` (đã thay bằng `Dockerfile.worker`).
- Xóa toàn bộ `requirements.txt` + `requirements/` (pip) — deps quản lý duy
  nhất bằng uv: `pyproject.toml` (common + groups api/ml) + `uv.lock`,
  torch lấy bản CPU (host/container không GPU).
- `auto_process.sh`: viết lại gọi `--incremental` (thay 3 lệnh rời + đoạn
  `psql` hack `persons->>'via'` đã obsolete từ OCR-only). Giữ flock.
  Bản cũ lưu `auto_process.sh.bak`.
- Verify: `bash -n`, `py_compile`, compose config parse.

## P2 — Dọn compose
- Service `cron` sleep-loop trong `docker-compose.ocr.yml`: gắn
  `profiles: ["dev-cron"]` (prod đã có k8s CronJob, tránh double-crawl).
- Xóa dòng `version: "3.9"` obsolete trong `docker-compose.yml`.
- Verify: `docker compose config -q` cả 2 file + confirm `cron` không chạy mặc định.

## P3 — K8s hardening nhẹ
- Thêm readiness `postgres` (`pg_isready`) và `qdrant` (`/healthz`) để
  Job/API không giành chạy trước DB.
- Nợ (cần CI mới đáng): HPA api, PDB, tag image theo sha.

## Rollback
Mỗi P fail → restore file tương ứng từ backup tar, không sửa tiếp.
`Dockerfile.svc-ocr` cũ + `auto_process.sh` localhost xóa hẳn sau 1 tuần
k8s chạy ổn.

## Đợt 2 (2026-09-15 tối) — reliability + dọn face + S3 (user chọn full P0–P3)
- P0 reliability (cron chết giữa chu kỳ do Qdrant gRPC DEADLINE):
  - `vector_store.ensure_payload_indexes()` — integer index `document_id` cả 2
    collection (delete từ timeout 30–120s → 1.33s). Gọi trong `ensure_collections()`.
  - `_with_retry()` cho mọi write/delete Qdrant (3 lần, backoff 2/4/8s).
  - `QdrantConfig.timeout` + env `QDRANT_TIMEOUT` (worker/cron đặt 120).
  - `recheck_edits`: cách ly lỗi từng doc (rollback + `errors`), 1 doc kẹt
    không giết chu kỳ 6h. `run_incremental`: cách ly từng stage, luôn
    heartbeat + `_notify("crawl.incremental[_error]")`, raise sau khi heartbeat.
  - Heartbeat redis `incremental:last_ok` / `:last_error` (JSON) — monitor:
    last_ok cũ >7h hoặc last_error mới hơn last_ok = fail.
- P1 disk (/ 91–99%): prune build cache, retention Prometheus 3d/2GB (có sẵn),
  panel Grafana "Disk / usage", volume `hfcache` share model E5/CLIP.
- P2 dọn face legacy: xóa `face.py`, `tests/test_face.py`, `run_annotate`,
  `run_build_faces`, flag `--annotate`/`--build-faces`, `FaceConfig` + block
  yaml (config.yaml + k8s/configmap.yaml), deps ultralytics/insightface/
  onnxruntime khỏi requirements.txt. `auto_process.sh` → wrapper exec
  `cron/incremental.sh` (giữ throughput cũ qua env, crontab không đổi).
  `vlm.py` giữ (api chat + rewrite vẫn dùng).
- P3 S3: `S3Config.profile` (AWS_PROFILE) + boto3 Session; bucket
  sict-crawl-media đã tồn tại. `scripts/verify_s3.py` quét DB↔S3:
  71940 media → 134 orphan healable (base64), 0 dead, 16 raw missing.
  Sync local→S3 xong; compose api/svc-ocr/cron mount `~/.aws:ro` +
  `AWS_PROFILE=quyen-admin`. Xóa local media 1.3G sau khi verify đọc S3 ok.
