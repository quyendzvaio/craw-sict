# SICT Multi-Modal Crawler

Crawl `https://sict.haui.edu.vn/vn` (toàn bộ tree) → text + table + image + video keyframes
→ PostgreSQL + Qdrant + AWS S3 → RAG Q&A API + Chat UI.

> Đang thực hiện **production hardening (Đợt 3)** — quyết định, lộ trình, verify/rollback ở
> [`plan.md`](plan.md). Phần dưới mô tả **trạng thái hiện tại**; mục "Kiến trúc mục tiêu" mô tả
> đích đến đã chốt.

## Chạy local

```bash
# 1. hạ tầng
docker compose up -d postgres redis qdrant

# 2. python deps — chỉ dùng uv (không pip/requirements.txt)
uv sync --group dev --group api   # thêm --group ml cho E5/CLIP/OCR/video

# 3. S3: AWS_PROFILE hoặc AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.
#    Không có credentials → lưu local ./_local_store

# 4. crawl thử 20 trang / chu kỳ incremental
uv run python -m sict_crawler.pipeline.main --crawl-limit 20
uv run python -m sict_crawler.pipeline.main --incremental --check-edits 100 --crawl-limit 200 --annotate-ocr-n 500

# 5. API + UI (http://localhost:8000)
uv run uvicorn sict_crawler.api.routes:app --port 8000

# 6. test (giống CI)
uv run pytest tests/ -q --ignore=tests/smoke_incremental.py
```

## Index lần đầu: snapshot offline → compute (GPU) → load

```bash
# 1. chỉ tải (CPU, mạng): HTML + ảnh vùng nội dung + file đính kèm + danh sách video; chạy lại = resume
python -m sict_crawler.pipeline.snapshot --out _snapshot/2026-10-12
# 2. tính toán nặng (máy GPU nếu có, tự rơi về CPU): parse → chunk → embed → OCR → keyframe → artifact
python -m sict_crawler.pipeline.compute --snapshot _snapshot/2026-10-12
# 3. nạp (CPU, không embed lại): PG + Qdrant sict_text_v2/sict_image_v2 + ảnh lên S3, kiểm tra số point
python -m sict_crawler.pipeline.loader --artifacts _snapshot/2026-10-12/artifacts/v2-multilingual-e5-small
```

Chuyển production sang v2: `scripts/cutover_v2.sh` (từng bước gọi tay; upload S3 cần
`CONFIRM_S3_UPLOAD=yes`), kiểm tra bằng `python3 scripts/eval_api.py --api http://<VM1>:8003`.

Artifact (`_snapshot/<ngày>/artifacts/<spec>-<model>/`) không phụ thuộc kho vector, id cố định (uuid5)
— nạp lại ghi đè, không nhân bản. Incremental về sau chạy trên CPU qua cùng quy tắc (`indexer.py`).

## Kiến trúc hiện tại

`frontier` (Redis BFS, dedup, politeness) → `crawler` (httpx → Obscura → crawl4ai) →
`parser` (markdown + table + img/video URLs) → `chunker` → `embedder` (E5 text 384-d, CLIP image 512-d)
→ `vector_store` (Qdrant `sict_text_v2`: E5 + BM25 sparse bỏ dấu, `sict_image_v2`: CLIP) + `db`
(PostgreSQL: metadata + `media`/`media_occurrence`/`videos`) + `storage` (S3). Ghi index qua
`indexer` (incremental) / `loader` (bulk) — cùng quy tắc `indexing` + `doc_rules`.

Mã nguồn chia theo trách nhiệm (import chỉ trỏ xuống tầng dưới): `sict_crawler/core` (cấu hình, chuẩn hoá chữ, BM25) → `model` (bảng ORM, schema API) → `repository` (Postgres, Qdrant, S3, Redis) → `service/{crawl,index,embedding,retrieval,graph}` (nghiệp vụ) → `pipeline` (job/CLI) | `api` (HTTP + UI); test ở `tests/`.

- Ảnh: tải → S3 `media/<sha256>.<ext>` → CLIP → `sict_image_v2` (1 point / 1 file, ảnh dùng chung
  nhiều bài giữ danh sách bài); mỗi lần xuất hiện có 1 caption = header bài + alt + OCR Tesseract
  → E5 → `sict_text_v2` (để hỏi bằng chữ ra ảnh). UI lấy ảnh qua `/media/s/<sha256>`.
- Graph thực thể (`graph.py`): người / sự kiện / CLB trích bằng luật từ tiêu đề, thân bài, alt riêng,
  đoạn văn sát ảnh, OCR → Postgres `entities`/`entity_links` (kèm đoạn trích bằng chứng) + payload
  Qdrant `entities`. Câu hỏi nêu "thầy X", "IT Festival 2023", "CLB HIT" → lọc thẳng chữ + ảnh/video
  gắn thực thể đó. Cạnh thực thể ↔ thực thể (`entity_relations`, suy từ cùng ảnh / cùng tiêu đề) cho
  câu tổng hợp ("thầy X tham gia sự kiện nào") và câu nêu 2 thực thể ("ảnh thầy X ở CLB Y").
  Dựng / cập nhật graph qua 1 đường duy nhất `graphsync` (Postgres là nguồn sự thật, payload Qdrant
  sinh lại từ PG); alias / chặn đã duyệt lưu ở `entity_aliases`, tên gần giống vào hàng chờ
  `entity_review`; graph chỉ dựng bằng luật (đã gỡ trích LLM `graph_llm`, 2026-10-18). Bộ vàng `eval/graph_gold_v1.jsonl` + `scripts/eval_graph.py`.
- Video: keyframe ffmpeg (≤20, dedupe CLIP) → xử lý như ảnh; transcript chỉ lấy phụ đề có sẵn
  (không ASR); không lưu video gốc.
- Incremental (`--incremental`): discover → recheck (so `hash_v2` markdown sạch) → drain, OCR ngay lúc
  index; chỉ xoá khi 404/410, cầu chì xoá hàng loạt,
  heartbeat Redis `incremental:last_ok`/`:last_error`.
- Query: `/query`, `/chat` (SSE) qua agent loop (rewrite → hybrid retrieve → LLM judge → answer);
  `/media/{id}`; cache Redis db 3.

Hạn chế đã biết (xử lý trong `plan.md`): parser không khớp theme `irs-*` của site nên lấy cả trang
(ảnh/video sidebar bị nhân bản), hash tính trên raw HTML, endpoint API chặn event loop, LLM chỉ nhận
300 ký tự mỗi chunk.

## Triển khai hiện tại (2 VM, c7i-flex.large 2 vCPU / 4 GB)

- VM1 `docker-compose.yml`: postgres, redis, qdrant, api (:8003), nginx edge cache (:8080).
- VM2 `docker-compose.worker.yml`: worker incremental (`cron`, 6h/lần), `svc-ocr`, Obscura sidecar;
  trỏ về VM1 qua `.env` (xem `.env.worker.example`).
- CD: GitHub Actions `workflow_dispatch` (tar + scp + build trên VM).

## Kiến trúc mục tiêu (đã chốt, xem `plan.md`)

- Parser khớp theme hiện tại; media dedupe (`media` + `media_occurrence`, video khoá theo YouTube id).
- **Qdrant** giữ vai trò kho vector (point id cố định, cutover blue/green bằng collection `*_v2`); tìm từ khoá
  không dấu qua payload `text_folded` + full-text index; Postgres giữ metadata/quan hệ.
- Embedding **ONNX Runtime int8**, không torch; CLIP text đa ngôn ngữ cho câu hỏi tiếng Việt.
- API không chặn event loop, LLM nhận full chunk, agent có deadline.
- Lọc metadata (mục, loại tài liệu, năm; ưu tiên mềm theo câu hỏi) và router thành phần câu trả lời
  (ý định + bằng chứng): không phải câu hỏi nào cũng kèm ảnh.
- Image build trong CI → GHCR (tag SHA); VM2 Spot/lịch bật-tắt; swap, pin version, bind cổng private.

## Config

`config.yaml` — seed URLs, connections, chunk budget, video threshold.
Override path: `SICT_CONFIG=/path/to/config.yaml`. Biến môi trường (`POSTGRES_DSN`, `REDIS_URL`,
`QDRANT_*`, `LLM_*`, `VLM_*`, `AWS_*`, `S3_ENDPOINT_URL`) ghi đè yaml khi deploy.
