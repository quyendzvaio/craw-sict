# Deploy production index v3 — thứ tự bước, biến `.env`, verify, rollback

Hai VM Docker Compose (D15, D32): **VM1** (2 vCPU / 4 GB) postgres + redis + qdrant + api + nginx;
**VM2** (2 vCPU / 8 GB) embed int8 (`docker-compose.embed.yml`) + `cron` incremental + Obscura
(`docker-compose.worker.yml`). Image kéo từ GHCR bằng workflow CD theo SHA của `main` (D10), VM không build.
Mọi bước trên VM / S3 chỉ chạy khi chủ dự án đã cho phép (quy tắc 2026-10-14).

**Quyết định chủ dự án 2026-11-05:**
- Mọi nhánh chỉ merge vào `develop`. `develop → main` là bước cuối cùng, ngay trước khi lên production.
- CD chỉ chạy ở bước cuối, sau khi đã merge vào `main` (bước 0 bên dưới mở đầu chuỗi đó).
- Dữ liệu production được thay bằng bản mới nhất (index v3), không giữ dữ liệu hiện có trên VM. Bước 8 vẫn backup trước khi thay.
- Grafana Cloud / Alloy chỉ bật khi lên production (bước 13), không bật sớm hơn.

## 1. Thứ tự bước

| # | Ở đâu | Lệnh | Kiểm |
|---|---|---|---|
| 0 | GitHub | merge `develop → main` → workflow `release-images` tạo 3 image `sict-{api,worker,embed}:<sha>` | `gh run list --workflow release.yml` xanh |
| 1 | VM1 | `.env` theo mục 2 (`scripts/store_auth.sh init-env` sinh mật khẩu store) | `scripts/cutover_v3.sh env-check vm1` |
| 2 | VM2 | `.env` theo mục 2 (cùng mật khẩu store với VM1) | `scripts/cutover_v3.sh env-check vm2` |
| 3 | AWS | Security Group: VM1 nhận 5432 / 6379 / 6333–6334 từ SG VM2; VM2 nhận 8090 từ SG VM1; 22 cho runner CD | từ VM2 `nc -zv <IP-private-VM1> 5432 6379 6333 6334` |
| 4 | GitHub | CD `target=vm2`, `tag=<sha>`, `deploy_embed=true` (embed lên trước API: API v3 nhúng câu hỏi qua nó). Lần đầu tải fp32 3,5 GB + build tháp ~2,5 phút | `curl http://<IP-private-VM2>:8090/healthz` → `{"ok":true}`; `scripts/embed_parity.py --base http://…:8090/v1` (qua SSH tunnel) |
| 5 | VM2 | `docker compose -f docker-compose.worker.yml stop cron` (không để incremental ghi PG / Qdrant VM1 trong lúc thay dữ liệu) | `docker compose -f docker-compose.worker.yml ps` không còn cron |
| 6 | máy dev | `./scripts/cutover_v3.sh export` (mục 1a; chạy khi worker dev không giữa lượt) → `scp scripts/cutover_v3.sh ubuntu@<VM1>:craw-sict/scripts/` và `scp -r _cutover/dev-state ubuntu@<VM1>:craw-sict/_cutover/` (CD không chép script này) | `manifest.json`: `documents`, point 3 collection, `media.missing_in_local_store` = 0 |
| 7 | máy dev (có AWS) | `CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v3.sh upload-media-store` (file `media/` của local store, ~2 GB; key đã có thì bỏ qua, chạy lại an toàn) | dòng cuối `S3 có đủ N key media của local store`, N = `media.local_store_files` của manifest |
| 8 | VM1 | `./scripts/cutover_v3.sh backup` (pg_dump + snapshot Qdrant mọi collection đang có) | `pg_restore --list ~/backup-v3-*/sict_crawler.dump \| head` |
| 9 | VM1 | `docker compose stop api` rồi `OUT=_cutover/dev-state CONFIRM_RESTORE=yes ./scripts/cutover_v3.sh restore` (mục 1b: thay DB `sict_crawler` + nạp 3 snapshot `*_v3`, rồi so với manifest) | in `khớp manifest`; lệch → exit 1 (`verify-restore` chạy lại riêng bước so) |
| 10 | GitHub | CD `target=vm1`, `tag=<sha>` (chép compose/config, `store_auth check`, `sync-pg`, `up -d`; API đọc `.env` v3) | job xanh; `curl -sf http://<VM1>:8003/app.js`; `docker stats` API ~200 MiB |
| 11 | máy dev | `API=http://<VM1>:8003 ./scripts/cutover_v3.sh verify` (xoá cache `q:v10:*` Redis db 3 trước) | golden_v1 R@5 ≥ 0,985 / MRR ≥ 0,93; golden_v2 url_hit ≥ 70/80; list_v2 ≥ 0,90; `accept_api` exit 0; `/media/s/<key>` 200 |
| 12 | VM2 | `docker compose -f docker-compose.worker.yml up -d cron` | `./scripts/monitor_2vm.sh worker`; Redis `incremental:last_ok` mới, `last_error` trống; lượt đầu graph "rebuild" một lần (Redis production chưa có chữ ký, mục 1b), lượt sau "skipped" |
| 13 | GitHub | CD `deploy_alloy=true` (Alloy đã cài trên VM) | `scripts/verify_gcloud.py --instance sict-vm1 --instance sict-vm2` VERIFIED |

Bước 6–9 thay cho đường artifact (`pack` + `load` + `upload-media`, mục 1c) theo quyết định 2026-11-05: artifact
`_snapshot/2026-10-12/artifacts/v3-jina-clip-v2` thiếu bài mới (2.277 bài so với 2.303 trong PG dev), thiếu nhãn bảng
`[Bảng: …]` đã index lại trên máy dev ngày 2026-11-01, còn graph khái niệm và payload index `phrase_matching` chỉ có
trong store dev. Incremental không bù được (chỉ index lại khi hash trang đổi), nên production nhận nguyên trạng thái
store dev.

### 1a. `export` (máy dev)

`./scripts/cutover_v3.sh export` chỉ đọc store dev (container `craw-sict-postgres-1`, Qdrant `localhost:6333`; đổi bằng
`SRC_PG_CONTAINER` / `SRC_QDRANT`) và ghi vào `OUT` (mặc định `_cutover/dev-state`, phải trống):

- kiểm chỗ trống trước khi ghi (cỡ DB + `MIN_FREE_GB`, mặc định 5 GB; trước mỗi lần tải snapshot: cỡ snapshot + 1 GB);
- đếm số dòng mọi bảng `public` và số point chính xác (`points/count` exact) của 3 collection trước và sau khi dump;
  lệch → exit 1 (store bị ghi giữa chừng: chờ worker nghỉ rồi chạy lại);
- `pg_dump -Fc sict_crawler` → `sict_crawler.dump`;
- snapshot **chỉ** `sict_text_v3`, `sict_image_v3`, `sict_ocr_v3`, tải về `<collection>.snapshot`, so sha256 với checksum
  Qdrant trả về, rồi xoá đúng các snapshot vừa tạo trong Qdrant dev (cả khi lỗi giữa chừng: `trap EXIT`). Snapshot mang
  theo cấu hình vector, sparse `bm25` và payload index (`phrase_matching`…);
- `manifest.json`: sha256 + cỡ từng file, phiên bản Postgres / Qdrant, commit của repo, số dòng từng bảng, số point +
  cấu hình vector + payload schema từng collection, và đối chiếu `media.s3_key` với file trong local store
  (`media.missing_in_local_store` phải là 0 thì `upload-media-store` mới đủ ảnh).

### 1b. `restore` (VM1) và Redis production

`restore` từ chối khi thiếu `CONFIRM_RESTORE=yes`, khi sha256 file lệch manifest, khi `env-check vm1` hỏng, khi chưa có
`~/backup-v3-*/sict_crawler.dump` (bỏ qua: `SKIP_BACKUP_CHECK=yes`), khi API compose còn chạy (`ALLOW_API_RUNNING=yes`),
khi Postgres đích cũ hơn nguồn, Qdrant đích khác `major.minor` của nguồn, hoặc thiếu chỗ (2 × cỡ export + 1 GB). Rồi:

1. Postgres: `DROP DATABASE sict_crawler WITH (FORCE)` + `CREATE DATABASE … OWNER sict`, rồi `pg_restore --no-owner
   --no-privileges --exit-on-error --single-transaction` qua socket trong container (không cần mật khẩu, không có gì bí
   mật trên dòng lệnh). DB production cũ (v1) mất hẳn, chỉ còn trong bản backup bước 8.
2. Qdrant: `POST /collections/<c>/snapshots/upload?priority=snapshot&checksum=<sha256>` cho từng collection (thay
   collection cùng tên nếu có; `sict_text` / `sict_image` v1 giữ nguyên). `QDRANT_API_KEY` đọc từ `.env`, gửi bằng
   `curl -H @<(printf 'api-key: %s' …)`: không lên argv, `ps` không thấy.
3. So lại số dòng mọi bảng, số point chính xác, cấu hình vector / sparse, payload schema với manifest. Chạy lại riêng
   bước so: `OUT=… ./scripts/cutover_v3.sh verify-restore`. Thử trên máy khác: `PG_CONTAINER=<container postgres>`,
   `QDRANT_URL=http://localhost:<cổng>`, `ENV_FILE=<.env thử>`.

Đã thử trên máy dev 2026-11-05 (export thật → restore vào container `postgres:16` + `qdrant:v1.19.1` riêng, có API key):
export 421 MB (dump 6,1 MB, snapshot text 261 MB / image 77 MB / ocr 78 MB), 2.303 bài, 6.240 media, 4.938 khái niệm,
327.647 cạnh, point 16.628 / 6.019 / 5.955; restore ~6,5 phút trên máy đang tải nặng (nạp snapshot text 137 s), khớp
manifest, và vẫn khớp sau khi máy khởi động lại. golden_v1 qua API trỏ vào store restore và API trỏ vào store dev
(cùng code, không cache, không LLM): giống nhau từng câu, R@5 1,0 / R@10 1,0 / MRR 0,947 / media_mode 0,871 / media
đúng bài 7/7 (n = 67).

**Thứ tự với CD vm1 và mật khẩu store (D12).** Production VM1 hiện (2026-11-05, xem tay, chỉ đọc): Postgres 16.15
(`~/craw-sict/.pgdata`, DB `sict_crawler`, user `sict`, 1.205 bài v1), Qdrant 1.19.1 (tag `latest`, volume
`craw-sict_qdata`, chỉ `sict_text` + `sict_image`), store chưa có mật khẩu, `.env` cũ chưa có biến v3; RAM trống ~2 GB,
đĩa trống 18 GB. Bảng trên đặt `restore` (bước 9) **trước** CD vm1 (bước 10), và cả hai thứ tự đều chạy được:

- Restore chỉ cần `.env` mới (bước 1: mật khẩu + biến v3, `env-check vm1` qua) và compose đang có trên VM1
  (`docker compose exec -T postgres`, service tên `postgres` như cũ). Postgres đi qua socket trong container nên không
  cần mật khẩu; Qdrant nhận header `api-key` từ `.env` — Qdrant cũ chưa bật khoá thì bỏ qua header, Qdrant đã bật khoá
  (sau CD) thì header là khoá đúng. Kiểm phiên bản Qdrant đích (`1.19`) khớp nguồn.
- Bước 10 (CD vm1) sau đó tạo lại container store với khoá / mật khẩu trên cùng data dir / volume (dữ liệu restore giữ
  nguyên), chạy `store_auth.sh sync-pg` đặt mật khẩu cho role `sict` (mật khẩu role thuộc cluster, không nằm trong dump
  nên restore không làm mất), rồi mới bật API v3 — API không bao giờ chạy trên DB v1 với cấu hình v3.
- Chạy CD vm1 trước restore cũng được nhưng API v3 lên trên DB v1 (không có collection `*_v3`) cho tới khi restore
  xong: phải `docker compose stop api` ngay sau CD, restore, rồi `docker compose up -d api`.
- API v1 dừng từ bước 9 tới bước 10 (restore từ chối khi API compose còn chạy); `DROP DATABASE … WITH (FORCE)` thay
  toàn bộ DB v1 (1.205 bài) bằng DB dev, collection v1 để nguyên cho tới khi chủ dự án xoá.

**Redis production không bị restore đụng tới** (export không có dữ liệu Redis). Lượt incremental đầu sau đó:

- `frontier:seen` (của worker v1 cũ) được `mark_known_seen` bổ sung URL mọi bài trong PG mới (≤ 2.303, URL đã có không
  tính), nên bài đã có không bị xếp hàng lại. `frontier:list` / `frontier:retry` còn sót từ worker v1 vẫn được drain
  (≤ `CRAWL_LIMIT`, trong ngân sách 5 phút): URL đã có bài thì `hash_v2` trùng → bỏ qua, chỉ tốn một lần tải. Muốn bắt
  đầu sạch thì trước bước 12 xoá `frontier:list frontier:retry frontier:fails` bằng `redis-cli DEL` trong container
  redis, mật khẩu qua biến `REDISCLI_AUTH` truyền bằng `docker compose exec -e REDISCLI_AUTH` (không ghi giá trị trên
  dòng lệnh).
- `graph:corpus_sig` chưa có (hoặc là chữ ký cũ) → khác chữ ký corpus mới → `graphsync.rebuild` chạy **một lần** ở lượt
  đầu. Cache cụm danh từ `chunk_phrases` đi theo dump nên không phải tách lại; thực thể và cạnh ghi lại như cũ, payload
  so theo tập nên không point nào phải ghi lại. Đo trên store thử (image worker, `graphsync rebuild`): tách cụm 0,4 s
  (không đoạn nào phải tách), 4.938 khái niệm, 0 bị xoá, 327.647 cạnh, payload đổi 0 / 16.628 + 0 / 5.955 point, tổng
  ~2,2 phút (ghi cạnh 98 s). Các lượt sau "skipped" tới khi corpus đổi.
- `reconcile:last` chưa có → reconcile chạy ở lượt đầu; PG và Qdrant từ cùng một bản export nên không có point mồ côi.
- Cache truy vấn `q:v10:*` (Redis db 3) có `index_tag` (collection đang phục vụ) trong khoá nên không trả nhầm kết quả
  v1; vẫn xoá trước khi đo (bước 11).

Lượt incremental đầu có thể vượt ngân sách 5 phút (bài đăng sau lúc export, rebuild graph): phần còn lại sang lượt sau
(12 h), hoặc chạy tay một lần ngoài giờ.

### 1c. Đường thay thế: nạp từ artifact (`pack` + `load`)

Dữ liệu chỉ tới snapshot 2026-10-12 (không có bài mới, nhãn bảng, graph của máy dev). Chỉ dùng khi không lấy được trạng
thái máy dev; thay bước 6, 7, 9 bằng:

| Ở đâu | Lệnh | Kiểm |
|---|---|---|
| máy dev | `ART=_snapshot/2026-10-12/artifacts/v3-jina-clip-v2 ./scripts/cutover_v3.sh pack` → `scp _cutover/v3-load.tgz ubuntu@<VM2>:craw-sict/` | ~330 MB trên VM2 |
| máy dev (có snapshot + AWS) | `CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v3.sh upload-media` (1,9 GB; key đã có thì bỏ qua) | log loader `uploaded N`; `aws s3 ls s3://<bucket>/media/ \| wc -l` ≈ 5.962 |
| VM2 (sau backup bước 8) | `./scripts/cutover_v3.sh load` (image worker; loader nạp PG + 3 collection `*_v3` trên VM1, không upload media, dựng graph khái niệm lần đầu ~13 phút) | log `ok: true`, `points` = `expected`; `graphsync stats` ≈ 4,9k khái niệm |

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
- **Postgres**: đường artifact (`load`) chỉ thêm bảng / cột, code cũ bỏ qua. Đường máy dev (`restore`) thay cả DB:
  rollback về v1 phải khôi phục bản dump của bước 8 theo cùng cách restore làm (API dừng; `psql -U sict -d postgres`
  trong container: `DROP DATABASE sict_crawler WITH (FORCE)` + `CREATE DATABASE sict_crawler OWNER sict`; rồi
  `docker compose exec -T postgres pg_restore -U sict -d sict_crawler --no-owner --no-privileges --exit-on-error
  < ~/backup-v3-*/sict_crawler.dump`), sau đó đổi `.env` như trên. `--clean` trên DB v3 không đủ: bảng v3 có khoá
  ngoại tới `documents` chặn DROP.
- Nạp lại sạch: `restore` chạy lại được (DROP DATABASE + snapshot `priority=snapshot` thay collection cùng tên);
  đường artifact: `./scripts/cutover_v3.sh load` với `LOAD_RECREATE=yes` (loader `--recreate`).

## 5. Việc chủ dự án phải quyết (chưa có trong repo)

`EMBED_API_BASE` trên VM2 (tên service `embed` hay IP private), cổng SG (22 cho runner GitHub), bucket S3 và tài
khoản AWS của khách, LLM provider / key, domain + TLS (nginx hiện nghe 8080, API 8003, không TLS), environment
`production` với Required reviewers, cookies YouTube (`COOKIES_TXT`), lịch backup định kỳ.
