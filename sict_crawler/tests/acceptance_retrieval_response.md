# Hợp đồng hành vi: Retrieval + Response — viết từ plan & trao đổi, KHÔNG đọc code nguồn
# CHƯA CHẠY (chờ crawl + preprocessing full). File test thực thi: test_retrieval_response_contract.py

## Nhóm R — Retrieval (tìm kiếm kết hợp)

- **AC-R1.** Mỗi câu hỏi luôn trả đoạn văn bản kèm nguồn (URL + tiêu đề/heading + đoạn trích + ngày đăng nếu có). Media (ảnh/frame video) **chỉ trả khi router cho phép** (AC-R8, AC-R9) và được phép rỗng; mỗi media trả về có URL + caption + timestamp (nếu là frame video).
- **AC-R2.** Ảnh tìm được bằng chữ: caption ảnh đã nhúng chung không gian chữ nên hỏi tên người/sự kiện phải ra ảnh liên quan.
- **AC-R3.** Hợp nhất 3 nguồn (chữ 384 chiều, ảnh 512 chiều, metadata sự kiện) rồi xếp lại. Một video tính 1 điểm (không để 20 frame của cùng video lấn hết top).
- **AC-R4.** Tôn trọng `top_k`. Không có gì khớp thì trả rỗng, cấm bịa kết quả.
- **AC-R5.** Câu hỏi được viết lại/mở rộng trước khi tìm (xử lý tên viết tắt, thiếu dấu).
- **AC-R6.** Kết quả trùng (cùng bài, cùng ảnh qua nhiều occurrence) phải gom lại, không lặp.
- **AC-R7.** Tìm theo từ khoá không phân biệt dấu: hỏi không dấu ("tuyen sinh") ra bài có dấu ("tuyển sinh") và ngược lại.
- **AC-R8.** Router ý định: mỗi câu hỏi được xếp mức media `none` / `optional` / `required` và cờ muốn file đính kèm. Việc xếp loại đi cùng bước viết lại câu hỏi (không thêm lượt gọi LLM); không có LLM → luật từ khoá. Mức `none` (quy chế, học phí, hạn nộp, số liệu, liên hệ...) → không chạy nhánh media, không đưa caption ảnh vào ngữ cảnh LLM.
- **AC-R9.** Chặn theo bằng chứng: mức `optional` chỉ trả media thuộc các bài đứng đầu kết quả chữ, tối đa 3; mức `required` tối đa `top_k`. Mọi media phải vượt ngưỡng điểm gốc theo loại (CLIP ảnh, caption E5) — không dùng điểm RRF để cắt; ngưỡng hiệu chỉnh trên bộ câu hỏi vàng (AC-G3). Không media nào đạt → danh sách rỗng kèm ghi chú "không tìm thấy ảnh phù hợp", cấm trả ảnh không liên quan cho đủ số.
- **AC-R10.** Câu hỏi muốn tải tài liệu/biểu mẫu/quy định → trả kèm link file đính kèm liên quan (tài liệu loại `attachment`) cùng trang chứa nó.
- **AC-R11.** Lọc metadata: lọc **cứng** (mục, loại tài liệu, khoảng năm) chỉ khi caller truyền `filters`; thông tin suy từ câu hỏi (mục, năm, "mới nhất") chỉ **ưu tiên mềm** (nâng điểm), không loại kết quả. Tài liệu thiếu metadata không bị phạt. Bộ lọc khác nhau → khoá cache khác nhau.
- **AC-R12.** Response có trường `media_mode`; giao diện chỉ hiện khung ảnh/video khi danh sách media không rỗng.
- **AC-R13 (graph D20).** Người / sự kiện / CLB nêu trong câu hỏi (có dấu hoặc không, tên đầy đủ, "tên đệm + tên" khi không nhầm người, tên viết tắt như HIT, ICISN) được nhận ra từ danh bạ thực thể; response có trường `entities`.
- **AC-R14.** Hỏi ảnh một người / sự kiện có trong graph: ảnh gắn thực thể bằng bằng chứng mạnh (tên trong alt riêng, đoạn văn sát ảnh, OCR, trang hồ sơ) đứng trước ảnh chỉ khớp CLIP; hỏi về NGƯỜI mà graph đã có ảnh thì không chèn ảnh chỉ khớp CLIP.
- **AC-R15.** Chuỗi sự kiện lặp theo năm (IT Festival, Lễ tốt nghiệp, Olympic Tin học) là 1 thực thể; câu hỏi kèm năm ưu tiên ảnh của năm đó.
- **AC-R16.** Mỗi cạnh ảnh↔thực thể lưu nguồn (`alt|context|ocr|doc`), trọng số và đoạn trích bằng chứng (AC-C6). Graph không định danh khuôn mặt: ảnh "gắn với X" nghĩa là văn bản đi kèm ảnh nêu X, không phải nhận diện mặt (AC-C1).
- **AC-R18.** Cạnh thực thể ↔ thực thể chỉ sinh từ bằng chứng cùng xuất hiện có cấu trúc (cùng ảnh, cùng tiêu đề, tiêu đề + thân bài/ảnh), kèm số bài làm chứng; hai tên chỉ cùng nằm trong thân bài (VD danh sách sinh viên) không tạo cạnh. Câu hỏi nêu 1 thực thể → ngữ cảnh LLM có dữ kiện thực thể liên quan theo loại; nêu ≥ 2 thực thể → bài/ảnh gắn cả hai đứng đầu.
- **AC-R17.** Graph không làm giảm chất lượng tìm chữ trên bộ câu hỏi vàng (nhánh thực thể chỉ là 1 danh sách thêm vào RRF, không lọc cứng).

## Nhóm S — Response (trả lời)

- **AC-S1.** Câu trả lời chỉ dùng ngữ cảnh tìm được. Mọi tên người phải kèm bằng chứng (đoạn trích + nguồn).
- **AC-S2.** Bằng chứng không đủ → nói không biết, cấm suy đoán (`sufficient=false`).
- **AC-S3.** Tầng trả lời vẫn chặn 2 kiểu cấm (vị trí trong ảnh, ảnh trẻ em) dù tầng tìm kiếm có trả về gì.
- **AC-S4.** LLM chết/thiếu key → vẫn trả danh sách nguồn tìm được, phần đáp án sinh để trống, không sập request.
- **AC-S5.** Trả lời ngắn gọn tiếng Việt, đúng câu hỏi.
- **AC-S6.** LLM nhận nội dung đầy đủ của các đoạn tìm được (trong ngân sách ngữ cảnh cấu hình), không chỉ phần trích ngắn hiển thị trên giao diện. Bước kiểm tra "đủ ngữ cảnh" đọc đúng phần ngữ cảnh mà LLM sẽ dùng để trả lời.
- **AC-S7.** Mỗi câu hỏi có trần tổng thời gian xử lý (mặc định 20 s) và mỗi lần gọi LLM có timeout riêng. Quá hạn → trả danh sách nguồn kèm ghi chú, không treo request.

## Nhóm K — Cache đa tầng (theo thiết kế cache đã chốt)

- **AC-K1.** Ảnh/media (nội dung bất biến theo key) được edge cache dài (30 ngày); trang giao diện cache ngắn (10 phút); API POST không cache.
- **AC-K2.** Hỏi lại câu giống hệt (cùng câu + cùng top_k) → phục vụ từ cache L2, không tính lại embedding/retrieval.
- **AC-K3.** Bài bị sửa/xóa → cache liên quan bị vô hiệu (ảnh bất biến theo key nên không cần).
- **AC-K4.** Cache đầy hoặc backend chậm → request vẫn đi thẳng backend (fail-open), không sập.
- **AC-K5.** Chat streaming không bị proxy dồn cục (tắt buffer trên luồng stream).

## Nhóm P — Hiệu năng API (máy 2 vCPU / 4 GB)

- **AC-P1.** Một câu hỏi đang xử lý không chặn request khác: giao diện, `/media` vẫn phản hồi trong lúc `/chat` đang chạy.
- **AC-P2.** Số câu hỏi xử lý đồng thời có giới hạn; quá tải thì xếp hàng hoặc trả lỗi tạm thời (503), không để hết RAM.
- **AC-P3.** API chỉ báo sẵn sàng (`/readyz`) khi model đã nạp xong; câu hỏi đầu tiên sau khi khởi động không phải chờ nạp model.

## Use case đa phương thức (D21, không gian jina chung chữ+ảnh)

- **AC-U1** text→text: câu hỏi chữ trả đoạn văn kèm nguồn; mỗi nguồn có `heading_path` (mục chứa đoạn, D23) khi chunk dựng từ block; ngữ cảnh LLM in `[loại] Tiêu đề › Mục (ngày)`.
- **AC-U2** text→image: vector chữ (tiền tố query) tìm trong collection ảnh; ảnh chỉ trả khi vượt `t2i_min` và (mức `optional`) thuộc bài chữ đứng đầu; nguồn có `via=clip|graph|caption`.
- **AC-U3** image→image: ảnh người dùng gửi tìm ảnh giống (`via=i2i`) vượt `i2i_min`; KHÔNG bị buộc thuộc top bài chữ; giao diện hiện riêng nhóm "Ảnh tương tự".
- **AC-U4** image→text: vector ảnh tìm chunk/caption (lọc `i2t_min` khi truy vấn chỉ có ảnh) và kéo đoạn gần ảnh nhất của BÀI chứa ảnh giống (`via=i2i-doc`) đứng đầu danh sách chữ.
- **AC-U5** text+image→text: RRF các danh sách {dense chữ, BM25, graph, dense ảnh}.
- **AC-U6** text+image→image: ảnh theo vector chữ (cần thuộc bài chữ đứng đầu ở `optional`) cộng ảnh giống theo vector ảnh (không cần thuộc); gộp theo khoá media, mỗi video 1 chỗ.
- **AC-U7** text+image→text+image: U5 và U6 cùng lúc; `inputs=["text","image"]`, `outputs` = những gì thực sự trả về.
- **AC-U8** truy vấn CHỈ có ảnh (question rỗng): router mức `required`, ra cả ảnh lẫn chữ; judge dùng câu hỏi giả "(chỉ gửi ảnh)"; không chạy nhánh lexical; ảnh hỏng/không giải mã được và không có chữ → rỗng, không lỗi.
- **AC-U9** Response có `inputs`/`outputs` (thực tế); ảnh của người dùng chỉ vào LLM trả lời khi `llm.answer_with_image` bật (mặc định tắt: chỉ retrieval, LLM chỉ thấy chữ truy xuất).
- **AC-U10** Ngưỡng tách theo cặp (`t2i_min`, `t2c_min`, `i2i_min`, `i2t_min`), điểm gốc, không dùng RRF để cắt; hiệu chỉnh bằng `eval/golden_images_v1` trước cutover (TẠM).
- **AC-S7** (D23) hash_v2 của một trang KHÔNG đổi khi thêm tính năng cấu trúc; sửa chỉ ảnh/video/file (markdown giữ nguyên) làm `hash_struct` đổi và incremental index lại bài đó; bài chưa có `hash_struct` chỉ được ghi bù cấu trúc, không index lại.

## Trích dẫn số trang và ảnh → trang (D24)

- **AC-S8** Mọi câu trả lời khi có nguồn đều kèm ≥ 1 trích dẫn hợp lệ `[n]` (n trong 1…số nguồn của ngữ cảnh). LLM không trích dẫn → server ghép dòng "Nguồn:" (tối đa 3 nguồn đầu); `[n]` không tồn tại bị bỏ. Ngữ cảnh rỗng thì không ép. Phản hồi có `citations` [{n, url, title, kind, page, heading_path, cite}].
- **AC-S9** Nguồn là file PDF/PPTX trích dẫn kèm số trang thật ("Tên file — trang N"); trang web trích dẫn bằng tiêu đề + mục (`heading_path`) và URL; docx/xlsx không có số trang thì chỉ tên file.
- **AC-H8** Chunk của PDF/PPTX dựng theo trang (khi `chunking.structured`): không chunk nào vắt qua ranh giới trang, trang trống giữ đúng số thứ tự, slide sắp theo số (slide2 trước slide10), PDF scan OCR theo từng trang.
- **AC-U11** Ảnh ánh xạ về URL bài + block + mục; ngữ cảnh phụ cận lấy chunk chứa block của ảnh (hoặc chunk liền trước/sau khi ảnh ở ranh giới); chưa có vị trí block thì rơi về chunk giống vector ảnh nhất của bài.
