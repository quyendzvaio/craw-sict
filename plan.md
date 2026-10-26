# Plan — SICT RAG: production hardening (Đợt 3)

Ngày chốt: 2026-10-12. Trạng thái: **ĐÃ CHỐT, chưa implement**.
Nguyên tắc: sửa theo thứ tự phụ thuộc, mỗi bước verify được, mỗi bước có rollback.
Repo đã là git → mỗi giai đoạn 1 branch/PR riêng. Trước mọi bước đụng DỮ LIỆU:
`pg_dump` + snapshot Qdrant lên S3 (code rollback bằng git, data rollback bằng backup).
Tiêu chí chấp nhận: `tests/acceptance_criteria.md` (AC-B/E/G/H/O) và
`acceptance_retrieval_response.md` (AC-R/S/K/P), kịch bản ở `tests/features/`.

## Bối cảnh — bằng chứng (snapshot PG local, crawl tới 2026-09-15)

| Chỉ số | Giá trị | Ý nghĩa |
|---|---|---|
| documents | 2.806 (960 là `/vn/tags?q=…`, 800 từ `www.haui.edu.vn`, 12 trang lỗi ASP.NET) | corpus nhiễu |
| chunks | 19.710 (2.201 trùng nội dung, TB 1.393 ký tự) | boilerplate; LLM chỉ thấy 300 ký tự/chunk |
| ảnh | 44.414 dòng / 8.760 ảnh khác nhau; top 5 ảnh ~2.000 lần mỗi ảnh | ~80% xử lý trùng |
| frame video | 27.072 dòng / 16 video / 252 frame khác nhau; 1 video nhúng ở 1.673 trang | ~99% xử lý video trùng |
| asset kẹt OCR | 993 frame bị pick lại mỗi chu kỳ, mỗi lần thêm 1 point Qdrant trùng | Qdrant phình, backlog nghẽn |

**Gốc rễ chung — parser không khớp theme hiện tại của site.** Không selector nào trong
`_CONTENT_SELECTORS` khớp (theme dùng class `irs-*`) → fallback lấy CẢ trang; sidebar class
`irs-side-bar` không bị strip (selector chỉ có `sidebar`). Thử trên bài
`/vn/tin-tuc/…/71898`: thêm `div.irs-blog-single-col` + strip `side-bar` → ảnh 28→10,
video sidebar 1→0, markdown 15.099→8.425 ký tự. Bộ đếm lượt xem nằm trong HTML → hash raw
đổi mỗi lần có người đọc → trang bị coi là "sửa" và xử lý lại toàn bộ.

Phần cứng: VM1, VM2 = c7i-flex.large (2 vCPU, 4 GB). Tổng `mem_limit` VM1 = 3.650 MB
(gần hết RAM), VM2 = 6.150 MB (vượt RAM nếu svc-ocr + cron chạy cùng lúc).

## Quyết định đã chốt

| # | Quyết định | Thay cho |
|---|---|---|
| D1 | Sửa parser theo theme `irs-*` là bước đầu; dedupe media là lớp phòng thủ thứ 2 | dedupe đơn thuần |
| D2 | Mô hình `media` (unique theo sha256) + `media_occurrence`; video khóa theo YouTube id | 1 dòng media / 1 lần xuất hiện |
| D3 | Hash v2 = nội dung chính đã parse (bỏ ngày giờ/lượt xem) + bảng + URL media đã sort | sha256 raw HTML |
| D4 | Trạng thái OCR tường minh (`ocr_status`, `ocr_attempts`) | dò chuỗi `sub:`/`ocr:` trong caption |
| D5 | `/vn/tags` và trang lỗi không vào corpus; media/chunk lặp ở > N bài (mặc định 20) là trang trí | index mọi thứ |
| D6 | API: endpoint chạy threadpool + semaphore, 1 uvicorn worker, warm-up + `/readyz`; LLM nhận full chunk theo ngân sách `llm.context_chars`; agent bỏ attempt `top_k×2`, thêm deadline; bỏ rerank E5 (cross-encoder sau cờ, mặc định tắt) | async def chặn event loop; excerpt 300 ký tự |
| D7 | Embedding chạy **ONNX Runtime**, gỡ torch khỏi cả API lẫn worker; CLIP text → `sentence-transformers/clip-ViT-B-32-multilingual-v1` (cùng không gian ảnh CLIP B/32). **Device tự chọn** (`embedding.device: auto`, env `EMBED_DEVICE`): có GPU → GPU (model fp16), không có → CPU (model int8); dò 1 lần lúc nạp model, ép `cuda` mà không có GPU → rơi về CPU. Image mặc định **bản CPU**; bản GPU là build-arg riêng + `docker-compose.gpu.yml`, chỉ dùng khi máy có GPU | torch + CLIP text tiếng Anh, device cố định `cpu` |
| D8 | **Giữ Qdrant làm kho vector** (chốt lại 2026-10-12, KHÔNG chuyển pgvector); Postgres giữ metadata/quan hệ. Nhất quán PG↔Qdrant: point id uuid5 cố định từ artifact (upsert ghi đè, chạy lại không nhân bản) + đối soát hằng đêm (AC-E6). Keyword không dấu: **sparse vector BM25** (`bm25`, `modifier=idf`) trên token đã bỏ dấu — cùng tokenizer với eval, 1 lượt gọi có xếp hạng (sửa 2026-10-14; bản nháp `text_folded` + full-text index chỉ lọc, không xếp hạng được). RAM: scalar quantization int8 (bản quantized trong RAM, vector gốc on-disk). Cutover blue/green bằng collection mới `*_v2` | id uuid4 ngẫu nhiên, `MatchText` không index + từ khoá bỏ dấu không khớp chữ có dấu |
| D9 | Giữ 2 VM; VM2 chạy Spot hoặc bật/tắt theo lịch; **1 đường triển khai** trên VM2 (compose `cron`; CD gỡ `sict-worker-24h` từ 2026-10-21), bỏ `sict-worker-24h`/`loop24h.sh` | 2 đường deploy song song |
| D10 | CI build image → GHCR, tag theo commit SHA; VM chỉ `pull`; rollback = đổi tag — **đã làm 2026-10-21** (`release.yml`, `cd.yml`; chưa chạy trên VM) | tar + scp + build trên VM 4 GB |
| D11 | Swap 2 GB trên cả 2 VM; pin version mọi image (bỏ `:latest`) — **ghim xong 2026-10-21** trừ `minio/mc` (profile minio local) | không swap, `:latest` |
| D12 | Bảo mật: bind cổng DB/cache vào IP private (hoặc SG chỉ cho VM2), Redis `requirepass`, Qdrant API key (`QDRANT__SERVICE__API_KEY`), mật khẩu PG mạnh, LLM qua TLS/tunnel | cổng 0.0.0.0, `sict/sict`, Qdrant không khoá, HTTP thường |
| D13 | Giám sát (chốt 2026-10-21): **Grafana Cloud Free** (Prometheus/Mimir + Loki + Grafana host sẵn) — mỗi VM 1 Grafana Alloy (systemd, ~50 MB RAM) đẩy metric (60 s/lần: gói Free tính 1 điểm/phút/chuỗi) + log; nhãn `instance` = `sict-vm1` / `sict-vm2`; token ở `/etc/alloy/secrets.env` (root 600) qua systemd EnvironmentFile, config dùng `sys.env`. Template `monitor/alloy/`, render `scripts/render_alloy.py`, triển khai `scripts/deploy_alloy_vm.sh`, giả lập 2 VM local `scripts/monitor_sim.sh`, kiểm chứng phía cloud `scripts/verify_gcloud.py`. Không cảnh báo (chỉ dashboard). Không chạy Prometheus/Grafana trên VM (VM1 4 GB) | `docker-compose.monitor.yml` (Prometheus/Grafana local, thư mục `monitor/` cũ không còn) |
| D14 | Bucket S3 cùng region với VM — đã đúng: VM1, VM2 và bucket `sict-crawl-media-quyen-ohio` đều ở `us-east-2` (kiểm tra 2026-10-20) | ghi nhầm VM ở `ap-southeast-2` |
| D15 | **Chỉ dùng Docker Compose**, không k8s. Lịch chạy = service `cron` trong compose (không crontab host, không CronJob) | k8s (đã gỡ), crontab host `auto_process.sh` |
| D16 | **Index lần đầu = snapshot offline → compute trên máy GPU → load**: (1) `python -m sict_crawler.pipeline.snapshot` chỉ tải HTML + ảnh vùng nội dung + danh sách video (CPU, mạng); (2) máy GPU thuê theo giờ chạy parse → chunk → embed → OCR → keyframe từ snapshot, xuất artifact (chunk + vector) ra file; (3) load artifact vào DB/vector store trên CPU. Incremental về sau chạy inline trên CPU | crawl + xử lý chung 1 vòng trên VM 2 vCPU |
| D17 | **Router thành phần câu trả lời — 2 tín hiệu**. (1) *Ý định* (trước tìm kiếm): bước viết lại câu hỏi trả JSON `{rewritten, media: none\|optional\|required, wants_file}` — không thêm lượt gọi LLM; không có LLM → luật từ khoá. `none` → bỏ hẳn nhánh media. (2) *Bằng chứng* (sau tìm kiếm): `optional` chỉ lấy media thuộc các bài đứng đầu nhánh chữ, tối đa 3; `required` tối đa `top_k`; mọi media phải vượt ngưỡng điểm gốc theo loại (CLIP, caption E5 — không cắt bằng điểm RRF), ngưỡng hiệu chỉnh trên bộ câu hỏi vàng; không đạt → media rỗng + ghi chú. Response có `media_mode`; UI chỉ hiện khung ảnh khi có media (AC-R1, R8–R10, R12) | luôn trả 2 chân, top_k ảnh xếp theo RRF không ngưỡng, UI hiện mọi ảnh |
| D18 | **Lọc metadata 2 tầng** (`filters.py`): lọc cứng (mục, loại tài liệu, khoảng năm) chỉ khi caller truyền `filters`; suy từ câu hỏi (mục, năm, "mới nhất") chỉ nâng điểm. Payload index `section`/`doc_type`/`year`; ngày đăng vào ngữ cảnh LLM (AC-R11) | không có metadata |
| D19 | **Index spec v2** (`indexing.py`) dùng chung cho bulk compute và incremental: header ngữ cảnh `[tiêu đề \| mục \| ngày]`, bỏ URL trong link, bảng cắt theo nhóm dòng lặp header (bỏ bảng nối thêm lần 2), không index trang listing/trang lỗi/trùng nội dung, lọc chunk boilerplate > N bài, file đính kèm pdf/docx/xlsx/pptx thành tài liệu `attachment` kế thừa mục + ngày của trang cha | markdown thô, bảng lặp 2 lần, đính kèm bị bỏ |
| D20 | **Graph thực thể nối chữ ↔ ảnh/video** (`graph.py`, chốt 2026-10-14): nút = tài liệu, media, video, thực thể (người / sự kiện / CLB); cạnh `about` (tiêu đề), `mentions` (thân bài), `depicts` (ảnh: tên trong alt riêng / đoạn văn sát ảnh / OCR = mạnh, ảnh thuộc bài về thực thể = yếu) kèm trọng số + nguồn + đoạn trích bằng chứng. Trích bằng luật (học hàm/vai trò + họ Việt, tên trong ngoặc + từ khoá sự kiện, "CLB …"), không model/LLM → bulk và incremental cho cùng kết quả. Lưu PG (`entities`, `entity_links`, `entity_relations` thực thể ↔ thực thể) + payload Qdrant `entities`/`entities_strong`; truy xuất: khớp danh bạ trên câu hỏi → thêm 1 danh sách vào RRF + nhánh media lọc theo thực thể. Không dùng graph DB riêng (Neo4j) — 1–2 bước nhảy, PG + payload đủ, RAM 4 GB | alt 85% chỉ lặp tiêu đề bài, CLIP text tiếng Anh không nhận ra tên người/sự kiện Việt |
| D21 | **Hệ use case đa phương thức (chốt 2026-10-17)**, tất cả cùng một không gian vector jina: U1 text→text, U2 text→image, U3 image→image, U4 image→text (vector ảnh tìm chunk/caption và bài chứa ảnh gần trùng), U5 text+image→text, U6 text+image→image, U7 text+image→text+image. Đầu vào/đầu ra = `Route.inputs/outputs` (từ router + ý định); truy vấn chỉ có ảnh → ra cả ảnh và chữ. Ngưỡng tách theo cặp (`t2i_min`, `i2i_min`, `i2t_min`, `t2t_caption_min`, `dup_min`), thay `clip_min`/`caption_min`. Ảnh tới LLM trả lời chỉ khi bật `llm.answer_with_image` (mặc định: chỉ retrieval) | ảnh chỉ vào chân CLIP |
| D22 | **Embedding = một model đa phương thức `jina-clip-v2`** (1024-d, Matryoshka, 8k token, có tiếng Việt), qua **cổng embedding cấu hình được** (`embedding.backend=api`, `EMBED_API_BASE/KEY/MODEL/DIM`): lần preprocessing đầu dùng endpoint cloud; sau đó **tự host ONNX Runtime int8** (`model_int8.onnx` ~874 MB) sau cùng giao diện, đổi nguồn chỉ là đổi cấu hình (endpoint do owner cấp sau). Trọng số tự host là CC BY-NC 4.0 → owner đã xác nhận phạm vi dùng (2026-10-21). Index mới `sict_text_v3`/`sict_image_v3`, blue/green như D8; v2 giữ để rollback. Prefix query viết tay, corpus không prefix; cache vector theo hash đầu vào | D7 (CLIP B/32 + E5) |
| D23 | **Giữ cấu trúc trong trang** (chốt 2026-10-17; không lưu cây site): parser sinh `blocks` theo thứ tự DOM (heading có cấp + `heading_path`, đoạn, danh sách, bảng, ảnh, video, file đúng vị trí); chunk mang `heading_path` + `block_range`, bảng thừa hưởng heading của mục chứa nó; `hash_v2` giữ nguyên (cache LLM còn dùng) và thêm `hash_struct` (outline + URL media/file) để sửa ảnh/file cũng kích hoạt index lại. **Lưu trữ: raw (HTML gz, ảnh, file đính kèm) ở object store MinIO (S3 tương thích, `S3_ENDPOINT_URL`); Postgres giữ "source"**: tài liệu, `structure` JSONB (outline block), khoá object store, quan hệ ảnh/video/file ↔ block | chunk chỉ có 1 heading, mất vị trí media |

| D24 | **Trích dẫn bắt buộc + số trang + ảnh→trang (chốt 2026-10-17)**: mã trang = URL bài (kèm `heading_path`) với trang web; **số trang thật** (từ 1) với file PDF/PPTX — `ix.extract_file_pages` tách theo `\f` của pdftotext / theo slide / theo trang OCR, chunk file đính kèm theo trang (`[trang N]`, payload `page`) khi `chunking.structured`. Ngữ cảnh LLM đánh số nguồn `[n]` kèm nhãn `cite` ("Tên file — trang N" / "Tiêu đề › Mục"); `citations.enforce` kiểm lại phía server (bỏ `[n]` ảo, không có `[n]` thì ghép dòng "Nguồn:"), `/chat` phát thêm sự kiện `citations`, `/query` trả `citations`. Ảnh người dùng gửi → ảnh giống (i2i) → bài chứa → chunk có block nằm trong/sát block ảnh (`_chunks_around`, vị trí lấy từ điểm caption), không có vị trí thì rơi về chunk giống vector ảnh. Tìm trang bằng chữ trước rồi mới lấy ảnh (text→image trực tiếp còn yếu). Không crop ảnh trong PDF | trích dẫn tuỳ ý |
| D25 | **Siết luật graph + danh mục chuẩn (2026-10-18)**: tên người trong bài phải viết hoa, "tên đệm + tên" chỉ sau thầy/cô/TS…, địa danh đứng trước chặn tên; `<br>` trong ô bảng → "; ", cột PDF → " | "; cán bộ lấy từ tiêu đề hồ sơ + đối chiếu slug. CLB / chuỗi sự kiện theo danh mục chuẩn `sict_crawler/service/graph/graph_catalog.yaml` (dữ liệu, chủ dự án duyệt) khớp cả thân bài; "Chung kết/Cuộc thi X" = X. Quan hệ: trọng số giảm chấn theo số bài, sự kiện/CLB 1 bài ngoài danh mục không tạo quan hệ | luật thuần trích từ tiêu đề |
| D26 | **Duyệt trước cutover v3 (chủ dự án, 2026-10-21)**: `graph_catalog.yaml`, bộ 12 câu `eval/compare_q12.jsonl`, ngưỡng router hiện tại (`i2i_min` 0,6 …, vẫn hiệu chỉnh tiếp khi có dữ liệu), giấy phép CC BY-NC 4.0 của jina-clip-v2 — đã duyệt. VM2 nâng lên **8 GB** khi triển khai thật (đặt dịch vụ embed int8). Cặp người gần trùng: chủ dự án giao Claude tự duyệt (2026-10-22) → 766 dòng / 699 cặp: **7 gộp** (biến thể OCR / gõ sai tên cán bộ: Kim Son, Kèm Sơn → Nguyễn Thị Kim Sơn; Lương Thi Hong Lan; Nguyễn Hữu Quynh; Phạm Xuân Thanh; Phạm Văn Bong; Đặng/Đăng Quỳnh Nga), **759 tách** (khác họ / tên đệm, hai dòng khác mã SV, cùng bài nêu cả hai tên); quyết định ở `eval/review/near_dup_pairs_2026-10-22.csv`, áp lên DB khác bằng `graphsync review-import` (khớp theo khoá khi id khác) | đã duyệt |
| D27 | **Guardrail truy xuất không phụ thuộc LLM, bỏ LLM chấm ngữ cảnh (chủ dự án, 2026-10-25)**: câu "Trường có những ngành đào tạo nào?" chỉ ra 3 ngành vì (1) chỉ tìm bằng câu LLM viết lại (đổi model là đổi kết quả), (2) top 5 bị vài bài chiếm, (3) LLM chỉ thấy 300 ký tự mỗi đoạn, (4) LLM chấm "đủ ngữ cảnh" không thấy phần thiếu (chấm "đủ" cả 6/8 câu ngoài phạm vi) và tốn 1–3 lượt gọi. Sửa: câu gốc luôn được tìm, câu viết lại chỉ thêm danh sách RRF; `retriever.diversify` (≤ 2 đoạn / bài); câu liệt kê (`router.is_list_question`) ≥ 10 nguồn, 1 đoạn / bài; `Source.content` đầy đủ trong ngân sách `llm.source_chars` 1500 / `context_chars` 9000; 1 lượt truy xuất, `sufficient` = luật `agent.evidence` (cosine ≥ 0,45, phủ từ câu hỏi ≥ 0,8 — TẠM, hiệu chỉnh trên 89 câu); cache `q:v4`. Đo 2026-10-25 (index v3, `eval_api.py`): golden recall@5 / MRR — cũ không LLM 0,985 / 0,891, cũ deepseek 0,970 / 0,857; mới không LLM 0,985 / 0,898, mới deepseek 0,985 / 0,888, mới qwen3.8-27b 0,985 / 0,896 (lệch giữa các LLM: MRR 0,010, trước 0,034). `eval/list_v1.jsonl` (12 câu liệt kê + 8 ngoài phạm vi, nháp): độ phủ mục 0,424 / 0,53 → 0,724 / 0,736 / 0,744; `sufficient` đúng 0,6 → 0,95 / 0,9 / 0,95. `/chat` 2 lượt LLM (trước 2–5), trung vị ~20 s với deepseek miễn phí (trước ~30–36 s; ngữ cảnh dài hơn làm lượt trả lời chậm hơn). Còn thiếu: liệt kê đủ một danh mục (VD 9 ngành, chỉ 3–6 lọt top 10 vì thời khoá biểu chứa mọi tên ngành) cần nguồn có cấu trúc (menu site), chưa làm | đã làm |
| D28 | **Giảm độ trễ truy xuất (chủ dự án, 2026-10-25)**: (1) ảnh truy vấn 392 → **336 px** (dịch vụ int8, 40 ảnh: 4,26 s vs 6,09 s / ảnh, hạng 1 40/40 vs 39/40, cosine thấp nhất 0,783; NHƯNG `accept_api` U6 q15 — ảnh PNG đồ hoạ trang Thông tin chung — tụt: ảnh gốc hạng 2, bài chứa ảnh rơi khỏi top 3, 392 thì hạng 1; quay lại = `EMBED_QUERY_IMAGE_SIZE=392` + `EMBED_QUERY_IMAGE_SIZES=336,392`); (2) lượt Qdrant chạy song song 3 tầng (giai đoạn `retriever._STAGE_POOL` → nhánh `vector_store.parallel` → collection `_per_collection`); (3) **tắt LLM viết lại câu hỏi** (`llm.rewrite_query`, bật lại `LLM_REWRITE_QUERY=1`; golden 0,985 / MRR 0,898, liệt kê 0,724 — y như không LLM), router dùng luật. Đo trên máy dev: truy xuất 4,4–23,6 s → 0,9–7,2 s (chữ 0,9–2,4 s; ảnh 7,1 s vì nhúng ảnh 5 s); `/query` không còn gọi LLM; `/chat` toàn trình không giảm rõ (trung vị ~24 s) vì lượt LLM trả lời của nhà cung cấp miễn phí dao động 6–33 s trước chữ đầu. Cache `q:v5`. Chủ dự án chốt 336 ngày 2026-10-26, chấp nhận U6 q15 tụt | đã chốt |

### Câu hỏi còn mở (không chặn Giai đoạn 0–1)
- ~~**Q1** Phạm vi `www.haui.edu.vn`~~ → **chốt 2026-10-12: chỉ SICT** (`external_hosts: []`, thêm `/vn/tags` vào `skip_patterns`).
- **Q2** Context window của model `sict`: đo rồi đặt `llm.context_chars` (tạm giữ 6.000).
- **Q3** RAM cho cross-encoder: đo lại sau Giai đoạn 2 (ONNX) rồi quyết.
- **Q4** Region thật của 2 VM: kiểm tra trước D14.

## Snapshot offline + compute (D16–D19) — đang làm từ 2026-10-12
Đã xong (có test):
- Parser khớp theme `irs-*` + ngày đăng, nhãn mục, link đính kèm (fixture `tests/fixtures/`).
- `snapshot.py`: resume qua `state.sqlite`, retry backoff, chỉ host SICT, phân trang giữ depth (crawler
  cũ bị chặn ở ~5 trang/mục do site chỉ link trang kế tiếp); phase 3 tải file đính kèm (quét lại HTML
  đã lưu nên snapshot đang chạy cũng bổ sung được).
- `indexing.py` (D19), `compute.py` (stage docs/text/images/videos → artifact JSONL + `.npy`, id uuid5
  cố định, OCR cache để resume, `embedding.device: auto`).
- `filters.py` (D18) nối vào `api.retrieve` / `agent` / `vector_store`; cache key `q:v2` gồm filters.

Đã xong thêm (2026-10-12, có test — 117 test pass):
- Router D17: `router.py` (luật từ khoá + gộp JSON từ lượt LLM viết lại `vlm.rewrite_and_route`,
  `gate` chặn theo bằng chứng, 1 video = 1 vị trí), nối vào `api.retrieve` (mức `none` bỏ hẳn nhánh
  media + loại caption khỏi nguồn chữ), `agent` (`media_mode`, `wants_file`, `media_note`), UI (ghi chú
  khi không có ảnh đạt, link tải file + trang chứa file). Ngưỡng ở `config.yaml: router` (TẠM).
  Luật dự phòng trên bộ vàng: 87% đúng mức media, 100% câu `required`/muốn file, 0 câu thông tin bị ép ảnh.
- Bộ câu hỏi vàng `eval/golden_v1.jsonl` (70 câu: URL đúng, mức media, cờ file, ý định, `expect_any`
  cho câu thời gian) + `scripts/eval_retrieval.py` (dense / BM25 không dấu / hybrid / hybrid+boost,
  recall@5/10, MRR@10 mức tài liệu).
- Sửa phát hiện khi crawl: URL trùng do http/https + "/" cuối (139 trang crawl 2 lần → `canonicalize`
  chuẩn hoá + `normalize_state` gộp); khối "Các bài đã đăng"/tags lọt vào nội dung thông báo ngắn
  (parser strip `div.bottom-box`, `div.row:has(> h3.irs-sidebar-title)`, `irs-courses-img-text`);
  `database is locked` khi đọc state song song (SQLite WAL + timeout).
- Image GPU: `Dockerfile.worker` `VARIANT=gpu` (torch cu126 cùng version lock) + `docker-compose.gpu.yml`
  + `poppler-utils`; `.dockerignore`.

Còn lại theo thứ tự:
1. ~~Eval e5-small vs e5-base~~ **chốt e5-small 2026-10-13**; → xong 2026-10-13 (`eval/RESULTS.md`): hybrid+boost small R@5 1.0 / MRR 0.928 vs base 0.985 / 0.878, small embed nhanh ~3.5×, vector nửa kích thước → **đề xuất giữ e5-small** (chờ chốt).
2. ~~Crawl~~ → **xong 2026-10-13**: 2.148 trang (1.933 nội dung), 5.804 ảnh khác nhau (1,9 GB),
   18 lượt video, 375 file đính kèm (301 MB) — tổng 2,3 GB. Sửa thêm khi chạy: host chết
   (`fit-haui.edu.vn`, `tuyensinh.haui.edu.vn`) bỏ sau 5 lỗi kết nối liên tiếp; site đặt SAI ĐUÔI file
   (133 ".xls" là xlsx, 19 ".doc" là docx) → nhận định dạng theo chữ ký byte; doc/xls cũ qua `catdoc`
   (thêm vào image); 52 PDF scan (447 trang, gồm quy định đồ án, tuyển sinh thạc sĩ, QĐ 721) → OCR
   tesseract `OMP_THREAD_LIMIT=1` song song theo trang. File đọc được: 165 → ~370/375.
   Image GPU 25,3 → 13,8 GB (bỏ `chown -R /app` nhân đôi venv; sửa cả `Dockerfile.crawl`).
   **Compute xong 2026-10-13** (chốt e5-small; docs/text/images trên server GPU 2×Tesla T4 qua ngrok —
   container thiếu libcuda nên dùng thư viện user-space driver 580.178.04 giải nén riêng, không Docker;
   videos chạy local vì cần cookies YouTube). Artifact `_snapshot/2026-10-12/artifacts/v2-multilingual-e5-small/`
   (115 MB, kiểm tra toàn vẹn OK): 2.284 tài liệu (1.807 bài, 112 trang tĩnh, 365 file đính kèm), 17.264
   chunk, 5.802 ảnh (2.947 có chữ OCR), 5.901 caption ảnh, 18 video (17 có khung hình, 253 frame, 0 phụ
   đề), 2.146 tài liệu có ngày đăng, 44 mục. Thời gian GPU: docs 28' (OCR PDF scan chiếm phần lớn),
   text 3' (17k chunk), images 60' (OCR ảnh trên 4 CPU là nút cổ chai, không phải GPU).
   Eval có file đính kèm: hybrid+boost R@5 1.0, MRR 0.917 (không có đính kèm: 0.928).
3. ~~Loader~~ → **xong 2026-10-14** (`loader.py` + `indexer.py` + `sparse.py`): PG `documents` (upsert
   theo url, giữ id) + bảng mới `media` (sha256 unique), `media_occurrence`, `videos`,
   `video_occurrence` (vẫn qua `db.init_db`, Alembic chưa làm); Qdrant `sict_text_v2` (E5 dense +
   sparse BM25 bỏ dấu, IDF phía Qdrant) / `sict_image_v2` (1 point / 1 file, `document_id` là danh
   sách bài); int8 scalar quantization, vector gốc on-disk. Nạp local: 2.284 tài liệu, 23.183 point
   chữ, 6.055 point ảnh/frame, khớp 100% artifact, ~3 phút, không embed lại. config mặc định trỏ
   `*_v2`; API thêm `GET /media/s/{sha256}` (`Source.media_key`).
   Bộ vàng trên Qdrant thật (rewrite luật, không LLM): hybrid R@5 0.97 / MRR 0.88; đường `api.retrieve`
   có rerank E5 chỉ 0.94 / 0.81 → v2 bỏ xếp lại theo cosine (giữ cosine làm `raw_score` cho router):
   0.97 / 0.87.
4. Hiệu chỉnh ngưỡng router (`clip_min`, `caption_min`) — **chờ brainstorm lại** (2026-10-14).
5. ~~Incremental sang `indexing.py`~~ → **xong 2026-10-14**: `main._process_page` → `indexer.index_page`
   (cùng hàm quy tắc + id uuid5 với compute). So sánh bằng `hash_v2` = sha1 markdown sạch, không còn
   hash HTML thô (bộ đếm lượt xem làm mọi bài "đổi" mỗi lần recheck). OCR ảnh/frame chạy ngay lúc
   index; file đính kèm tải + trích text theo trang chứa; listing chỉ đẩy link. Smoke trên dữ liệu
   thật: trang đã nạp bulk → `unchanged`; ép index lại / xoá rồi thêm lại → cùng số point, cùng id.
   Sửa kèm: cầu chì xoá hàng loạt trước đây bị `except` nuốt (không dừng được pass); xoá bài ghi PG
   trước rồi mới gỡ Qdrant; bài đang index lại được đặt `hash_v2=NULL` trước khi gỡ point.
   Còn thiếu so với bulk: lọc chunk boilerplate xuyên bài — không cần (bulk đo ra 0 chunk bị lọc).
6. ~~Graph thực thể (D20)~~ → **xong 2026-10-14**: parser lấy đoạn văn sát từng ảnh
   (`image_contexts`); danh bạ 6.999 thực thể (6.577 người, 392 sự kiện, 30 CLB), 38k cạnh; 4.231/5.901
   lần ảnh xuất hiện có cạnh, 2.708 có bằng chứng mạnh. Đo trên Qdrant thật: câu `required` của bộ
   vàng có ảnh đúng bài 5/7 → 6/7; "ảnh Olympic Tin học" 0/5 → 5/5 ảnh đúng, "ảnh IT Festival" 1/5 →
   5/5, "ảnh PGS.TS Nguyễn Thị Kim Sơn" (trước bị router xếp `none`) 5/5; tìm chữ không đổi (MRR
   0,867 → 0,863). Router nhận "ảnh" CÓ DẤU là ý định xem ảnh (trừ "ảnh hưởng"). Còn trượt: câu hỏi
   diễn đạt khác tên sự kiện (q49 "cuộc thi tấm thiệp …" vs "… tri ân/sáng tạo …").
6b. ~~Cạnh thực thể ↔ thực thể~~ → **xong 2026-10-14**: `entity_relations` suy từ `entity_links`
   (cùng được nêu trên 1 ảnh 1.0, cùng tiêu đề 0.8, tiêu đề + thân bài/ảnh 0.5, chỉ cùng thân bài → bỏ;
   noisy-OR qua các bài, kèm số bài/ảnh làm chứng). 6.453 cạnh (person–event 3,7k, person–person 2,2k,
   event–event 0,5k, person/event–CLB ~150). Tính lại toàn bộ vài giây → chạy trong loader + cuối mỗi
   lượt incremental. Truy xuất: dữ kiện `[graph]` trong ngữ cảnh LLM ("thầy X — sự kiện: …; CLB: …;
   người: …"), câu nêu ≥ 2 thực thể ưu tiên bài/ảnh gắn CẢ HAI. Bộ vàng: MRR 0,871 (không giảm), media
   `required` 6/7. Sửa thêm luật tên: bỏ "Ông" khỏi họ, chặn cụm tiêu đề viết hoa ("Ứng Dụng Của…",
   "Lý Chất Lượng"), "Hồ Chí".
6c. **Graph lộ trình A → B → C** (chốt 2026-10-14; endpoint/key LLM chủ dự án cung cấp sau):
   - **A — xong**: Postgres là nguồn sự thật, 1 đường code `graphsync` cho bulk / incremental / dựng lại
     hằng đêm (incremental gọi `rebuild` mỗi 20h); payload Qdrant `entities` SINH LẠI từ PG (so khác biệt,
     gom point cùng payload — 1 lệnh/point tốn ~70 ms); `entity_aliases` (alias / chặn đã duyệt = dữ
     liệu, không vá code), `entity_review` (hàng chờ, export/import CSV); đoạn văn sát ảnh lưu ở
     `media_occurrence.context`; `mentions` theo chunk; danh mục cán bộ (77 người, `attrs.staff`) làm
     chuẩn. Sửa lỗi phát hiện khi làm: **khoá người giữ dấu** (bỏ dấu gộp nhầm Hùng/Hưng, Tuấn/Tuân,
     Huế/Huệ — 124 khoá bị gộp); gộp "tên chạy lấn" chỉ khi phần thừa là chức danh (đã gộp nhầm
     "Nguyễn Thị Kim Anh" vào "Nguyễn Thị Kim"); tên phổ biến Thành/Trung/Tâm/Quốc/Gia/Ngân/Khoa/
     Trường/Học/Thi không còn bị chặn như từ đơn (chặn theo cặp từ: "thành phố", "khoa công"…);
     trong bài viết alias "tên đệm + tên" chỉ nhận khi viết hoa / sau thầy-cô ("người chiến thắng" ≠
     Nguyễn Chiến Thắng, "tư duy sáng tạo" ≠ Nguyễn Duy Sáng), bỏ alias 1 chữ (Procon ≠ NAPROCK PROCON).
   - **B — xong phần máy, chờ người duyệt**: `eval/graph_gold_v1.jsonl` 150 bài phân tầng, tự chứa văn
     bản, nháp `draft.rules` + `draft.llm` (gán mù bởi 5 agent, không xem kết quả luật — CHỈ là gợi ý);
     hướng dẫn `eval/GRAPH_ANNOTATION.md`; `scripts/eval_graph.py`; cổng CI `test_eval_graph.py` trên 6
     bài gán tay (người P/R 1.0, sự kiện R 0.25, ảnh R 0.27 — đã biết, giai đoạn C nhắm vào). Ước lượng
     TẠM luật so với nháp LLM (150 bài, trước vòng sửa luật cuối): người P 0.88 / R 0.89, sự kiện P 0.47 /
     R 0.34, CLB R 0.09, ảnh P 0.45 / R 0.64. Số chính thức chỉ có khi `status: reviewed`.
   - **C — ĐÃ GỠ 2026-10-18 (owner): graph chỉ dùng luật; 1.504 kết quả LLM lưu ở `/DATA/craw-sict-data/2026-10-16/llm/`, bảng `llm_extractions` đã drop.** Mô tả cũ: `graph_llm.py` (JSON schema {type, name, roles, evidence,
     confidence}; kiểm chứng câu trích nguyên văn + tên nằm trong câu trích; vai trò theo danh sách; ảnh
     chỉ theo văn bản riêng của ảnh; độ tin < 0.6 → hàng chờ) + bảng `llm_extractions` (theo hash_v2 +
     model + PROMPT_VERSION) + nối vào `graphsync` (ứng viên bổ sung, vai trò → `entity_links.roles`,
     `entity_relations.roles`, dữ kiện `[graph]` có vai trò) + incremental gọi khi `graph_llm.enabled`.
     Khi có key: `graph_llm gold` → `eval_graph --pred <file>` so với luật trên bộ đã duyệt → quyết định
     dùng LLM cho toàn bộ hay chỉ gán vai trò.
     **Chạy thật 2026-10-14** (Groq `qwen/qwen3.8-27b`, json_schema OK, ~1 s/bài): 119/150 bài xong,
     31 bài dừng vì key gói miễn phí chỉ có **8.000 token/phút, 200.000 token/ngày** (đã dùng hết).
     Trên 119 bài, tham chiếu tạm = nháp gán mù (CHƯA người duyệt): LLM F1 người 0,95 (P 0,99) /
     sự kiện 0,57 / CLB 0,63 / ảnh 0,70 (P 0,77), vai trò đúng 69%; luật 0,91 / 0,41 / 0,16 / 0,56. Gộp
     luật + LLM: F1 chung 0,77 < LLM riêng 0,81 (nhiễu của luật kéo precision) → khi đã duyệt bộ vàng,
     cân nhắc "có LLM thì ưu tiên LLM, luật là đường lùi". Sửa kèm: hết hạn mức NGÀY (retry-after >
     120 s) → `QuotaExhausted` ngay, không treo index; incremental bật LLM vẫn chạy bằng luật (smoke OK).
     Bộ vàng qua HTTP (`eval_api.py`, LLM bị 429 → lùi về luật): R@5 0,97, MRR 0,873, chế độ media
     đúng 87%, ảnh đúng bài 6/7. Với hạn mức này `/chat` chỉ ~2 câu/phút và trích toàn bộ ~1,9k bài
     (~7–8 triệu token) mất ~38 ngày → cần gói trả phí / nhà cung cấp khác.
     **Chốt 2026-10-14**: key riêng cho graph (`GRAPH_LLM_*` trong `.env`, chat chưa có key → lùi về
     luật); đủ 150/150 bài. Bộ vàng: chủ dự án duyệt nguyên khối bản nháp gán mù → `status: reviewed`
     (ghi rõ nguồn trong `labeler`). Số chính thức (F1): luật người 0,91 / sự kiện 0,39 / CLB 0,16 / ảnh
     0,56 / chung 0,78; LLM 0,81 / 0,56 / 0,57 / 0,71 / 0,74 (sót tên trong bảng dài); **người = luật ∪ LLM,
     sự kiện/CLB/ảnh = LLM → chung 0,84** → đã áp vào `DocInput.link` cho bài có kết quả LLM. 69 cặp tên
     dính cán bộ: chủ dự án đồng ý toàn bộ gợi ý → 20 merged / 49 distinct (`entity_aliases`). Sửa kèm:
     tên người trong ngoặc ở tiêu đề không còn bị nhận thành sự kiện. Trích LLM toàn bộ ~1.900 bài: chờ
     chọn phương án (đo thật ~1.100 token/bài → cả site ~2,9 triệu token ≈ 15 ngày gói miễn phí; bỏ tuyển
     dụng / kế hoạch / cán bộ (0 thực thể trong mẫu) ≈ 10 ngày).
     **LLM chat qua gateway local (`LLM_*` = `localhost:20128`, combo `sict` → Groq qwen) 2026-10-14**:
     sửa lỗi `vlm._parse_completion` — gateway trả JSON + đuôi `data: [DONE]` nên trước đây MỌI lượt LLM
     (viết lại, router, chấm ngữ cảnh) âm thầm trả "" và lùi về luật. Bộ vàng qua HTTP có LLM thật:
     R@5 0,97, R@10 0,985, MRR 0,843, chế độ media đúng 84%, ảnh đúng bài 7/7 (luật: 0,97 / 0,97 / 0,873
     / 87% / 6/7) — LLM viết lại giúp recall@10 + ảnh, làm lệch thứ hạng vài câu (q27 hạng 1 → 6); thử
     tìm bằng cả câu gốc + câu viết lại không cải thiện (R@5 0,955) → bỏ. Chế độ media: LLM và luật cùng
     sai ở câu phán đoán ("hội thảo X diễn ra khi nào" có cần ảnh?) → thuộc bước hiệu chỉnh router.
     `/chat` sinh câu trả lời bằng dữ kiện graph (~7 s). Trích LLM trong incremental chạy thật (smoke OK).
7. ~~Đối soát PG ↔ Qdrant (AC-E6)~~ → **xong 2026-10-14** (`reconcile.py`, incremental gọi mỗi 20h):
   xoá point chữ mồ côi, gỡ id bài đã xoá khỏi point media, đặt lại `hash_v2` cho bài mất point;
   cầu chì > 20% mồ côi thì dừng.
8. Alembic: **hoãn có chủ đích** — schema v2 chỉ thêm bảng/cột, `db.init_db` idempotent (chạy lại
   không hại), loader chạy `init_db` trước khi ghi. Làm khi có thay đổi schema phá vỡ (đổi/xoá cột).
9. Postgres local hỏng sau lần máy tắt đột ngột 2026-10-12 (autovacuum `cannot freeze committed
   xmax`, mất chunk TOAST ở `media_assets`) → 2026-10-14 tạo lại (bản cũ đổi tên
   `.pgdata.corrupt-20261014`), nạp lại v2 khớp 100%.

**Cutover production — đã chuẩn bị, CHƯA chạy** (2026-10-14: chủ dự án chốt **không upload S3, không
chạy gì trên VM cho tới khi có lệnh**). Kịch bản `scripts/cutover_v2.sh`, mỗi bước gọi tay:
`pack` (máy dev, ~140 MB artifact + HTML) → `backup` (VM1: `pg_dump` + snapshot Qdrant v1) → `load`
(VM1: build image mới, API cũ vẫn chạy; loader `--skip-media-upload`) → deploy code (workflow CD,
API + worker cùng lúc — worker v2 từ chối ghi collection v1) → `verify` (`scripts/eval_api.py`, bộ
vàng qua `/query`) → `upload-media` (chỉ khi có lệnh: `CONFIRM_S3_UPLOAD=yes`, loader `--media-only
--upload-media`, ~1,9 GB, key `media/<sha256>.<ext>` đã có thì bỏ qua). Loader không bao giờ ghi S3
nếu thiếu `--upload-media`. Ảnh v2 hỏng (404) tới khi upload xong → muốn tránh thì upload trước deploy.
Rollback: `cutover_v2.sh rollback` (env API về `sict_text`/`sict_image`, worker image cũ).

Output: `_snapshot/2026-10-12/` (`pages.jsonl`, `images.jsonl`, `videos.jsonl`, `files.jsonl`, `html/`,
`media/`, `files/`), artifact ở `_snapshot/2026-10-12/artifacts/<spec>-<model>/`. Keyframe video cần
cookies YouTube (`cookies/`) vì IP cloud hay bị chặn.

## Giai đoạn 0 — Chuẩn bị & vận hành an toàn (rủi ro thấp)
- Backup: `pg_dump` + snapshot Qdrant (`POST /collections/{name}/snapshots`) → S3; thử restore 1 lần.
- Baseline: `scripts/quality_probe.py` (p50/p95 latency, tỉ lệ `sufficient`) + thời gian 1 chu kỳ
  incremental + các chỉ số ở bảng bối cảnh. Lưu kết quả để so sau mỗi giai đoạn.
- D11: swap 2 GB, pin version image trong compose + Dockerfile (`uv`, qdrant, nginx, obscura, minio).
- D9: VM2 chỉ chạy compose `cron`; gỡ `sict-worker-24h`; `svc-ocr` chuyển sang `profiles` (chạy tay),
  để tổng `mem_limit` VM2 ≤ 3.500 MB.
- D15: gỡ `auto_process.sh` + dòng crontab host `*/30 … auto_process.sh` (đang lỗi âm thầm: trỏ tới
  `/home/quyen/craw-sict/cron/incremental.sh` không tồn tại). Comment k8s trong code/compose đã dọn (2026-10-12).
- D10: workflow build → GHCR; CD đổi sang `docker compose pull && up -d` theo tag SHA.
- Verify: `docker compose config -q`; CD deploy thử 1 lần + rollback về tag trước; `free -m` có swap.
- Rollback: CD cũ còn trong git history.

## Giai đoạn 1 — Sửa nhanh, không đổi schema (vẫn chạy trên Qdrant)
Ingest (VM2):
- 1.1 Parser (D1): thêm `div.irs-blog-single-col` (+ selector cho từng loại trang seed, khảo sát mỗi
  loại 1 trang), strip `div[class*='side-bar']` + khối meta ngày/lượt xem. Fallback toàn trang →
  log cảnh báo + đếm `parser_fallback`. Fixture 3–5 trang HTML thật cho `test_parser` (AC-H1..H3).
- 1.2 Dedupe ngắn hạn (D2, chưa đổi bảng): video theo YouTube id đã có frame → bỏ qua yt-dlp/ffmpeg;
  ảnh theo URL chuẩn hoá rồi sha256 (`s3_key`) → bỏ qua S3 put/CLIP/OCR, dùng lại caption.
- 1.3 Hash v2 (D3) lưu `documents.meta.hash_v2`. Chuyển đổi không gây sóng xử lý lại: doc chưa có
  `hash_v2` → đọc raw HTML cũ từ S3, tính v2 cho bản cũ và bản mới, chỉ xử lý lại khi khác.
- 1.4 OCR stop-gap: OCR rỗng vẫn gắn marker để không bị pick lại; caption point id cố định
  `uuid5("cap", media_id)`; backlog gom frame theo (document_id, source_url). Dọn point caption trùng.
- 1.5 `skip_patterns` thêm `/vn/tags`; nhận diện trang lỗi (chữ ký ASP.NET/"Server Error",
  markdown quá ngắn) → không index (AC-H4, H5). Không xoá dữ liệu cũ ở bước này (để Giai đoạn 3 lọc).
API (VM1, làm song song):
- 1.6 Endpoint `query`/`chat`/`media` chạy threadpool + semaphore; `Storage` singleton; warm-up model
  trong lifespan + `/readyz` (AC-P1..P3).
- 1.7 Tách `excerpt` (UI) và context LLM (full chunk, `llm.context_chars`), gộp chunk liền kề (AC-S6).
- 1.8 Agent: bỏ attempt `top_k×2`; judge đọc đúng context LLM đọc; cache rewrite Redis 1 ngày;
  deadline tổng 20 s, timeout rewrite 5 s / judge 8 s (AC-S7). Rerank: cờ `retrieval.rerank`, mặc định `none`.
  (D27, 2026-10-25: đã bỏ hẳn LLM judge và vòng thử lại — xem D27.)
- Verify: test parser fixture; 1 chu kỳ incremental: số lần chạy ffmpeg ≤ số video mới, không asset nào
  bị OCR lại; `quality_probe` không tệ hơn baseline, p95 giảm; gọi `/media` trong lúc `/chat` đang chạy
  vẫn trả ngay.
- Rollback: revert PR; data không đổi schema.

## Giai đoạn 2 — Embedding ONNX + CLIP đa ngôn ngữ (D7)
- Export E5-small, CLIP B/32 vision, CLIP multilingual text → ONNX, quantize int8. Module embed mới
  giữ nguyên chữ ký `embed_texts`/`embed_query`/`embed_image_bytes`/`embed_text_clip`.
- Gỡ `torch`/`sentence-transformers`/`transformers` khỏi image API và worker (thay `onnxruntime`,
  `tokenizers`). Chunker semantic dùng cùng module.
- Device: giữ `embedding.device: auto` (đã có từ 2026-10-12 trên đường torch, xem `embedder.get_device`).
  Bản ONNX: provider `CUDAExecutionProvider` khi có GPU + model fp16, ngược lại `CPUExecutionProvider`
  + model int8 (int8 không chạy tốt trên CUDA). Image CPU dùng `onnxruntime`; image GPU (build-arg
  `VARIANT=gpu`) dùng `onnxruntime-gpu` + CUDA runtime, chạy kèm `docker-compose.gpu.yml`
  (`deploy.resources.reservations.devices: nvidia`). Không gộp CUDA vào image CPU (thêm vài GB đĩa vô ích).
- GPU chỉ có lợi cho embed theo lô (worker, re-index Giai đoạn 3 — có thể thuê 1 máy GPU Spot vài giờ);
  API embed 1 câu hỏi/lần nên CPU đủ. OCR Tesseract và ffmpeg vẫn chạy CPU.
- Verify: cosine(ONNX, torch) ≥ 0,99 trên 200 câu/ảnh mẫu; bộ câu hỏi tiếng Việt tìm ảnh tốt hơn
  baseline (AC-G1); RAM API (`docker stats`) và latency embed giảm; image size giảm.
- Rollback: cờ `embedding.backend: torch|onnx` giữ 1 bản phát hành, sau đó gỡ.

## Giai đoạn 3 — Schema mới + load artifact vào Qdrant `*_v2` (D2, D4, D5, D8) — cutover blue/green
- Alembic: `stamp` schema hiện tại làm baseline; mọi thay đổi schema từ đây qua migration
  (thay `ALTER … IF NOT EXISTS` trong `db.init_db`). **Chưa làm** — schema v2 hiện tạo bằng `init_db`.
- ~~Postgres + loader + retrieval keyword~~ → **xong 2026-10-14** (xem mục "Snapshot offline + compute",
  bước 3 và 5). Schema chốt: `media(sha256 unique, kind, s3_key, source_url, ocr_text, ocr_status,
  ocr_attempts, video_id, timestamp, point_id)`, `media_occurrence(document_id, media_id, alt, pos,
  caption_point_id)`, `videos(video_key unique, url, title, subs, frames, status)`,
  `video_occurrence(document_id, video_id, caption_point_id)`; `documents` thêm `doc_key`, `section`,
  `doc_type`, `published_at`, `year`, `parent_url`, `hash_v2` (`next_check_at` để Giai đoạn 4).
  Keyword = sparse BM25 bỏ dấu (không dùng `text_folded`).
- Cutover: đổi tên collection trong config/env sang `*_v2`; giữ collection cũ 2 tuần rồi xoá.
  Backup: snapshot Qdrant + `pg_dump` lên S3 trước cutover.
- Verify: số point khớp số dòng artifact; không còn ảnh/frame trùng; `quality_probe` + bộ câu hỏi vàng
  ≥ baseline; hỏi không dấu ra bài có dấu; xoá 1 bài không xoá ảnh còn dùng ở bài khác (AC-B5).
- Rollback: đổi tên collection về bản cũ (không đụng tới collection cũ trước khi xoá).

## Giai đoạn 4 — Crawl bền vững
- Session riêng mỗi trang + rollback trong `run()`/`drain_queue` (AC-E10).
- Frontier: pop atomic; lỗi tạm thời (0/5xx/429/timeout) → ZSET `frontier:retry` backoff, tối đa 5 lần
  → `frontier:dead`; doc bị xoá → `SREM` khỏi `frontier:seen` (AC-E7).
- Recheck thích ứng theo `next_check_at`: không đổi ×2 (trần 30 ngày), đổi → reset; thử conditional GET (AC-E8).
- Song song có giới hạn: 1 `httpx.AsyncClient` dùng chung, tải ảnh song song theo host, OCR/ffmpeg qua
  `asyncio.to_thread`; giữ 1 req/s cho HTML.
- Đối soát hằng đêm DB↔S3 (mở rộng `scripts/verify_s3.py`) (AC-E6).
- Verify: giết kết nối mạng giữa chu kỳ → URL vào retry, không mất; thời gian chu kỳ giảm so với baseline.

## Giai đoạn 5 — Vận hành & bảo mật
- D12 bảo mật; D14 region S3; D9 VM2 Spot hoặc lịch bật/tắt (EventBridge) — worker idempotent nên chịu
  được ngắt; D13 giám sát + cảnh báo heartbeat > 7 h; backup hằng ngày giữ 7–14 ngày + thử restore định kỳ.

## Ngân sách RAM mục tiêu (ước tính, đo lại sau mỗi giai đoạn)

| VM1 | Hiện tại | Mục tiêu | | VM2 | Hiện tại | Mục tiêu |
|---|---|---|---|---|---|---|
| api | 1.500 | ~600 (ONNX) | | cron (worker) | 3.000 | ~2.000 |
| qdrant | 900 | ~600 (quantization int8) | | svc-ocr | 3.000 | 0 (profile, chạy tay) |
| postgres | 800 | 800 | | obscura | 150 | 150 |
| redis + nginx | 450 | 450 | | | | |
| **Tổng** | **3.650** | **~2.450** | | **Tổng** | **6.150** | **~2.150** |

---

## Lịch sử (đã xong)

### Đợt 1 (2026-09-15) — microservice k8s + Alpine/slim
> k8s, `cron/incremental.sh`, `Dockerfile.svc-ocr`, `requirements*.txt` đã bị gỡ sau đó
> (chuyển sang Docker Compose 2 VM). Giữ lại để tra cứu.

- P0 `config.py` đọc env (12-factor): `POSTGRES_DSN`, `REDIS_URL`, `QDRANT_HOST`, `QDRANT_PORT`,
  `QDRANT_GRPC_PORT` override yaml; không set env → giữ hành vi cũ.
- P1 dọn legacy: xoá `Dockerfile.svc-ocr`, `requirements.txt` + `requirements/` (deps chỉ quản lý bằng
  uv: `pyproject.toml` + `uv.lock`, torch CPU); `auto_process.sh` gọi `--incremental`, giữ flock.
- P2 compose: service `cron` gắn `profiles: ["dev-cron"]`; bỏ `version: "3.9"`.
- P3 readiness `pg_isready`/qdrant `/healthz`.

### Đợt 2 (2026-09-15 tối) — reliability + dọn face + S3
- Reliability: `vector_store.ensure_payload_indexes()` (index `document_id`, delete 30–120 s → 1,33 s);
  `_with_retry()` cho write/delete Qdrant; `QDRANT_TIMEOUT`; `recheck_edits` cách ly lỗi từng doc;
  `run_incremental` cách ly từng stage + heartbeat Redis `incremental:last_ok`/`:last_error`.
- Disk: prune build cache, volume `hfcache` dùng chung model.
- Gỡ face legacy (`face.py`, `--annotate`, `--build-faces`, `FaceConfig`, deps ultralytics/insightface).
- S3: `S3Config.profile` (AWS_PROFILE); `scripts/verify_s3.py` quét DB↔S3 (71.940 media → 134 orphan
  healable, 0 dead, 16 raw missing); xoá 1,3 GB media local sau khi verify S3.
