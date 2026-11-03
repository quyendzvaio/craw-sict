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

## 2026-11-02 — phạm vi mục, năm theo nhãn bảng, cộng điểm tiêu đề (nhánh feat/section-year-filter)

Đo truy xuất qua `/query` (không LLM) trên index v3 máy dev, so với develop (2f6fe52), cache tắt:

| | golden_v1 recall@5 | MRR@10 | golden_v2 url_hit | list_v1 coverage |
|---|---|---|---|---|
| develop | 0,985 | 0,898 | 69/78 | 0,864 |
| nhánh này | 1,0 | 0,928 | 70/78 | 0,864 |

- q63 (golden_v1) / g053: hai mảnh bảng NĂM 2021 của trang "Bài báo khoa học" lên hạng 1–2 (trước: không có trong top 60).
- g054: ba mảnh bảng NĂM 2021 của trang "Báo cáo khoa học" ở hạng 1–3, thay cho mảnh chỉ có tiêu đề.
- list_v1 0,864 ở cả hai bản; lần đo 2026-10-26 là 0,970. Câu l01 "Trường có những ngành đào tạo nào?" chỉ còn 2/9:
  các file thời khoá biểu (.xls) chen vào top 10. Mức tụt này đã có ở develop, không do nhánh này; các trang chương
  trình đào tạo đã được worker chạy thử index lại ngày 29–31/10. Chưa tìm ra nguyên nhân.

## 2026-11-02 — list_v2 (42 câu liệt kê) và nguyên nhân list_v1 0,970 → 0,864

**Nguyên nhân list_v1 tụt.** Không do code hay vector:
- code 27/10 chạy trên dữ liệu hiện tại cũng ra 0,864;
- vector lưu trùng vector nhúng lại (cosine 0,999);
- chunk của các bài liên quan giống bản index gốc.

Hai câu lật thứ hạng ở ranh giới top 10 sau các thay đổi nhỏ của index (nhãn bảng 01/11: −129 chunk / 101 trang; 13 bài mới; BM25 IDF tính trên cả collection):
- **l01:** bài 391 (chứa 8/9 ngành) hoà điểm RRF tuyệt đối (0,01449) với một file thời khoá biểu ở hạng 10/11.
- **l07:** câu liệt kê lấy 1 đoạn / bài, và đoạn có tên các phó giáo sư (idx 0) thua đoạn không có tên (idx 1, có mặt ở cả dense lẫn BM25).

**Đã thử, không đạt:** RRF theo bài (hạng = số bài khác đứng trước). golden_v1 MRR 0,928 → 0,898, list_v1 không đổi. Nguyên nhân: thứ chen vào là nhiều file thời khoá biểu khác nhau, không phải nhiều đoạn của một file. Nhánh local `exp/doc-aware-rrf`, không merge.

**list_v2.** Mốc trên develop:

| | n câu liệt kê | coverage | full_coverage | sufficient_acc |
|---|---|---|---|---|
| develop (bbb4500 + docs) | 42 | 0,724 | 23 | 0,96 |

- Hai lần chạy giống hệt nhau từng câu.
- Câu mới độ phủ 0 (hỏng truy xuất thật, chờ phân tích):
  - m03 cố vấn học tập (không dấu);
  - m11 đề tài NCKH 2022 (không dấu);
  - m21 tổ hợp mới 2025;
  - m22 điều kiện duy trì học bổng;
  - m24 khóa ngắn hạn 2026;
  - m27 đội chung kết SPC 2026.

## 2026-11-03 — ngữ cảnh đủ đoạn, đoạn liền kề cho câu liệt kê, khôi phục dấu (nhánh feat/context-fit)

**Phân tích 6 câu độ phủ 0 của list_v2.** Có hai nguyên nhân chung.

1. **Đã tìm đúng bài nhưng mục cần liệt kê không tới LLM** (m21, m22, m24, m27; l07 trước đây).
   - 74% đoạn của index v3 dài hơn 1500 ký tự (trung vị ~1940), mà mỗi nguồn chỉ được 1500 ký tự: m22 có mục ở ký tự 1854–2062, m27 ở 1663/2073.
   - Câu liệt kê lấy 1 đoạn / bài, và đoạn khớp câu hỏi nhất thường là phần mở đầu: m21 chọn idx 0 trong khi mục nằm ở idx 1/2; m24 chọn idx 3 trong khi mục ở 0/2/4.
2. **Câu không dấu** (m03, m11): jina-clip-v2 không hiểu tiếng Việt không dấu. Nhánh dense không có bài đúng trong top 100; cùng câu có dấu thì bài đúng ở hạng 1–2.

**Sửa (tổng quát, không theo câu):**
- `llm.source_chars` 1500 → 2200, `context_chars` 9000 → 11000, `list_context_chars` 15000 → 31000;
- `retrieval.list_neighbor_docs` 2: câu liệt kê thêm đoạn liền trước / sau của 2 bài đầu, đặt sau các nguồn chính;
- `retrieval.restore_accents`: câu hoàn toàn không dấu được khôi phục dấu bằng bigram âm tiết của corpus (không LLM); chỉ nhánh dense dùng câu đã khôi phục.

**Số đo** (API trên index v3, không cache Redis; golden_v1 ghi recall@5 / MRR@10):

| | list_v2 coverage | full_coverage | golden_v1 | golden_v1 bỏ dấu | golden_v2 url_hit | ngữ cảnh câu liệt kê |
|---|---|---|---|---|---|---|
| develop | 0,724 | 23/42 | 1,0 / 0,928 | 0,881 / 0,775 | 70/78 | ~13.100 ký tự |
| chỉ đổi ngân sách | 0,798 | 30/42 | không đổi | không đổi | 70/78 | ~15.800 |
| + đoạn liền kề | 0,878 | 34/42 | không đổi | không đổi | 70/78 | ~18.400 |
| chỉ khôi phục dấu | 0,724 | 23/42 | 1,0 / 0,928 | 0,985 / 0,920 | 70/78 | không đổi |
| **cả ba (nhánh này)** | **0,878** | **34/42** | **1,0 / 0,928** | **0,985 / 0,920** | **70/78** | ~18.400 |

- Không câu nào tụt ở bất kỳ bộ nào; `sufficient_acc` giữ 0,96.
- Còn 0: m03, m11 (bài đúng đã vào top nhờ khôi phục dấu, nhưng mục nằm sâu trong bài dài, ngoài ±1 đoạn), m22 (câu thường nhiều nguồn, ngân sách chia đều vẫn cắt).
- Chưa chấm RAGAS cho thay đổi này (câu trả lời đổi vì ngữ cảnh dài hơn 20–40%); chủ dự án duyệt áp dụng ngay 2026-11-03, RAGAS chạy sau khi có quota LLM.

## 2026-11-03 — graph khái niệm (cụm danh từ, kiểu LazyGraphRAG) thay graph luật (nhánh feat/graph-concepts, D33)

**Vì sao.** Graph luật (người / sự kiện / CLB) cần một luật cho mỗi dạng tên, nên luôn sót trường hợp ngoài dự tính.
Đã thử 3 hướng dựng graph, không LLM, giữ nguyên code truy xuất (chỉ thay danh bạ + liên kết đoạn):
- **luật cũ**;
- **GLiNER** đa ngôn ngữ (`urchade/gliner_multi-v2.1`, nhãn người / sự kiện / CLB-đội, ngưỡng 0,5 / 0,6 / 0,7);
- **cụm danh từ** (underthesea `chunk`): cụm ≥ 2 từ gặp ở 2 bài đến 5,57% số bài (115 / 2.066).

**Lần đo thử nghiệm** (graph nạp từ file, lọc theo id đoạn; golden_v1 ghi recall@5 / MRR@10):

| | golden_v1 | golden_v2 url_hit / MRR | từ chối đúng | list_v2 coverage (đủ mục) |
|---|---|---|---|---|
| luật | 1,0 / 0,928 | 70/80 / 0,716 | 4/8 | 0,878 (34/42) |
| **cụm danh từ** | **1,0 / 0,954** | **71** / 0,719 | 3/8 | **0,916 (37/42)** |
| GLiNER 0,5 | 0,985 / 0,940 | 71 / 0,706 | 4/8 | 0,892 (33/42) |
| GLiNER 0,6 | 1,0 / 0,946 | 71 / 0,712 | 4/8 | 0,834 (32/42) |
| GLiNER 0,7 | 0,985 / 0,935 | 72 / 0,716 | 4/8 | 0,834 (32/42) |

- GLiNER ngưỡng cao lọc quá chặt: list m20 3/4 → 0, m31 6/6 → 0; g058 (ảnh lễ tốt nghiệp) hạng 1 → 4. Dựng ~2,7 giờ CPU
  (6 luồng), ~3 GB RAM. F1 trên `graph_gold_v1` (150 bài) thấp hơn luật ở mọi ngưỡng (tất cả: 0,64–0,66 vs 0,73 trên 75 bài).
- Cụm danh từ: m03 0/4 → 4/4, m20 → 4/4, m24 → 3/3, g085 ngoài top → hạng 1; mất g080 (câu không có đáp án, khớp
  khái niệm "học phí" nên `sufficient` thành đúng). Dựng ~10 phút / 1 lõi, < 1 GB.

**Bản code thật** (`graph.mode: concepts`, `graphsync rebuild` trên PG + Qdrant máy dev, lọc theo payload `entities`):
danh bạ 4.958 khái niệm và 330.992 liên kết, trùng 100% với file thử nghiệm (payload `entities` của 22.583 điểm khớp
từng điểm). Ghi lại payload 16 nghìn điểm làm Qdrant tối ưu lại segment, và tìm kiếm HNSW xấp xỉ đổi hạng ở vài câu
golden_v2: chạy lại code thử nghiệm trên index hiện tại cũng ra 70 / 0,701 thay vì 71 / 0,719. So trên **cùng trạng thái index**:

| | golden_v1 | golden_v2 url_hit / MRR | từ chối đúng | list_v2 coverage (đủ mục) | sufficient_acc |
|---|---|---|---|---|---|
| luật | 1,0 / 0,933 | 70/80 / 0,690 | 4/8 | 0,871 (34/42) | 0,98 |
| **khái niệm** | **1,0 / 0,954** | **70/80 / 0,701** | 3/8 | **0,916 (37/42)** | 0,96 |

- golden_v2 tốt lên: g020, g060 (hạng 2 → 1), g051, g070 (lên 1 bậc), g085 (ngoài top → 1); tụt: g018 (2 → 5), g023, g053, g055
  (1 bậc), g029 "chủ nhiệm CLB HIT" (hạng 4 → ngoài top), g080 (mất từ chối đúng).
- Mất theo thiết kế: dòng `[graph]` trong ngữ cảnh (không có cạnh thực thể ↔ thực thể) và nhánh ảnh theo tên người;
  `media_mode_acc` giữ 0,871, ảnh bắt buộc 7/7.
- Chưa chấm RAGAS (ngữ cảnh mất dòng `[graph]` nên câu trả lời có thể đổi); chờ quota LLM.

## 2026-11-03 — RAGAS golden_v2 sau PR #30 + #31 (trả lời gpt-oss-120b-free, giám khảo qwen3.5-flash-free)

Hai bộ câu trả lời cùng một giám khảo: `results/ragas/2026-11-03-g2-prev` (câu trả lời 11-02, trước PR #30 / #31)
và `results/ragas/2026-11-03-g2` (develop sau PR #30 ngữ cảnh đủ đoạn + đoạn liền kề + khôi phục dấu, PR #31 graph
khái niệm). Ghép cặp theo câu (chỉ câu có điểm ở cả hai bộ; 2 + 5 chỉ số lỗi 504 của kiraai bị bỏ). Không so thẳng
với lần 11-02 (giám khảo deepseek).

| | cũ | mới |
|---|---|---|
| faithfulness (n=76) | 0,959 | 0,946 |
| answer_relevancy (n=79) | 0,742 | 0,739 |
| context_precision (n=78) | 0,754 | 0,758 |
| context_recall (n=78) | 0,833 | **0,887** |
| url_hit | 0,885 | 0,897 |
| false_abstain | 0,100 | 0,075 |
| abstain_ok | 1,0 | 1,0 |

- Câu liệt kê (6): context_recall 0,49 → 0,85, faithfulness 0,86 → 0,98, context_precision 0,78 → 0,53 (thêm đoạn liền kề).
- Faithfulness −0,99 điểm tổng trên 76 câu, chấm lại 2 lần để tách nhiễu: g031, g032 lên 1,0 khi chấm lại (nhiễu giám
  khảo); lỗi thật: **g018** (truy xuất mất đoạn có "750", xem mục dưới), g021 (gpt-oss chép sai số điện thoại
  `0383371290` → "038 3372 90"), g061 (tả ảnh "giảng viên hướng dẫn, logo" không có nguồn).
- false_abstain: 5 câu hết né nhờ PR #30 (g052, g053, g054, g058, g061); mới né g018, g029 (bài đúng rơi khỏi top 5
  từ PR #31), g050 / g056 là bắt nhầm (câu trả lời đúng, có câu ghi chú "không có thông tin chi tiết").

## 2026-11-04 — graph khái niệm: bỏ khoá hư từ / từ hỏi (nhánh fix/graph-func-words)

Nguyên nhân g018 "Lễ tốt nghiệp năm 2025 của SICT có bao nhiêu sinh viên nhận bằng?": câu hỏi khớp 2 khái niệm
"tốt nghiệp" và **"bao nhiêu"** (underthesea gắn nhầm cụm danh từ, 4 bài) → danh sách AND (cả hai) chỉ còn 1 đoạn
(bài "Hơn 1.000 tân Thạc sĩ…") được đẩy lên hạng 2, đoạn 0 / 1 của bài lễ tốt nghiệp (có "750") rơi khỏi top 5.
Bỏ "bao nhiêu" thì đoạn đó vào lại top 5. golden_v1 6/70, golden_v2 21/88 câu khớp ít nhất một khái niệm hư từ.

Đo 4 biến thể trên CÙNG trạng thái index (chỉ lọc danh bạ khớp câu hỏi, payload không đổi; biến thể "giữ nguyên"
tái lập đúng số lần đo D33):

| Biến thể | golden_v1 R@5 / MRR | golden_v2 url_hit / MRR | list_v2 coverage / sufficient_acc | abstain |
|---|---|---|---|---|
| giữ nguyên | 1,0 / 0,954 | 70/80 / 0,701 | 0,916 (37/42) / 0,96 | 3/8 |
| **① bỏ hư từ** (20 khái niệm) | 1,0 / 0,954 | 70/80 / **0,708** | 0,916 (37/42) / 0,94 | 3/8 |
| ② trần df trên số bài liên kết (258) | 1,0 / 0,958 | 70/80 / 0,688 | 0,916 (37/42) / 0,98 | 3/8 |
| ① + ② | 1,0 / 0,958 | 71/80 / 0,696 | 0,916 (37/42) / 0,98 | 3/8 |

- ①: g007 hạng 2 → 1, g018 5 → 4 (đoạn có "750" vào ngữ cảnh), không câu nào xuống; list m03 vẫn đủ 4/4 mục nhưng
  `sufficient` lật False (sát ngưỡng).
- ② loại: trần 115 bài liên kết bỏ cả khái niệm rộng mà có ích ("tốt nghiệp" 741 bài, "Đa phương tiện"): g016, g020,
  g058, g085 xuống, g018 mất hẳn (chỉ còn "bao nhiêu"); lên g048, g023, g053. ① + ②: g018 không còn khái niệm nào
  → lại mất đoạn có "750".
- RAGAS 11 câu golden_v2 đổi ngữ cảnh khi áp ① (trả lời gpt-oss-120b-sale, giám khảo qwen3.5-flash-sale — kiraai gỡ
  mọi model `-free` ngày 2026-11-04; mỗi bản sinh câu trả lời 1 lần), 9 câu có đáp án:

| | giữ nguyên | ① |
|---|---|---|
| faithfulness | 0,806 | **0,988** |
| answer_relevancy | 0,741 | **0,871** |
| context_precision | **0,798** | 0,734 |
| context_recall | 0,889 | **0,944** |

  Phần lớn mức tăng là g018 ("Không có thông tin…" → "hơn 750 sinh viên nhận bằng cử nhân [5]", F 0 → 1,0);
  context_precision giảm ở g018, g086, g009, g004 (danh sách graph không còn kéo về đúng 1 đoạn); 2 câu ngoài phạm vi
  (g075, g078) vẫn từ chối đúng.
- Áp dụng: khoá bị bỏ khi số bài đọc theo hư từ > số bài đọc theo khái niệm (quyết định theo khoá vì câu hỏi khớp chữ
  bỏ dấu: "hien tai" 112 bài "hiện tại" / 2 "HIỀN TÀI"). Dựng lại trên dev: đúng 20 khái niệm như biến thể ①.
  Sau khi ghi lại payload, Qdrant tối ưu lại segment nên vài hạng trôi dù khái niệm khớp không đổi (q50 1 → 2;
  g029 vào lại top 5, g055 / g057 / g058 lên, g031 xuống; list m11 0/8 → 8/8). Số trên index mới (2 lần đo trùng nhau):
  golden_v1 1,0 / 0,947, golden_v2 71/80 / 0,731, list_v2 0,940 (38/42) / sufficient_acc 0,94, abstain 3/8.
