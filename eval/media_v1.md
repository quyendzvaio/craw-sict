# eval/media_v1.jsonl — bộ đo các use case có ảnh / video / file (2026-11-05, `status: draft`)

Chủ dự án nhận xét bộ golden chưa có mấy câu yêu cầu trả ảnh. Bộ này phủ các use case có media của
`retriever.retrieve` / `router` và chấm trên **nguồn trả về của `POST /query`** (không trên câu trả lời LLM):
`scripts/eval_media.py --api URL --set eval/media_v1.jsonl --out eval/results/media_v1_<ngày>.json`.
Test đơn vị của hàm chấm: `tests/test_eval_media.py`.

Mỗi dòng: `id`, `question`, `case`, `style`, `answerable`, `media_mode` mong đợi (`none|optional|required`),
`wants_file`, `expected_urls` (bài hoặc file đúng), `expected_media_docs` (bài mà ảnh/video trả về phải thuộc về;
thường = `expected_urls`, rỗng với câu `none` / ngoài phạm vi), `min_media`, `reference` (1–2 câu lấy từ bài, cho
RAGAS), `notes` (id bài, số ảnh / khung hình trong `sict_image_v3` lúc soạn), `variant_of` (cùng nhu cầu, kiểu gõ khác).

Mọi bài trong `expected_urls` đã kiểm tra tồn tại trong Postgres (`documents.url`); bài ảnh có point `kind=image`
trong `sict_image_v3`, bài video có point `kind=video_frame`, file có `doc_type=attachment` (xem `notes`).

## 101 câu

| case | số câu | nghĩa |
|---|---|---|
| `image` | 36 | U2 chữ → ảnh, `media_mode=required`: "cho xem ảnh / hình ảnh …" về lễ, cuộc thi, hội thảo, đoàn khách, cơ sở vật chất, người; 2016–2026; tin-tuc, thu-vien-anh, guong-sang, tin-khcn, cán bộ giảng viên |
| `video` | 15 | "có video … không", "video giới thiệu ngành …", bản tin FIT News: bài có video (`videos` / `video_occurrence`) |
| `file` | 15 | `wants_file=true`: tải file / quyết định / biểu mẫu / kế hoạch / lịch, kể cả câu chỉ nêu tên văn bản |
| `optional` | 16 | hỏi tin tức / sự kiện, không đòi ảnh nhưng bài có ảnh |
| `none` | 15 | fact / quy định / học phí / lịch nghỉ: **không được** trả media; có các từ dễ nhầm khi bỏ dấu ("tiếng Anh" → anh/ảnh, "tại" → tải, "Lê" → lễ) |
| `out_of_scope` | 4 | ngoài phạm vi (`answerable: false`), 2 câu đòi ảnh / video |

Kiểu gõ (`style`): question 35, phrase 28, no_accent 14, abbrev 9, typo 6, chatty 6, english 3; 11 cặp `variant_of`.

## Tiêu chí chấm từng loại

| số đo | áp dụng | đạt khi |
|---|---|---|
| `media_mode_acc` | mọi câu | `route.media_mode` == `media_mode` mong đợi |
| `wants_file_acc` | mọi câu | `route.wants_file` == `wants_file` |
| `url_hit` | câu có `expected_urls` | một url mong đợi có trong nguồn chữ (`url` hoặc `parent_url`) hoặc media (`parent_url`) |
| `text_mrr@10` | câu có `expected_urls` | 1 / hạng của bài đúng đầu tiên trong nguồn chữ, mức tài liệu (file tính về bài cha) |
| `required_has_media` | `media_mode=required`, trong phạm vi | số media trả về ≥ `min_media` |
| `media_on_target` | câu có `expected_media_docs` | ≥ 1 media có `parent_url` thuộc `expected_media_docs`; câu `optional` không trả media thì bỏ qua (không bắt buộc có), câu `required` không trả media là sai |
| `media_precision` | câu có `expected_media_docs` | micro: tổng media thuộc bài đúng / tổng media trả về |
| `none_clean` | `media_mode=none` hoặc ngoài phạm vi | `media` rỗng |
| `file_hit` | `wants_file=true` | có nguồn `kind=file` với `url` trong `expected_urls` (top-k) |
| `video_hit` | `case=video` | có media `kind=video` (index v2/v3: `/query` gộp mỗi video thành 1 nguồn, url YouTube) hoặc `video_frame` (v1) thuộc bài đúng |
| `abstain_ok` | ngoài phạm vi | `sufficient == false` |
| `false_abstain` | câu trả lời được | `sufficient == false` (càng thấp càng tốt) |

So khớp url bỏ scheme, `//` thừa, dấu `/` cuối và chữ hoa. Bảng theo `case` và `style` dùng cùng các số đo.
Một câu "fail" khi trượt ít nhất một tiêu chí áp dụng cho nó (`summary.failures`).

## Lưu ý khi đọc kết quả

- `media_mode` mong đợi là **hành vi mong muốn**, không phải luật hiện có: vài câu cố ý không có từ khoá của
  `router.classify_rules` (`notes` ghi rõ: m033 "photos", o014 buổi làm việc không có từ sự kiện, f002 "tải ở đâu",
  f007 / f010 / f013 nêu tên văn bản không nói "tải"). Trượt `media_mode_acc` / `wants_file_acc` ở đó là
  tín hiệu cho router, không phải lỗi bộ câu.
- `m021` có "quyết định" trong câu hỏi ảnh nên `wants_file` của luật sẽ bật; bộ để `wants_file=false` theo ý người hỏi.
- Ảnh của một bài có thể dùng chung với bài khác (point ảnh v2 có `document_id` là danh sách); `/query` trả
  `parent_url` của bài được chọn, nên `media_on_target` so theo bài đó.
- Cache Redis (`q:v12:*`) khiến câu lặp trả nhanh; so sánh hai nhánh thì xoá cache giữa hai lượt (xem CLAUDE.md).
