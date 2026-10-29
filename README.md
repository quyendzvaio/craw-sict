# SICT Multimodal RAG

Hệ thống hỏi đáp trên toàn bộ nội dung website Trường Công nghệ Thông tin và Truyền thông —
Đại học Công nghiệp Hà Nội ([sict.haui.edu.vn](https://sict.haui.edu.vn/vn)): crawl cây trang →
tách chữ, bảng, ảnh, keyframe video, file đính kèm → PostgreSQL + Qdrant + kho đối tượng (S3/MinIO)
→ API RAG (`/query`, `/chat` stream SSE) kèm giao diện chat.

Hỏi được bằng chữ, bằng ảnh, hoặc cả hai; câu trả lời có trích dẫn `[n]` trỏ về bài viết / file
(kèm số trang) và ảnh, video liên quan khi câu hỏi cần.

> Dự án đang ở giai đoạn production hardening. Mọi quyết định thiết kế (D1–D30), lộ trình và cách
> verify/rollback nằm ở [`plan.md`](plan.md); tiêu chí nghiệm thu ở `tests/acceptance_*.md`.
> [`CLAUDE.md`](CLAUDE.md) là tài liệu kỹ thuật chi tiết nhất về từng thành phần.

## Tính năng

- **Crawl đủ cây trang**: hàng đợi BFS trên Redis (dedup, giãn cách theo host), giữ độ sâu khi phân
  trang, backend `httpx` → Obscura (trang dựng bằng JS) → crawl4ai. Parser khớp theme `irs-*` của
  site, bỏ sidebar, bộ đếm lượt xem.
- **Đa phương thức**: ảnh vùng nội dung (dedupe theo sha256), keyframe video bằng ffmpeg (≤ 20 khung,
  lọc trùng), phụ đề YouTube có sẵn (không ASR), OCR Tesseract tiếng Việt cho ảnh và PDF scan, file
  đính kèm PDF / DOCX / XLSX / PPTX / DOC / XLS (nhận dạng theo magic bytes vì site đặt sai đuôi).
- **Index có cấu trúc**: chunk theo khối DOM (heading, đoạn, bảng lặp hàng tiêu đề, danh sách), mỗi
  chunk mang `heading_path`; file đính kèm chunk theo trang để trích dẫn được "trang N".
- **Tìm kiếm lai**: dense + BM25 trên chữ đã bỏ dấu (sparse vector Qdrant, IDF), gộp RRF; câu gốc
  luôn được tìm, câu viết lại chỉ bổ sung. Ưu tiên mềm theo mục, năm, "mới nhất" suy từ câu hỏi;
  lọc cứng khi người gọi truyền `filters`.
- **Graph thực thể bằng luật** (người, sự kiện, CLB): lọc chữ và ảnh/video theo thực thể nêu trong
  câu hỏi ("ảnh thầy X ở CLB Y"), thêm dữ kiện quan hệ vào ngữ cảnh. Không dùng LLM để trích.
- **Guardrail không phụ thuộc LLM**: đa dạng tài liệu trong top k, câu liệt kê lấy rộng hơn, bài áp
  đảo được thêm đoạn, cờ `sufficient` tính bằng luật (cosine + độ phủ từ khoá). Đổi model LLM không
  đổi kết quả truy xuất.
- **Router media**: chỉ kèm ảnh/video khi câu hỏi cần (`none | optional | required`) và khi có bằng
  chứng (ngưỡng điểm thô theo từng cặp phương thức).
- **Cập nhật tăng dần an toàn**: phát hiện bài mới / bài sửa theo hash nội dung đã làm sạch, chỉ xoá
  khi HTTP 404/410, cầu chì xoá hàng loạt, reconcile Postgres ↔ Qdrant hằng đêm.

## Kiến trúc

```
                 ┌──────────── ingest (worker) ────────────┐
sict.haui.edu.vn → frontier → crawler → parser → indexing/chunk → embedder ─┐
                   (Redis)              │                                   │
                                        ├→ PostgreSQL  (documents, chunks, media, graph — nguồn sự thật)
                                        ├→ S3 / MinIO  (HTML gốc, ảnh, file đính kèm)
                                        └→ Qdrant      (text / image / ocr: dense + BM25) ←┘

câu hỏi (+ ảnh) → /query, /chat → agent: rewrite (luật) → retrieve (hybrid + graph + media router)
                                        → dựng ngữ cảnh đánh số [n] → LLM (stream) → trích dẫn
```

Hai đường ghi index dùng **cùng một bộ luật** (`service/index/indexing.py`, `doc_rules.py`, id điểm
uuid5 cố định nên nạp lại là ghi đè, không nhân bản):

- **Bulk** (D16): `snapshot` (chỉ tải, resume được) → `compute` (parse, chunk, embed, OCR, keyframe →
  artifact `.jsonl` + `.npy`, chạy được trên máy GPU thuê) → `loader` (nạp PG + Qdrant, không embed lại).
- **Incremental**: `pipeline.main --incremental` chạy định kỳ trên worker, ghi qua `indexer.index_page`.

### Embedding

| Index | Backend | Mô hình | Ghi chú |
|---|---|---|---|
| v2 (`*_v2`, mặc định cấu hình) | `torch` | multilingual-e5-small 384-d (chữ) + CLIP ViT-B/32 512-d (ảnh) | |
| v3 (`*_v3`) | `api` | jina-clip-v2 1024-d, chữ và ảnh chung một không gian | dịch vụ tự host ONNX int8 trên CPU (`embed_server.py`, cổng 8090), cosine so với fp32: chữ 0,999, ảnh 1,000 |

Chuyển giữa hai bên chỉ là cấu hình (`embedding.backend`, `QDRANT_COLLECTION_*`); cutover kiểu
blue/green, rollback = trỏ API về collection cũ.

## Cấu trúc mã nguồn

Import tuyệt đối và chỉ trỏ xuống tầng dưới; `api` và `pipeline` là hai tầng trên cùng, không import nhau.

| Gói | Nội dung |
|---|---|
| `sict_crawler/core/` | cấu hình (yaml + env → dataclass), bỏ dấu, tokenizer BM25 |
| `sict_crawler/model/` | bảng SQLAlchemy, schema pydantic (`QueryIn`, `Source`) |
| `sict_crawler/repository/` | Postgres, Qdrant, S3 / kho local, hàng đợi Redis, cache Redis |
| `sict_crawler/service/crawl/` | crawler, parser HTML → markdown + khối, phân loại URL |
| `sict_crawler/service/index/` | luật index v2, chunker, OCR, video, đường ghi `indexer` |
| `sict_crawler/service/embedding/` | client embedding (torch / HTTP), dịch vụ ONNX tự host |
| `sict_crawler/service/retrieval/` | retriever, agent, router media, bộ lọc, trích dẫn, gọi LLM |
| `sict_crawler/service/graph/` | luật trích thực thể, `graphsync`, catalog CLB / sự kiện |
| `sict_crawler/pipeline/` | CLI: crawl, incremental, snapshot, compute, loader, reconcile |
| `sict_crawler/api/` | FastAPI, `/metrics`, giao diện chat tĩnh |
| `tests/` | pytest, fixture HTML thật, Gherkin, tiêu chí nghiệm thu |
| `scripts/` | đánh giá, nghiệm thu qua HTTP, cutover, giám sát |
| `eval/` | bộ câu hỏi vàng, bộ câu liệt kê, bộ vàng graph, kết quả đo |

## Chạy local

Yêu cầu: Docker, [uv](https://docs.astral.sh/uv/) (dependency **chỉ** quản lý bằng uv, không pip),
Python ≥ 3.10. Nhóm dependency: `dev` (pytest), `api` (FastAPI), `ml` (torch CPU, sentence-transformers,
OCR, yt-dlp), `embed` (dịch vụ ONNX), `eval` (RAGAS).

```bash
cp .env.example .env                         # điền LLM_API_BASE / LLM_MODEL / LLM_API_KEY nếu cần câu trả lời sinh
docker compose up -d postgres redis qdrant   # hạ tầng
uv sync --group dev --group api              # thêm --group ml để embed / OCR / video

# crawl thử 20 trang, hoặc một chu kỳ incremental
uv run python -m sict_crawler.pipeline.main --crawl-limit 20
uv run python -m sict_crawler.pipeline.main --incremental --check-edits 50 --crawl-limit 200 --budget-sec 300

# API + giao diện chat tại http://localhost:8000
uv run uvicorn sict_crawler.api.routes:app --port 8000
```

Không có thông tin AWS thì file lưu vào `./_local_store`. Không có khoá LLM thì `/chat` trả lại
ngữ cảnh đã truy xuất thay vì câu trả lời sinh.

Index hàng loạt từ đầu:

```bash
python -m sict_crawler.pipeline.snapshot --out _snapshot/2026-10-12        # chạy lại = resume
python -m sict_crawler.pipeline.compute  --snapshot _snapshot/2026-10-12   # stages docs,text,images,videos
python -m sict_crawler.pipeline.loader   --artifacts _snapshot/2026-10-12/artifacts/v2-multilingual-e5-small
python -m sict_crawler.service.graph.graphsync rebuild                      # graph thực thể
python -m sict_crawler.pipeline.reconcile --dry-run                         # kiểm PG ↔ Qdrant
```

## API

| Endpoint | Mô tả |
|---|---|
| `POST /query` | `{question, top_k, image?, filters?}` → nguồn chữ, media, route, `sufficient`, trích dẫn |
| `POST /chat` | cùng đầu vào, trả SSE: `status` → `sources` → các mảnh câu trả lời → `citations` → `[DONE]` |
| `GET /media/s/{sha256}` | ảnh / keyframe (index v2/v3) |
| `GET /media/{id}` | ảnh index v1 |
| `GET /metrics` | Prometheus: request, độ trễ, heartbeat pipeline, số tài liệu, hàng đợi |
| `GET /` | giao diện chat |

`image` là base64 (không tiền tố `data:`); `filters` nhận `sections`, `doc_types`, `year_from`, `year_to`.

```bash
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "Trường có những ngành đào tạo nào?"}'
```

## Cấu hình

`config.yaml` (đổi đường dẫn bằng `SICT_CONFIG`) nạp vào dataclass ở `core/config.py`; biến môi
trường ghi đè khi triển khai:

| Biến | Dùng cho |
|---|---|
| `POSTGRES_DSN`, `REDIS_URL`, `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_GRPC_PORT` | kết nối hạ tầng |
| `QDRANT_COLLECTION_TEXT` / `_IMAGE` / `_OCR` | collection đang phục vụ (`*_v2`, `*_v3`) |
| `LLM_API_BASE`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_MAX_NEW_TOKENS` | LLM trả lời, API tương thích OpenAI |
| `EMBED_API_BASE`, `EMBED_MODEL`, `EMBED_DIM`, `EMBED_API_KEY` | backend embedding `api` |
| `AWS_PROFILE` hoặc `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL` | kho đối tượng (S3 / MinIO) |
| `CRAWLER_BACKEND` | `auto` \| `httpx` \| `obscura` \| `crawl4ai` |

Model suy luận (glm, deepseek…) tốn 300–700 token để "nghĩ" trước khi trả lời, nên giữ
`llm.max_new_tokens` ≥ 2048.

## Kiểm thử và đánh giá

```bash
uv run pytest tests/ -q --ignore=tests/smoke_incremental.py   # giống CI
uv run python -m tests.test_safety                             # 3 script này CI chạy riêng
uv run python -m tests.test_captions
uv run python -m tests.test_agent
```

| Bộ | Script | Kết quả gần nhất (index v3) |
|---|---|---|
| `eval/golden_v1.jsonl` (70 câu, URL đáp án, chế độ media) | `scripts/eval_retrieval.py` (offline), `scripts/eval_api.py` (HTTP) | recall@5 0,985 · MRR@10 0,898 · chế độ media 0,871 |
| `eval/list_v1.jsonl` (12 câu liệt kê + 8 câu ngoài phạm vi) | `scripts/eval_api.py --list` | độ phủ mục 0,970 · `sufficient` đúng 0,95 |
| RAGAS trên golden + list | `scripts/eval_ragas.py collect / score` | faithfulness 0,884 · answer relevancy 0,750 · context precision 0,735 · context recall (list) 0,937 |
| `eval/graph_gold_v1.jsonl` (150 tài liệu, nhãn còn ở dạng draft) | `scripts/eval_graph.py`, CI gate `tests/test_eval_graph.py` | chờ người duyệt nhãn (`eval/GRAPH_ANNOTATION.md`) |
| Nghiệm thu U1–U6 qua HTTP | `scripts/accept_api.py --api URL` | 54/54 |

Chạy đánh giá trước khi đổi model, ngưỡng hay luật chunk. Lịch sử số đo ở `eval/RESULTS.md` và
`eval/results/`.

## Triển khai

Hai VM, Docker Compose (không k8s, D15):

- **VM1** `docker-compose.yml`: postgres, redis, qdrant, api (cổng 8003), nginx cache (8080).
- **VM2** `docker-compose.worker.yml`: `cron` chạy incremental mỗi 12 giờ, tối đa 5 phút / lượt (D31), OCR, Obscura sidecar;
  dịch vụ embedding `docker-compose.embed.yml`. Trỏ về VM1 qua `.env` (`.env.worker.example`).
- **CI/CD**: `ci.yml` chạy test và build thử image trên mọi PR; `release.yml` đẩy
  `ghcr.io/<owner>/sict-{api,worker,embed}:<sha>` khi merge vào `main`; `cd.yml` (chạy tay, chỉ
  `main`) kéo image theo SHA về VM, không build trên VM.
- **Giám sát**: Grafana Alloy trên mỗi host đẩy metric + log lên Grafana Cloud; dashboard sinh từ
  code (`scripts/grafana_dashboards.py`).

Postgres, Redis, Qdrant trong compose mở cổng để worker ở VM2 kết nối: chỉ cho phép mạng private
(security group / firewall) và đổi mật khẩu mặc định `sict` trước khi chạy thật.

## Quy ước đóng góp

- Nhánh `feat/…` / `fix/…` / `docs/…` → PR vào `develop` → CI xanh → merge; `main` chỉ nhận bản phát hành.
- Comment, tài liệu, commit viết tiếng Việt.
- Thay đổi parser phải kèm fixture HTML thật (`tests/fixtures/`).
- Không commit `.env`, cookie YouTube, khoá; ví dụ cấu hình nằm ở `.env.example`.

## Dữ liệu

Nội dung crawl thuộc về Trường CNTT&TT — Đại học Công nghiệp Hà Nội. Repo chỉ chứa mã nguồn và các
bộ câu hỏi đánh giá; dữ liệu crawl, snapshot và index không nằm trong repo.
