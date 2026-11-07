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

## 2026-11-05 — media_v1: bộ đo use case ảnh / video / file (nhánh feat/eval-media-v1)

Chủ dự án: "trong tập test chưa có mấy câu yêu cầu response ra ảnh". `eval/media_v1.jsonl` (101 câu, tiêu chí
`eval/media_v1.md`, chấm bằng `scripts/eval_media.py` trên nguồn của `/query`, không phụ thuộc LLM). Lần chạy đầu
trên API dev :8003 (nhánh fix/media-answer, index v3, cache Redis), `eval/results/media_v1_2026-11-05.json`:

| số đo | tổng | image (36) | video (15) | file (15) | optional (16) | none (15) | ngoài phạm vi (4) |
|---|---|---|---|---|---|---|---|
| media_mode_acc | 0,970 (98/101) | 0,944 | 1,0 | 1,0 | 0,938 | 1,0 | 1,0 |
| wants_file_acc | 0,950 (96/101) | 0,972 | 1,0 | 0,733 | 1,0 | 1,0 | 1,0 |
| url_hit | 0,979 (95/97) | 1,0 | 0,867 | 1,0 | 1,0 | 1,0 | – |
| text_mrr@10 | 0,850 | 0,863 | 0,598 | 1,0 | 0,854 | 0,913 | – |
| required_has_media | 0,980 (50/51) | 0,972 | 1,0 | – | – | – | – |
| media_on_target | 0,892 (58/65) | 0,917 | 0,733 | 1,0 | 1,0 | – | – |
| media_precision (micro) | 0,677 | 0,782 | 0,333 | 1,0 | 0,974 | – | – |
| none_clean | 0,939 (31/33) | – | – | 1,0 | – | 1,0 | 0,5 |
| file_hit | 1,0 (15/15) | – | – | 1,0 | – | – | – |
| video_hit | 0,667 (10/15) | – | 0,667 | – | – | – | – |
| abstain_ok / false_abstain | 4/4 / 4/97 | | | | | | |

Theo kiểu gõ (media_mode_acc / url_hit / text_mrr): question 0,971 / 1,0 / 0,902; phrase 1,0 / 0,963 / 0,907;
no_accent 0,929 / 1,0 / 0,875; abbrev 1,0 / 1,0 / 0,944; typo 1,0 / 1,0 / 0,611; chatty 1,0 / 1,0 / 0,708;
english 0,667 / 0,667 / 0,131 (3 câu).

Sửa bộ câu sau lần chạy đầu (lỗi ở câu / đáp án, không phải hệ thống): m018 đổi câu ("phòng lab của trường" trùng bài
khai trương Lab VTIT); m025 / m036 thêm thông báo 71714 của cùng hội thảo; m027 thêm bài "Chúc mừng thành công OLP'24…
ICPC 2024" (cũng là bài bế mạc); f013 đổi `media_mode` mong đợi sang `optional` (câu có "lễ tốt nghiệp", ảnh trả về
thuộc đúng bài). Scorer: `video_hit` nhận `kind=video` (v2/v3 gộp mỗi video thành 1 nguồn), bản đầu chỉ nhận
`video_frame` nên 1/15.

18 câu còn trượt, đều do hệ thống (chưa sửa code, chỉ ghi nhận):
- **Router (luật từ khoá)**: m033 "Show me photos…" → `none` (không có từ tiếng Anh); m015 "anh it festival 2016" →
  `optional` ("anh" đầu câu không dấu không có ngữ cảnh trong `_REQUIRED`); m021 "lễ trao quyết định bổ nhiệm" →
  `wants_file=true`; f002 "tải ở đâu", f007 / f010 / f013 nêu tên văn bản (kế hoạch học kỳ, mẫu sơ yếu lý lịch, sơ đồ hội
  trường) → `wants_file=false` (file vẫn được tìm thấy: file_hit 15/15); o014 buổi làm việc với Portsmouth → `none`.
- **Ảnh trả về không thuộc bài đúng dù bài đứng hạng 1 chữ**: m001 (8 ảnh từ thông báo tổ chức lễ 71845 và 4 bài
  khác, không ảnh nào của bài tường thuật 71855 có 16 ảnh); m023 "ảnh cựu sinh viên Phạm Văn Cương" (tên chỉ ở thân bài,
  caption = tiêu đề + OCR nên nhánh media không khớp; 8 ảnh của bài khác).
- **Video**: v002 "video giới thiệu CTĐT Công nghệ thông tin" → trang CTĐT html và 4 bài tin, bài có video 71355 ngoài
  top 10; v003 (MMT&TTDL) và v004 (Đa phương tiện) → bài tin giới thiệu ngành 2026 không có video đứng trên bài
  tuyển sinh 2025 có video (hạng 4), media toàn ảnh bài khác; v015 (gõ sai) có 4 ảnh đúng bài nhưng mất slot video;
  v008 / v013 tiếng Anh: `sufficient=false`, v013 không tìm được bài.
- **Ngoài phạm vi đòi ảnh** (x002 tháp Eiffel, x004 nấu phở): `sufficient=false` đúng nhưng vẫn trả 8 ảnh ngẫu nhiên
  (ngưỡng `clip_min` / `caption_min` không chặn) — trái AC-R9.

## 2026-11-05 — trả lời có ảnh: gom theo bài, nhãn OCR, chặn link chưa kiểm (nhánh fix/media-answer)

Sửa ba lỗi thấy khi thử câu hỏi có ảnh trên UI:
- link OCR đọc sai ("hqui" thay "haui") bị chép thành link;
- 6 ảnh cùng bài thành 6 gạch đầu dòng gần giống nhau;
- lưới ảnh trên UI.

Ngữ cảnh ảnh giờ gom theo bài: một dòng "Bài: … — đăng ngày … — N ảnh", rồi mỗi ảnh một dòng `[n]` chỉ mang chữ máy đọc của chính nó, có nhãn "OCR, có thể sai". `citations.enforce` bỏ dạng link của URL không có trong nguồn văn bản (`unlink_unverified`), gộp `[n]` lặp (`dedupe_runs`) và nhận thêm các kiểu trích dẫn của gpt-oss: `【2†L1-L4】`, chỗ giữ chỗ `[n]` chép nguyên từ prompt, và `[Nguồn: 6, 7]`.

Truy xuất không đổi so với develop: golden_v1 1,0 / 0,947, golden_v2 71/80 / 0,731, list_v2 0,940 (38/42) / sufficient_acc 0,94.

RAGAS chạy 2 lượt mỗi bản: trả lời bằng gpt-oss-120b-sale, giám khảo qwen3.5-flash-sale, cache tắt.

| | 14 câu có ảnh: dev / dev2 / fix / fix2 | 20 câu chỉ chữ: dev / dev2 / fix / fix2 |
|---|---|---|
| faithfulness | 0,911 / 0,896 / 0,864 / **0,954** | 0,992 / 0,959 / 0,931 / 0,993 |
| answer_relevancy | 0,838 / 0,842 / 0,794 / 0,795 | 0,657 / 0,659 / 0,679 / 0,656 |
| context_precision* | 0,611 / 0,596 / 0,629 / 0,627 | 0,787 / 0,781 / 0,757 / 0,813 |
| context_recall | 0,952 cả 4 lượt | 0,838 / 0,808 / 0,850 / 0,810 |

\* context_precision của câu có ảnh không so được giữa dev và fix, vì gom theo bài làm đổi số đoạn ngữ cảnh.

**Câu có ảnh.** Lượt fix đầu tụt faithfulness vì 4 lỗi đã sửa trước lượt fix2:
- dòng tiêu đề thiếu ngày đăng;
- trích dẫn dạng `【n†…】`;
- chỗ giữ chỗ `[n]`;
- câu trả lời quá cụt.

answer_relevancy vẫn thấp hơn khoảng 0,045, chủ yếu do g061 (0,37): model bỏ qua prompt và tả từng ảnh. Ở g064 model nói "không có thông tin ngày đăng" dù dòng tiêu đề có ngày. Cả hai là hành vi của model, chưa sửa.

**Câu chỉ chữ.** Mức tụt 0,931 ở lượt fix đầu là nhiễu: fix2 0,993 ngang dev. Hai lượt dev cũng lệch nhau 0,03.

## 2026-11-05 — nhánh cụm tên riêng + evidence đòi nguyên cụm tên (nhánh feat/person-name-retrieval)

Sự cố: "khoảnh khắc của sinh viên Phạm Văn Tùng" không ra bài "Chúc mừng đội thi HaUI_Submission". Dense nhúng tên theo
"âm hưởng" (Nguyễn Thanh Tùng cosine 0,53–0,62; đoạn chứa đúng tên 0,37–0,45, ngoài top 200), BM25 bỏ dấu coi
pham / van / tung là từ phổ biến (20,6 / 34,4 / 6,4 % số điểm), graph chỉ có ~4 % tên (78 % tên chỉ ở 1 bài) và danh sách
OR bị "khoảnh khắc" (85 điểm) chôn. `sufficient` True vì evidence đếm token rời.

Sửa (tổng quát, không danh sách từ):
- **A.** `query_rewriter.name_phrases`: cụm ≥ 2 từ liên tiếp viết hoa chữ đầu (bỏ từ đầu câu trừ khi cả câu là cụm; cắt ở
  dấu câu / số / viết tắt toàn hoa). Mỗi cụm thêm 1 dense + 1 BM25 lọc `vector_store.phrase_filter` (MatchPhrase trên
  index text `phrase_matching` của `content` / `caption`), bỏ cụm có ≥ 200 điểm ở một collection. `_rrf` giữ `dense_score`
  từ bất kỳ danh sách dense nào (hit vào trước qua BM25 không còn cosine None).
- **C.** `agent.evidence`: thiếu nguyên cụm tên (so bỏ dấu) trong ngữ cảnh → `sufficient` False ("thấp: tên riêng …").
- Index tạo mới (một lần, `ensure_payload_indexes`, không re-embed; RAM Qdrant 211 → 293 MiB): text `phrase_matching`
  `content` + `caption` trên `sict_text_v3` (16.628 điểm có `content`) và `sict_ocr_v3` (4.449 điểm có `caption`).
- Cache truy vấn `q:v10` → `q:v11`.
- Bộ mới `eval/person_v1.jsonl`: 40 câu dương (giảng viên có trang riêng 6, sinh viên có tên trong tiêu đề 8, tên CHỈ
  nhắc trong thân bài 22, tên là khái niệm 4; dạng "X là ai", "thành tích của X", "khoảnh khắc của sinh viên X", "ảnh X",
  1 không dấu, 1 gõ sai) + 4 câu âm tính (tên không có trong corpus, `expected_urls` rỗng: eval_api bỏ qua, chấm
  `sufficient` riêng). Mọi url có trong `documents.url`, chữ của bài (Qdrant v3) chứa nguyên cụm tên; câu âm tính:
  MatchPhrase đếm 0 điểm.

Đo qua `/query` trên 2 API cùng index v3, cache Redis tắt, develop (`b68132c`) vs nhánh:

| Bộ | develop | nhánh |
|---|---|---|
| golden_v1 R@5 / MRR@10 / media_mode | 1,0 / 0,947 / 0,871 | 1,0 / 0,947 / 0,871 |
| golden_v2 url_hit@5 / MRR / abstain / false_abstain | 71/80 / 0,731 / 3/8 / 7/80 | **72/80 / 0,738** / 3/8 / 7/80 |
| list_v2 coverage / full / sufficient_acc | 0,940 / 38/42 / 0,94 | 0,940 / 38/42 / 0,94 |
| person_v1 R@5 / hit@8 / MRR | 0,650 / 0,650 / 0,577 | **0,975 / 0,975 / 0,933** |
| person_v1 hit@8 theo nhóm (giảng viên / tiêu đề / thân bài / khái niệm) | 6/6 · 8/8 · 10/22 · 2/4 | 6/6 · 8/8 · **21/22** · **4/4** |
| person_v1 `sufficient` đúng (dương: True khi trúng; âm: False) | 29/44 (âm 1/4) | **39/44 (âm 4/4)** |

- Câu đổi hạng: golden_v2 g084 (tiếng Anh, "Information Technology") None → 2; golden_v1, list_v2 không câu nào đổi;
  person_v1 16 câu lên (p15 "khoảnh khắc … Phạm Văn Tùng" None → 1, p16 bản gõ sai None → 1, p17 3 → 1, p40 4 → 3…),
  **không câu nào tụt** ở cả 4 bộ.
- `sufficient` lật: golden_v1 0/70, golden_v2 0/88, list_v2 0/50. person_v1 5 câu True → False: 3 câu âm tính (đúng);
  p29 (bài đích hạng 1 nhưng tên ở ký tự 1.385 của đoạn 1.563 ký tự, ngân sách 11.000 / 8 nguồn = 1.375 cắt mất tên → LLM
  thật sự không thấy tên); p33 (cụm có trong ngữ cảnh, False do cosine tốt nhất 0,42 < 0,45). Hai câu kia của báo cáo
  điều tra (g004 "Năm Trường…", m30 "No Solution") không còn nhận nhầm vì cách lấy cụm (từ đầu câu, token lẫn ký hiệu).
- Biến thể đã thử: 1 danh sách dense / cụm → person_v1 hit@8 0,925, MRR 0,726 (p25 "khoảnh khắc … Nguyễn Hồng Đông"
  ngoài top 8: 1/61 RRF thua đoạn có mặt ở dense + BM25 + graph); 2 danh sách (dense + BM25) → 0,975 / 0,933, 3 bộ kia
  như nhau.
- Độ trễ `/query` (không LLM, cache nhúng nóng, xen kẽ 2 vòng, máy dev có tải khác): câu không tên p50 / p90 develop
  0,08 / 0,12 s vs nhánh 0,09 / 0,14 s; câu có tên 0,07 / 0,11 s vs 0,10 / 0,15 s (+2 count + 2 tìm / cụm, song song).
- Còn trượt: p20 "Chu Thi Lan Anh la ai" (không dấu: cụm không khớp index có dấu; khôi phục dấu chỉ dùng cho dense).

RAGAS trên 21 câu đổi nguồn: 20 câu person_v1 và golden_v2 g084. Câu trả lời sinh bằng gpt-oss-120b-sale, giám khảo là qwen3.5-flash-sale, mỗi bản sinh câu trả lời 1 lần, cache tắt. Một phần lượt chấm trúng lúc kiraai báo lỗi 402 hết tiền nên phải chấm lại bằng `--retry-errors`. Còn 2 câu ở develop và 5 câu ở nhánh không chấm được, nên số câu (n) mỗi chỉ số khác nhau.

| | develop | nhánh |
|---|---|---|
| faithfulness (n=17) | 0,973 | 0,907 |
| answer_relevancy (n=20) | 0,156 | **0,567** |
| context_precision (n=20) | 0,557 | **0,899** |

- **develop** trả lời "Không có thông tin về …" ở 15/21 câu, vì tên người không vào ngữ cảnh. Câu từ chối thì faithfulness luôn đúng nhưng answer_relevancy gần 0.
- **Nhánh** trả lời đúng người, ví dụ p15 "Phạm Văn Tùng là một trong ba thành viên…" và p20 "Chu Thị Lan Anh là học viên thạc sĩ ngành Hệ thống thông tin…".
- **Faithfulness giảm** chủ yếu do giám khảo:
  - n04: cả hai bản cùng từ chối "Không có thông tin về giảng viên Lý Thị Phương Thảo", nhưng nhánh bị chấm 0,20.
  - p24: 0,50.
  - p16: 0,83.
- Các câu âm tính n02–n04 vẫn từ chối đúng.

## 2026-11-05 — media theo bằng chứng: ảnh của bài chữ đứng đầu, video, `sufficient` (nhánh feat/media-evidence-gate)

Vấn đề (đã chẩn đoán trên code + API dev): ứng viên media chỉ đến từ CLIP / caption toàn collection, không lấy ảnh của
chính các bài nhánh chữ tìm ra; `caption_kw` qua gate không cần ngưỡng; câu đòi video bị ảnh thường lấp hết chỗ; câu
`sufficient` False vẫn nhận ảnh ngẫu nhiên; `accept_api` lấy thumbnail video làm ảnh truy vấn (U6 q15).

Sửa (chủ dự án duyệt A + C + D + E; tổng quát, không thêm danh sách từ khoá, không thêm ngưỡng):
- **A.** `retriever._doc_media`: 1 lượt Qdrant sau nhánh chữ, ảnh / khung hình của các bài thuộc `text_hits[:3]`
  (`document_id` MatchAny), xếp theo cosine câu hỏi↔ảnh, modality `doc`, cùng ngưỡng `t2i_min`. `gate`: ảnh gắn đứng
  trước theo hạng bài rồi cosine, `page_url` = bài chữ đã kéo ảnh lên; `caption_kw` chỉ nhận khi gắn.
- **Ý định quyết định số lượng** (`router.media_cap`): `required` top_k, `optional` 3, `none` `router.none_max`
  (mặc định **0**, xem dưới). `media_mode` vẫn là ý định của router nên `media_mode_acc` giữ nguyên nghĩa.
- **C.** `sufficient` False → `router.supported_media`: chỉ giữ media thuộc 3 nguồn chữ đầu, đúng loại được hỏi, và ảnh
  `i2i` của ảnh người dùng gửi.
- **D.** `Route.wants_video` (chính "video|clip" của `_REQUIRED`): thêm 1 lượt CLIP chỉ trong `video_frame` + 1 lượt
  dense chỉ trong caption video; video trước ảnh, ảnh không gắn bị loại; bài chữ đứng đầu có video → bỏ media không gắn.
- **E.** `accept_api`: U3–U6 chỉ dùng media `kind=image` (index v3 trả video là `kind=video`).
- Cache `q:v11` → `q:v12`.

Đo qua `/query`, 2 API cùng index v3, cache Redis tắt, develop (`289e847`) vs nhánh:

| số đo | develop | nhánh |
|---|---|---|
| media_v1 media_mode_acc / wants_file_acc | 98/101 / 96/101 | 98/101 / 96/101 |
| media_v1 url_hit / text_mrr@10 | 95/97 / 0,846 | **96/97** / 0,846 |
| media_v1 required_has_media | 50/51 | 47/51 |
| media_v1 media_on_target | 57/65 (0,877) | 58/67 (0,866) |
| media_v1 media_precision (micro) | 0,674 | **0,798** |
| media_v1 none_clean | 31/33 | **33/33** |
| media_v1 video_hit | 10/15 | **12/15** |
| media_v1 file_hit / abstain_ok / false_abstain | 15/15 / 4/4 / 4/97 | 15/15 / 4/4 / 4/97 |
| media_v1 precision theo case: image / optional / video | 0,778 / 0,974 / 0,333 | 0,818 / 0,927 / 0,644 |
| golden_v1 R@5 / MRR@10 / media_mode / required on target | 1,0 / 0,947 / 0,871 / 7/7 | như develop |
| golden_v2 R@5 / MRR@10 / media_mode / required on target | 0,91 / 0,746 / 0,909 / 7/8 | như develop |
| person_v1 R@5 / MRR@10 / media_mode | 0,975 / 0,921 / 0,795 | như develop |
| list_v2 coverage / full / sufficient_acc | 0,940 / 38/42 / 0,94 | như develop |
| accept_api (script nhánh) | 48/49 | 48/49 |

Nguồn chữ không đổi ở câu nào của 5 bộ (chỉ media đổi). `accept_api` trượt `M /metrics` ở cả hai vì API thử trỏ Redis
vào cổng đóng (`sict_pipeline_metrics_up 0`). Script develop chạy trên API develop: 48/50, U6 q15 FAIL (thumbnail video
làm ảnh truy vấn); script nhánh bỏ q15 khỏi U3–U6 vì media đúng bài của q15 chỉ có video.

Câu tốt lên: m001 0/8 → 8/8 ảnh đúng bài; m013 4/8 → 8/8; m018 6/8 → 8/8; v002 / v003 / v015 có video đúng bài (v002
qua lượt khung hình video, bài 71355 ngoài top chữ); v005 / v007 / v009 / v010 / v011 / v012 bớt ảnh lạc đề (v010 1/8 →
5/8); x002 tháp Eiffel, x004 nấu phở 8 → 0 media.

Câu tụt (mọi câu, kèm lý do):
- **m017** "thiệp sáng tạo 20/11 năm 2021": 3/8 → 0/8 đúng bài. Bài đúng (67435) hạng 3 nhánh chữ, bài hạng 1 (61717,
  cùng chủ đề năm khác) có đủ 8 ảnh vượt ngưỡng nên lấp hết chỗ. Lỗi xếp hạng chữ, ảnh chỉ đi theo.
- **o003 / o016** "Lễ khai mạc ICPC Asia Hanoi 2024 diễn ra ngày nào?": 0 → 1 ảnh của bài hạng 2 (71198, không phải bài
  khai mạc 71199 ở hạng 3). Trước đây không có ảnh nào vì CLIP toàn collection không ra ảnh của top bài.
- **v008** (tiếng Anh, `sufficient` False, cosine 0,436): 8 media (1 đúng bài) → 0. Luật C: chữ yếu thì media không gắn
  bị bỏ; bài đúng không nằm trong top chữ.
- **v004 / v013** `required_has_media` True → False: 8 media đều lạc đề → 0 (`sufficient` False), có `media_note`.
- Precision `optional` 0,974 → 0,927 do o003 / o016.

Còn trượt (không đổi so với develop): m023 "Phạm Văn Cương" bài đúng hạng 1 nhưng ảnh của bài có cosine tối đa 0,244 <
`t2i_min` 0,25 nên 8 ảnh CLIP không gắn lấp chỗ; m027 bài đúng hạng 2 sau bài hạng 1 nhiều ảnh (như m017); m033 tiếng Anh
(`none`, `sufficient` False).

**`none_max` để 0.** Đo trên 4 bộ (303 câu), câu route `none`: ảnh của bài chữ đứng đầu không tách được câu muốn xem
ảnh mà không có từ khoá ("khoảnh khắc của sinh viên Phạm Văn Tùng", "Show me photos…") khỏi câu fact: p15 và p17
"Phạm Văn Tùng là ai" cùng top bài, cosine ảnh 0,29–0,33 vs 0,21–0,23; ảnh của câu fact cũng 0,1–0,42 (n008 "mã CTĐT"
0,375). Mô phỏng các luật (ảnh bài hạng ≤ 1 ≥ 0,25 / ≥ 0,30 / ≥ 0,33; nằm trong top 3–10 CLIP toàn collection; ≥ cosine
ảnh tốt nhất − 0,02 / 0,04; caption của bài trong top chữ): luật tốt nhất bắt 3/11 câu dương và 14/169 câu fact, luật bắt
đủ 11/11 dính 95/169 câu fact. Vị trí ảnh cạnh đoạn trả lời không dùng được: ~92 % ảnh không có `block_idx` trên Qdrant.
Chạy thật `none_max: 2` (API thứ ba, cùng nhánh): 13/14 câu dương có ảnh (8 đúng bài: p15 Tùng, p25, p38, q14, q37,
q59, g062, o014), nhưng 151/228 câu fact cũng có 1–2 ảnh; media_v1 none_clean 33/33 → 15/33; list_v2 coverage 0,940 →
0,932 (37/42; ảnh chia ngân sách ngữ cảnh). Bật hay không là quyết định của chủ dự án; câu loại này cần tín hiệu ý định
(LLM router `llm.rewrite_query` đã có), không phải bằng chứng truy xuất.

Biến thể đã thử, không giữ: chia lượt ảnh gắn theo từng hit chữ (mỗi hit một ảnh): m017 / m027 lên, nhưng bài hạng 1
thường đúng nên precision image 0,818 → 0,720, optional 0,927 → 0,634.

Độ trễ `/query` (không LLM, xen kẽ 2 API, 2 vòng × 101 câu media_v1, cache nhúng nóng): develop p50 / p90 0,118 / 0,198 s,
nhánh 0,132 / 0,195 s (+1 lượt Qdrant lọc theo bài sau nhánh chữ; câu video +2 lượt song song).

Câu trả lời `/chat` sẽ đổi (ngữ cảnh LLM đổi vì media đổi; chưa chạy RAGAS): media_v1 38/101 (m001, m003, m004, m006,
m007, m008, m013, m015–m018, m028, m030, m034, m035, v002–v013, v015, o003, o005, o007, o010, o012, o013, o015, o016,
x002, x004), golden_v1 q15, q43, q48–q51, q70; golden_v2 g057, g059, g063, g064; person_v1 p06; list_v2 m27.
