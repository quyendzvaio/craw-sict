# SICT Multi-Modal Crawler

Crawl `https://sict.haui.edu.vn/vn` (toàn bộ tree) → text + table + image + video keyframes
→ PostgreSQL + Qdrant + AWS S3 → RAG Q&A API + Chat UI.

## Chạy

```bash
# 1. hạ tầng
docker compose up -d postgres redis qdrant

# 2. python deps (quản lý bằng uv, không dùng pip/requirements.txt)
uv sync   # thêm --group ml để có torch/E5/CLIP/OCR, --group api cho FastAPI

# 3. AWS S3 credentials
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# 4. crawl thử 20 trang
python -m sict_crawler.main --crawl-limit 20

# 5. API + UI
uvicorn sict_crawler.api:app --port 8000
# mở http://localhost:8000

# 6. test
python -m sict_crawler.tests.test_parser
```

## Kiến trúc

`frontier` (Redis BFS, dedup, politeness) → `crawler` (crawl4ai/Playwright) →
`parser` (markdown + table + img/video URLs) → `chunker` → `embedder` (E5 text, CLIP image)
→ `vector_store` (Qdrant) + `db` (PostgreSQL) + `storage` (S3).

- Image: tải → S3 + **base64** lưu PG → CLIP embed → Qdrant collection `sict_image`
- Video: ffmpeg scene keyframes → S3 + base64 → CLIP embed → **retrieval như image**
- Query: `/chat` SSE, hybrid text + cross-modal image search

## Config

`config.yaml` — seed URLs, connections, chunk budget, video threshold.
Override path: `SICT_CONFIG=/path/to/config.yaml`.
