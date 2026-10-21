# Kết quả đánh giá retrieval

Bộ vàng: `golden_v1.jsonl` (70 câu, 67 câu có URL đáp án + 3 câu thời gian `expect_any`).
Snapshot 2026-10-12 (1.919 tài liệu nội dung, ~4.380 chunk, index spec v2, chưa có file đính kèm).
Chạy offline bằng `scripts/eval_retrieval.py` (không LLM rewrite — số thật trên production có rewrite
có thể khác). Chi tiết từng câu: `results/<model>.json`.

## 2026-10-13 — e5-small vs e5-base (CPU)

| Model | Chế độ | recall@5 | recall@10 | MRR@10 | expect_any |
|---|---|---|---|---|---|
| multilingual-e5-small (384d) | dense | 0.910 | 0.955 | 0.818 | 2/3 |
| | bm25 (không dấu) | 0.866 | 0.925 | 0.765 | 2/3 |
| | hybrid | 0.940 | 1.000 | 0.867 | 2/3 |
| | **hybrid+boost** | **1.000** | **1.000** | **0.928** | **3/3** |
| multilingual-e5-base (768d) | dense | 0.925 | 0.940 | 0.832 | 2/3 |
| | bm25 (không dấu) | 0.866 | 0.910 | 0.756 | 3/3 |
| | hybrid | 0.955 | 0.985 | 0.849 | 2/3 |
| | hybrid+boost | 0.985 | 1.000 | 0.878 | 3/3 |

Thời gian embed trên CPU 8 nhân (cùng 4.38k chunk): small 745 s, base 2.621 s (~3.5×).

Kết luận: **giữ e5-small**. base chỉ nhỉnh ở dense đơn lẻ (+1.5 điểm R@5); trong cấu hình thật
(hybrid + ưu tiên metadata) small bằng/hơn base, vector nhỏ bằng nửa (RAM Qdrant), embed câu hỏi trên
VM 2 vCPU nhanh hơn ~3×. Mỗi lớp đều có đóng góp: dense → hybrid (+3 điểm R@5) → boost metadata (+6).

Lưu ý:
- Bộ vàng do Claude soạn từ nội dung đã crawl, cần người duyệt. 3 câu đã bổ sung đáp án sau khi kiểm
  chứng trên chunk (q12, q27, q55) — xem `notes`. Đạt trần 1.0 → cần thêm câu khó (đa bước, so sánh,
  hỏi trong file đính kèm) ở golden_v2.
- Chưa đo media/router vì chưa có artifact ảnh (chờ chạy GPU).
