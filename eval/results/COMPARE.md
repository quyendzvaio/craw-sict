# So sánh truy xuất: BM25 vs jina-clip-v2 vs bge-m3 (12 câu, corpus thu gọn)

Corpus: 550 chunk (132 chunk của tài liệu đáp án, 121 chunk PDF theo trang), 146 ảnh (66 ảnh của câu thị giác). Câu hỏi: `eval/compare_q12.jsonl` (assistant soạn, draft). **12 câu là mẫu rất nhỏ — dùng làm chỉ báo, không kết luận chất lượng tổng thể.**

## doc (10 câu chữ/PDF)

| phương pháp | R@1 | R@5 | R@10 | MRR@10 | n |
|---|---|---|---|---|---|
| bm25 | 0.6 | 1.0 | 1.0 | 0.783 | 10 |
| jina-clip-v2 | 0.9 | 1.0 | 1.0 | 0.933 | 10 |
| bge-m3 | 1.0 | 1.0 | 1.0 | 1.0 | 10 |
| bm25+jina-clip-v2 | 1.0 | 1.0 | 1.0 | 1.0 | 10 |
| bm25+bge-m3 | 1.0 | 1.0 | 1.0 | 1.0 | 10 |

## page (4 câu PDF)

| phương pháp | R@1 | R@5 | R@10 | MRR@10 | n |
|---|---|---|---|---|---|
| bm25 | 0.25 | 1.0 | 1.0 | 0.542 | 4 |
| jina-clip-v2 | 0.75 | 1.0 | 1.0 | 0.875 | 4 |
| bge-m3 | 1.0 | 1.0 | 1.0 | 1.0 | 4 |
| bm25+jina-clip-v2 | 0.75 | 1.0 | 1.0 | 0.875 | 4 |
| bm25+bge-m3 | 1.0 | 1.0 | 1.0 | 1.0 | 4 |

## image (2 câu thị giác)

| phương pháp | R@1 | R@5 | R@10 | MRR@10 | n |
|---|---|---|---|---|---|
| bm25 (caption) | 1.0 | 1.0 | 1.0 | 1.0 | 2 |
| jina-clip-v2 (caption) | 1.0 | 1.0 | 1.0 | 1.0 | 2 |
| jina-clip-v2 (ảnh) | 1.0 | 1.0 | 1.0 | 1.0 | 2 |
| bge-m3 (caption) | 1.0 | 1.0 | 1.0 | 1.0 | 2 |

## Từng câu (hạng của kết quả đúng đầu tiên, `-` = ngoài top 10)

| câu | loại | bge-m3 | bge-m3 (caption) | bm25 | bm25 (caption) | bm25+bge-m3 | bm25+jina-clip-v2 | jina-clip-v2 | jina-clip-v2 (caption) | jina-clip-v2 (ảnh) |
|---|---|---|---|---|---|---|---|---|---|---|
| q01 Một đội thi nước ngoài muốn tham dự kỳ thi lập trì… | pdf-page | 1 |  | 1 |  | 1 | 2 | 2 |  |  |
| q02 Em có điểm trung bình tích lũy 3,4 thì được xếp lo… | pdf-page | 1 |  | 3 |  | 1 | 1 | 1 |  |  |
| q03 Nhờ bạn đi thi thay mình lần đầu thì nhà trường xử… | pdf-page | 1 |  | 2 |  | 1 | 1 | 1 |  |  |
| q04 Buổi tổng kết và trao giải Olympic Tin học sinh vi… | pdf-page | 1 |  | 3 |  | 1 | 1 | 1 |  |  |
| q05 Thầy Khôi bên Công nghệ phần mềm học đại học ở đâu… | web | 1 |  | 2 |  | 1 | 1 | 3 |  |  |
| q06 Chỉ tiêu thạc sĩ ngành Hệ thống thông tin của SICT… | web | 1 |  | 1 |  | 1 | 1 | 1 |  |  |
| q07 Điều kiện để được miễn 100% học phí năm nhất theo … | web | 1 |  | 3 |  | 1 | 1 | 1 |  |  |
| q08 Buổi seminar quốc tế tháng 8/2026 với giáo sư ngườ… | web | 1 |  | 1 |  | 1 | 1 | 1 |  |  |
| q09 Hội nghị của các bạn lớp trưởng và bí thư chi đoàn… | web | 1 |  | 1 |  | 1 | 1 | 1 |  |  |
| q10 Chương trình Kỹ sư tài năng của HaUI năm 2026 dành… | web | 1 |  | 2 |  | 1 | 1 | 1 |  |  |
| q11 Cho tôi xem ảnh sản phẩm máy bay không người lái m… | visual | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| q12 Hình ảnh các tấm thiệp mà sinh viên khoa CNTT làm … | visual | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

Nhúng jina: model=jina, max_length=512, threads=4, load_s=13.1, queries_s=56.6, chunks_s=4044.7, images_s=695.4, max_rss_mb=3921

Nhúng bge-m3: model=bge-m3, max_length=512, threads=4, load_s=363.8, queries_s=2.6, chunks_s=844.3, max_rss_mb=4151
