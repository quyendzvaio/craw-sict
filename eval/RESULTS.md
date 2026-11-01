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

## 2026-11-02 — RAGAS trên golden_v2 (nhánh feat/answer-quality-eval-v2)

Bộ `golden_v2.jsonl` (88 câu theo vai người dùng, `status: draft`). API v3 trên máy dev, cache tắt.
Model trả lời `gpt-oss-120b-free`, model chấm `deepseek-v4-flash-free` (kiraai, khác model trả lời).
Embedding cho answer_relevancy: dịch vụ embed :8090. Ví kiraai hết tiền nên không chấm bằng glm-5.3
như lần 2026-10-26: số liệu không so thẳng được với lần đó.

Toàn bộ (80 câu có đáp án; 8 câu ngoài phạm vi / không có đáp án không chấm RAGAS):

| | faithfulness | answer_relevancy | context_precision | context_recall | url_hit | false_abstain | abstain_ok |
|---|---|---|---|---|---|---|---|
| bản sửa | 0,928 | 0,668 | 0,691 | 0,805 | 0,885 | 0,100 | 1,0 |

A/B với develop (311eb8d), cùng câu trả lời model trên, chỉ tính câu cả hai bản có điểm. Bản develop chỉ chấm
được 69/88 câu: kiraai hết hạn mức ngày, chủ dự án quyết định chỉ chấm tiếp bản sửa.

| | faithfulness (57 câu) | context_precision (49) | context_recall (61) | url_hit (88) |
|---|---|---|---|---|
| develop | 0,890 | 0,637 | 0,849 | 0,885 |
| bản sửa | 0,914 | 0,704 | 0,833 | 0,885 |

Nhóm yếu (bản sửa): danh sách (CP 0,40, CR 0,49; g053/g054 bài báo, báo cáo 2021 vẫn không vào top 10),
ảnh (F 0,84, CP 0,54), video (CP 0,47: nguồn khung hình ít chữ), câu nguồn mâu thuẫn hiệu trưởng (CP 0,25).
Theo cách gõ, url_hit: phrase 1,0, question 0,89, no_accent 0,75, abbrev / chatty 0,67, english 0,5.
answer_relevancy = 0 ở 4 câu trả lời "không có thông tin" (g041, g053, g072, g074); bỏ chúng thì 0,703.
Cặp biến thể (6 cặp): gốc / biến thể AR 0,82 / 0,53, CP 0,90 / 0,69, F 0,96 / 0,96.
Model chấm miễn phí dao động: g005 có câu trả lời gần như giống hệt ở hai bản nhưng faithfulness 0 và 1.
Chi tiết: `results/ragas/2026-11-01-g2-{base,new}.scores.jsonl`, `2026-11-01-g2-new.summary.json`.
