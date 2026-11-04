# Deploy production index v3 — thứ tự bước, biến `.env`, verify, rollback

Hai VM Docker Compose (D15, D32): **VM1** (2 vCPU / 4 GB) postgres + redis + qdrant + api + nginx;
**VM2** (2 vCPU / 8 GB) embed int8 (`docker-compose.embed.yml`) + `cron` incremental + Obscura
(`docker-compose.worker.yml`). Image kéo từ GHCR bằng workflow CD theo SHA của `main` (D10), VM không build.
Mọi bước trên VM / S3 chỉ chạy khi chủ dự án đã cho phép (quy tắc 2026-10-14).

## 1. Thứ tự bước

| # | Ở đâu | Lệnh | Kiểm |
|---|---|---|---|
| 0 | GitHub | merge `develop → main` → workflow `release-images` tạo 3 image `sict-{api,worker,embed}:<sha>` | `gh run list --workflow release.yml` xanh |
| 1 | VM1 | `.env` theo mục 2 (`scripts/store_auth.sh init-env` sinh mật khẩu store) | `scripts/cutover_v3.sh env-check vm1` |
| 2 | VM2 | `.env` theo mục 2 (cùng mật khẩu store với VM1) | `scripts/cutover_v3.sh env-check vm2` |
| 3 | AWS | Security Group: VM1 nhận 5432 / 6379 / 6333–6334 từ SG VM2; VM2 nhận 8090 từ SG VM1; 22 cho runner CD | từ VM2 `nc -zv <IP-private-VM1> 5432 6379 6333 6334` |
| 4 | GitHub | CD `target=vm2`, `tag=<sha>`, `deploy_embed=true` (embed lên trước API: API v3 nhúng câu hỏi qua nó). Lần đầu tải fp32 3,5 GB + build tháp ~2,5 phút | `curl http://<IP-private-VM2>:8090/healthz` → `{"ok":true}`; `scripts/embed_parity.py --base http://…:8090/v1` (qua SSH tunnel) |
| 5 | VM2 | `docker compose -f docker-compose.worker.yml stop cron` (VM1 chưa có collection v3; tránh incremental chạy song song loader) | `docker compose -f docker-compose.worker.yml ps` không còn cron |
| 6 | máy dev | `ART=_snapshot/2026-10-12/artifacts/v3-jina-clip-v2 ./scripts/cutover_v3.sh pack` → `scp _cutover/v3-load.tgz ubuntu@<VM2>:craw-sict/` | ~330 MB trên VM2 |
| 7 | máy dev (có snapshot + AWS) | `CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v3.sh upload-media` (1,9 GB; key đã có thì bỏ qua, chạy lại an toàn) | log loader `uploaded N`; `aws s3 ls s3://<bucket>/media/ \| wc -l` ≈ 5.962 |
| 8 | VM1 | `./scripts/cutover_v3.sh backup` (pg_dump + snapshot Qdrant mọi collection đang có) | `pg_restore --list ~/backup-v3-*/sict_crawler.dump \| head` |
| 9 | VM2 | `./scripts/cutover_v3.sh load` (image worker; loader nạp PG + 3 collection `*_v3` trên VM1, không upload media, dựng graph khái niệm lần đầu ~13 phút) | log `ok: true`, `points` = `expected`; `graphsync stats` ≈ 4,9k khái niệm |
| 10 | GitHub | CD `target=vm1`, `tag=<sha>` (chép compose/config, `store_auth check`, `sync-pg`, `up -d`; API đọc `.env` v3) | job xanh; `curl -sf http://<VM1>:8003/app.js`; `docker stats` API ~200 MiB |
| 11 | máy dev | `API=http://<VM1>:8003 ./scripts/cutover_v3.sh verify` (xoá cache `q:v10:*` Redis db 3 trước) | golden_v1 R@5 ≥ 0,985 / MRR ≥ 0,93; golden_v2 url_hit ≥ 70/80; list_v2 ≥ 0,90; `accept_api` exit 0; `/media/s/<key>` 200 |
| 12 | VM2 | `docker compose -f docker-compose.worker.yml up -d cron` | `./scripts/monitor_2vm.sh worker`; Redis `incremental:last_ok` mới, `last_error` trống; graph "skipped" (chữ ký trùng) |
| 13 | GitHub | CD `deploy_alloy=true` (Alloy đã cài trên VM) | `scripts/verify_gcloud.py --instance sict-vm1 --instance sict-vm2` VERIFIED |

Lượt incremental đầu trên production có thể vượt ngân sách 5 phút (2.277 bài `mark_known_seen`, bài tồn đọng từ 12/10):
phần còn lại sang lượt sau (12 h) — chấp nhận, hoặc chạy tay một lần ngoài giờ.

## 2. Checklist `.env` production (chỉ tên biến; giá trị không bao giờ đưa vào repo / log)

CD **không** ghi các biến này — người vận hành soạn tay, CD chỉ thêm `API_IMAGE` / `WORKER_IMAGE` / `EMBED_IMAGE`
(+ `*_PREV`). `scripts/cutover_v3.sh env-check vm1|vm2` kiểm tên + giá trị cố định (collection, dim) và gọi
`store_auth.sh check`. Loader so manifest artifact với `EMBED_*` / `QDRANT_*`: lệch → `SystemExit`, không nạp.

**VM1 (`.env.example`)**

| Biến | Giá trị production | Ghi chú |
|---|---|---|
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `QDRANT_API_KEY`, `STORE_BIND=0.0.0.0` | `store_auth.sh init-env` | D12 |
| `EMBED_BACKEND=api`, `EMBED_API_BASE=http://<IP-private-VM2>:8090/v1`, `EMBED_API_KEY=` (trống), `EMBED_MODEL=jina-clip-v2`, `EMBED_DIM=1024`, `EMBED_QUERY_IMAGE_SIZE=336` | | embed trên VM2 nghe `EMBED_BIND` (IP private) |
| `QDRANT_COLLECTION_TEXT=sict_text_v3`, `QDRANT_COLLECTION_IMAGE=sict_image_v3`, `QDRANT_COLLECTION_OCR=sict_ocr_v3`, `QDRANT_TEXT_DIM=1024`, `QDRANT_IMAGE_DIM=1024` | | index v3 |
| `CHUNK_STRUCTURED=1` | | D23 |
| `API_MEM_LIMIT=600m` | | API backend `api` ~200 MiB; trả ~900 MB cho VM1 |
| `LLM_API_BASE`, `LLM_MODEL`, `LLM_API_KEY` | của khách | thiếu → `/chat` chỉ echo ngữ cảnh |
| `AWS_PROFILE` (có `~/.aws`) hoặc `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | | `/media/s/<sha256>` đọc S3 |
| `API_IMAGE` (+ `API_IMAGE_PREV`) | CD ghi | rollback image |

**VM2 (`.env.worker.example`)**

| Biến | Giá trị production | Ghi chú |
|---|---|---|
| `POSTGRES_DSN=postgresql+asyncpg://sict:<POSTGRES_PASSWORD>@<IP-private-VM1>:5432/sict_crawler`, `REDIS_URL=redis://:<REDIS_PASSWORD>@<IP-private-VM1>:6379/0`, `QDRANT_HOST=<IP-private-VM1>`, `QDRANT_API_KEY`, `QDRANT_TIMEOUT=120` | cùng bí mật VM1 | D12 |
| `EMBED_BACKEND=api`, `EMBED_API_BASE=http://embed:8090/v1`, `EMBED_API_KEY=`, `EMBED_MODEL=jina-clip-v2`, `EMBED_DIM=1024` | | `embed` cùng project compose (cùng thư mục) → phân giải tên service; embed nơi khác → `http://<IP-private-VM2>:8090/v1` |
| `QDRANT_COLLECTION_TEXT/IMAGE/OCR=*_v3`, `QDRANT_TEXT_DIM=1024`, `QDRANT_IMAGE_DIM=1024`, `CHUNK_STRUCTURED=1` | | giống VM1 |
| `EMBED_BIND=<IP-private-VM2>` (và `EMBED_QUERY_IMAGE_SIZES=336` nếu đổi) | | embed nghe IP private cho VM1 |
| `CHECK_EDITS=50`, `CRAWL_LIMIT=200`, `ANNOTATE_OCR_N=2000` (tuỳ chọn `CYCLE_BUDGET_SEC`, `CRON_INTERVAL_SEC`, `CRON_MEM_LIMIT`) | | D31 |
| `AWS_*` / `S3_ENDPOINT_URL`, `CRAWLER_BACKEND=auto`, `HF_TOKEN` | | |
| `WORKER_IMAGE`, `EMBED_IMAGE` (+ `*_PREV`) | CD ghi | |

## 3. Verify sau cutover

- `scripts/cutover_v3.sh verify` (= `eval_api.py` golden_v1 / golden_v2 / list_v2 + `accept_api.py`); xoá cache
  `q:v10:*` (Redis db 3) trước khi đo.
- `scripts/monitor_2vm.sh infra` (VM1) / `worker` (VM2); `GET /metrics` có `sict_pipeline_metrics_up 1`.
- D12 trên máy thật: Redis không mật khẩu → NOAUTH; `curl http://<IP-VM1>:6333/collections` không key → 401/403.
- RAM: `docker stats` API < 600 MiB, Qdrant < 1.100 MiB (giữ v3 + collection cũ), embed < 6 GB.

## 4. Rollback

- **API về index cũ (không mất dữ liệu)**: `CONFIRM_ROLLBACK=yes ROLLBACK_TO=v2|v1 ./scripts/cutover_v3.sh rollback`
  trên VM1 = sửa `QDRANT_COLLECTION_*`, `QDRANT_TEXT_DIM/IMAGE_DIM`, `EMBED_BACKEND=torch`, `API_MEM_LIMIT=1500m`
  trong `.env` (bản cũ giữ ở `.env.bak-*`) rồi `docker compose up -d api`. Chỉ có ý nghĩa khi VM1 còn collection
  đó (production hiện chỉ có v1: `sict_text` / `sict_image`).
- **Image cũ**: CD với tag cũ, hoặc `API_IMAGE=$API_IMAGE_PREV` (`WORKER_IMAGE_PREV`, `EMBED_IMAGE_PREV`) rồi `up -d`.
- **Worker**: `docker compose -f docker-compose.worker.yml stop cron` (worker v3 từ chối ghi collection v1).
- **Postgres**: bảng / cột v3 chỉ thêm mới, code cũ bỏ qua; khôi phục hẳn = `pg_restore` bản dump của bước 8.
- Xoá collection v3 để nạp lại sạch: `./scripts/cutover_v3.sh load` với `LOAD_RECREATE=yes` (loader `--recreate`).

## 5. Việc chủ dự án phải quyết (chưa có trong repo)

`EMBED_API_BASE` trên VM2 (tên service `embed` hay IP private), cổng SG (22 cho runner GitHub), bucket S3 và tài
khoản AWS của khách, LLM provider / key, domain + TLS (nginx hiện nghe 8080, API 8003, không TLS), environment
`production` với Required reviewers, cookies YouTube (`COOKIES_TXT`), lịch backup định kỳ.
