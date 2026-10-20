# Hướng dẫn gán nhãn bộ vàng graph (D20, giai đoạn B)

File `eval/graph_gold_v1.jsonl`, mỗi dòng 1 tài liệu (tự chứa văn bản cần đọc). Người duyệt điền
`entities` và `media`, rồi đổi `status` từ `draft` sang `reviewed`. Chỉ dòng `reviewed` được tính
điểm (`scripts/eval_graph.py`).

`draft.rules` (luật hiện tại) và `draft.llm` (gán nhãn độc lập, không xem kết quả luật) chỉ là **gợi
ý**. Đọc hết `title`, `body`, `images[].alt/context/ocr` rồi tự quyết. Đừng chỉ chép gợi ý: thực thể
cả hai nguồn cùng bỏ sót mới là thứ bộ vàng cần bắt.

## Gán gì

`entities`: mọi thực thể **được nêu tên trong văn bản của dòng đó**.

| type | Gán | Không gán |
|---|---|---|
| `person` | người có họ tên đầy đủ (≥ 2 chữ) trong văn bản, kể cả sinh viên, khách mời, doanh nhân | chức danh không kèm tên ("Hiệu trưởng"), tên chỉ có tên riêng ("thầy Hợp"), nhân vật lịch sử dùng làm tên địa danh/tổ chức ("Đoàn TNCS Hồ Chí Minh") |
| `event` | sự kiện / cuộc thi / hội thảo / lễ **có tên riêng hoặc là chuỗi lặp hằng năm** ("IT Festival 2023", "Olympic Tin học Sinh viên Việt Nam", "Lễ tốt nghiệp 2025", "Hội thảo ICISN") | cụm chung chung ("hội thảo khoa học", "buổi seminar"), chủ đề học thuật không phải sự kiện ("Học máy") |
| `org` | CLB và đội tuyển thường trực của trường ("CLB Tin học HIT", "Đội Olympic Tin học", "IT Supporter") | khoa / trường / doanh nghiệp / Đoàn–Hội; **đội thi đặt tên riêng cho 1 cuộc thi** ("HaUI.HIT-H2K") |

Mỗi thực thể:

```json
{"type": "person", "name": "Đặng Trọng Hợp", "roles": ["chủ trì"], "evidence": "TS. Đặng Trọng Hợp chủ trì hội thảo"}
```

- `name`: dạng đầy đủ, đúng dấu như trong bài, **không** kèm học hàm ("Đặng Trọng Hợp", không phải "TS. Đặng Trọng Hợp").
  Sự kiện: tên kèm năm nếu bài nêu năm ("IT Festival 2023"). Bộ chấm điểm tự bỏ năm khi so chuỗi sự kiện.
- `roles` (có thể rỗng) — vai trò của người/CLB **trong bài này**, chọn trong danh sách:
  `chủ trì`, `phát biểu`, `báo cáo viên`, `diễn giả`, `khách mời`, `ban giám khảo`, `hướng dẫn`,
  `đạt giải`, `tham gia`, `tổ chức`, `thành viên`, `trưởng nhóm`, `chủ nhiệm đề tài`, `tác giả`, `khác`.
  Chỉ gán khi văn bản nói rõ. Không suy đoán.
- `evidence`: câu trích **nguyên văn** từ văn bản có chứa tên (và vai trò nếu có).
- Một người nêu nhiều lần thì gán 1 lần, gộp các vai trò.

`media`: với từng ảnh trong `images`, ghi thực thể mà **văn bản đi kèm ảnh đó nêu tên** (alt riêng,
đoạn văn sát ảnh, chữ OCR trên ảnh):

```json
{"sha256": "c392…", "entities": ["Nguyễn Thị Cẩm Ngoan"]}
```

- Không đoán ai trong ảnh qua khuôn mặt, và không gán chỉ vì bài nói về người đó (graph đánh dấu
  trường hợp đó là cạnh yếu riêng).
- **Alt chỉ lặp lại tiêu đề bài KHÔNG tính** là văn bản riêng của ảnh (site chép tiêu đề vào alt cho
  85% ảnh). Chỉ alt khác tiêu đề, đoạn văn sát ảnh và OCR mới tính.
- Ảnh không có văn bản nêu tên → bỏ qua (không cần dòng rỗng).
- Xem ảnh tại `/media/s/<sha256>` trên API nếu cần.

## Ví dụ

Đoạn văn sát ảnh: "ThS. Nguyễn Thị Cẩm Ngoan trao giải Nhất cho đội HIT TitanS (CLB Tin học HIT)".

```json
"entities": [
  {"type": "person", "name": "Nguyễn Thị Cẩm Ngoan", "roles": ["ban giám khảo"], "evidence": "ThS. Nguyễn Thị Cẩm Ngoan trao giải Nhất cho đội HIT TitanS"},
  {"type": "org", "name": "CLB Tin học HIT", "roles": ["đạt giải"], "evidence": "đội HIT TitanS (CLB Tin học HIT)"}
],
"media": [{"sha256": "…", "entities": ["Nguyễn Thị Cẩm Ngoan", "CLB Tin học HIT"]}]
```

## Chấm điểm

```bash
python scripts/eval_graph.py --gold eval/graph_gold_v1.jsonl --pred rules          # luật hiện tại
python scripts/eval_graph.py --gold eval/graph_gold_v1.jsonl --pred llm            # draft.llm / giai đoạn C
```

So khớp:
- Người: theo khoá giữ dấu (Hùng ≠ Hưng; thủy = thuỷ).
- Sự kiện / CLB: theo chuỗi bỏ dấu, bỏ năm; một tên chứa trọn tên kia (≥ 2 từ) cũng tính là khớp.
- Ảnh: theo cặp (ảnh, thực thể).
- Vai trò: chỉ chấm trên các thực thể đã khớp, khi phía dự đoán có vai trò.
