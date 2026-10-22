# jina-clip-v2 tự host ONNX int8 — đo 2026-10-20

Dịch vụ `sict_crawler/service/embedding/embed_server.py` (`Dockerfile.embed`), chạy trên đúng cấu hình production dự kiến:
container `--cpus 2 --memory 8g`, `EMBED_THREADS=2`. Index đích: Qdrant v3 local (`sict_text_v3` 16.794 chunk,
`sict_ocr_v3` 5.904 caption, `sict_image_v3` 5.962 ảnh/khung) — nhúng bằng Jina cloud (chữ) + fp32 tự host (ảnh).

## 1. Chọn cách lượng tử hoá (cosine với vector của index v3)

| biến thể | chunk chữ | câu hỏi | ảnh |
|---|---|---|---|
| `onnx/model_int8.onnx` chính thức của repo | 0,54–0,61 ❌ | 0,69 | 0,98 |
| tự `quantize_dynamic` (MatMul int8 per-channel, activation int8) | 0,36 ❌ | — | — |
| **int8 chỉ trọng số** (MatMulNBits 8-bit, block 32, activation fp32) — đang dùng | **0,9990** (min 0,996) | **0,9995** | **1,0000** (min 0,9989) |
| fp32 (đối chứng) | 0,9999 | — | 1,0000 |

Tháp chữ (nhánh jina-embeddings-v3) có activation ngoại lai lớn → mọi cách lượng tử activation đều phá vector;
chỉ lượng tử trọng số thì giữ nguyên. ORT không tỉa tháp không dùng của graph gộp → tách 2 tháp (`build_towers`).

## 2. Parity đầy đủ (`scripts/embed_parity.py`, 60 chunk + 30 ảnh + 70 câu golden)

- chunk ≤ 1.600 ký tự: cosine 0,9993–0,9994; chunk dài hơn: trung bình 0,983 — 1 chunk 0,66 vì bị cắt ở
  `EMBED_TEXT_MAX_LEN=2048` token (77/16.794 chunk dài hơn mức này; cloud nhận 8192) → script báo FAIL theo ngưỡng
  min ≥ 0,95. Không ảnh hưởng index hiện có (vector cũ giữ nguyên), chỉ chunk rất dài nhúng mới.
- ảnh 0,9996 (min 0,9974); câu hỏi 0,9995; trùng top-10 chunk so với vector câu hỏi gốc: 98%.

## 3. Truy xuất golden 70 câu (text→text, cùng chuỗi câu hỏi, không LLM rewrite)

| vector câu hỏi | hybrid hit@5 | hybrid MRR@10 | dense-only hit@5 |
|---|---|---|---|
| cloud / fp32 (tham chiếu) | 0,985 | 0,896 | 0,94 |
| **int8 chỉ trọng số** | **0,985** | **0,896** | **0,94** |
| (int8 chính thức — để so) | 0,896 | 0,735 | 0,627 |

## 4. Sáu ca sử dụng qua `api.retrieve` thật (ảnh truy vấn = ảnh corpus đã cắt 85%, thu 60%, JPEG q65)

4 ảnh (diễn giả hội thảo AI, tham quan doanh nghiệp, khai mạc Olympic lập trình, báo tường IT Festival) × U2–U6 +
6 câu golden U1. Kết quả **25/26**:

| UC | đạt | ghi chú |
|---|---|---|
| U3 ảnh→ảnh | 4/4 | ảnh gốc @1 (3 ca), @2 (IT Festival: ảnh 300×450, có ảnh khác cùng 0,91) |
| U4 ảnh→text | 4/4 | bài chứa ảnh @1 (3), @2 |
| U2 text→ảnh | 4/4 | ảnh của đúng bài @1 cả 4 (qua graph / caption) |
| U5 text+ảnh→ảnh | 4/4 | ảnh gốc @1 (3), @2 |
| U6 text+ảnh→text | 3/4 | IT Festival: bài của ảnh na ná (0,91) đứng đầu |
| U1 text→text | 6/6 | @1–@2 (có LLM rewrite) |

Lỗi tìm ra và đã sửa trong lượt này (có test hồi quy, không do int8 — lượt trước với vector khác cũng hỏng):
- `router.gate`: media "thuộc top bài chữ" được xếp trước điểm; truy vấn chỉ có ảnh thì top bài chữ do caption i2t
  yếu (~0,38) quyết định → ảnh trùng (i2i 0,99) rơi khỏi top 5 (U3/U4 trượt). Nay i2i luôn đứng đầu.
- `api.retrieve`: có chữ kèm ảnh thì đoạn của bài chứa ảnh xếp SAU kết quả chữ rồi bị cắt top_k (U6 0/4). Nay đoạn
  của bài có ảnh khớp nhất lên đầu, bài của ảnh giống kém hơn xếp sau.

Ghi nhận: ảnh sự kiện khác nhau giống nhau tới 0,88–0,97 với jina-clip-v2 → `router.i2i_min=0,6` (tạm) chỉ có nghĩa
"cùng kiểu ảnh", không phải "cùng ảnh" — cần hiệu chỉnh khi có thêm dữ liệu.

## 5. Tốc độ / RAM (2 vCPU, 8 GB) — tách tải "nhúng câu hỏi" và "nhúng nội dung"

Bản build hiện tại `w8a4e8`: tháp chữ MatMulNBits 8-bit `accuracy_level` 4 + bảng từ vựng int8 theo hàng (601 MB),
tháp ảnh 8-bit `accuracy_level` 0 (351 MB). Tháp chữ ~560M tham số: 1 câu hỏi ~28 token ≈ 31 GFLOP → trên 2 vCPU
bản fp32 cũng mất ~0,25 s; int8 ở đây giảm RAM chứ không nhanh hơn.

Tải câu hỏi (cái API cần mỗi lượt hỏi):

| | trước | bây giờ |
|---|---|---|
| RAM dịch vụ lúc rỗi | 1,97 GB | **1,33 GB** (đỉnh khi chỉ nhúng câu hỏi: 1,41 GB) |
| 1 câu hỏi qua HTTP | 724 ms (level 0) | **~300 ms** (trung vị, p95 364 ms) |
| `api.retrieve` 1 câu (không LLM) | **16,7 s** | **0,41 s** |
| 1 ảnh người dùng gửi | ~9 s | ~9 s |

16,7 s trước đây KHÔNG phải do LLM (LLM rewrite không chạy trong các lượt đo — container không có key) mà do
`rerank_text` nhúng lại chữ của mọi caption trong kết quả mỗi câu hỏi để lấy cosine, trong khi Qdrant đã có sẵn cosine
đó → nay dùng `dense_score` / vector đã lưu, không gọi embedding. Lượt hỏi thật qua API còn cộng thêm LLM rewrite +
trả lời (9router), không thuộc dịch vụ embedding.

Tải nội dung (incremental: bài / ảnh mới — chạy nền, không phải lúc người dùng hỏi):

| | thời gian | RAM đỉnh dịch vụ |
|---|---|---|
| chunk trung vị 864 token | ~6–8 s | ~2,6 GB |
| chunk 2.048 token | ~27 s | ~3,5 GB |
| chunk 4.096 token | 88 s | 6,9 GB → nên mới cắt 2.048 token |
| 1 ảnh | ~9 s | — |

Đỉnh trong toàn bộ đánh giá (có chunk 2.048 token): 3,2 GB. Build 1 lần (`embed_server build`): ~2,2 phút,
không OOM ở 8 GB. Nếu máy production chỉ phục vụ câu hỏi (incremental chạy nơi khác), `mem_limit` 2 GB là đủ.
