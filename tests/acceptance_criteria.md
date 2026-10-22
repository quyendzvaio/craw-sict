# Hợp đồng hành vi (Acceptance Criteria) — viết từ plan & trao đổi, KHÔNG đọc code nguồn

> Mỗi tiêu chí có ID. File Gherkin ở `features/` và test thực thi ở
> `test_behavior_contract.py` đều trỏ về các ID này.

## Nhóm A — Định danh sự kiện (từ OCR + chunk xung quanh)

- **AC-A1.** OCR trên phông nền/backdrop/slide là tín hiệu chính để gọi tên sự kiện + ngày.
- **AC-A2.** Ngữ cảnh của ảnh lấy theo cấu trúc (caption, heading của section, tiêu đề bài, ngày đăng), KHÔNG lấy theo cửa sổ vài chunk cố định.
- **AC-A3.** Mỗi media có bản ghi cấu trúc: định danh media, nguồn xuất hiện, sự kiện {tên, ngày, bằng chứng}, danh sách người {tên chuẩn, vai trò, nguồn, đoạn trích, độ tin cậy}, OCR, caption, tham chiếu ngữ cảnh.
- **AC-A4.** Bài có 30 ảnh không được gán cả 30 ảnh cùng một định danh mù — mỗi ảnh giữ caption/embedding/thứ tự riêng.

## Nhóm B — Chống ảnh/video trùng

- **AC-B1.** So trùng theo 4 tầng từ rẻ đến đắt, khớp tầng nào dừng tầng đó: URL chuẩn hoá → SHA-256 file → pHash/dHash (Hamming ≤ 10) → embedding ảnh + tìm gần đúng.
- **AC-B2.** Ảnh resize/nén lại (khác URL, cùng nội dung) phải bị bắt ở tầng SHA hoặc pHash, chỉ xử lý 1 lần.
- **AC-B3.** Ảnh chụp liên tiếp cùng sự kiện (giống nhau nhưng là ảnh khác) KHÔNG được gộp xóa — giữ nhóm gần-giống, mỗi ảnh xử lý riêng.
- **AC-B4.** Cùng một ảnh dùng lại ở nhiều bài: chỉ 1 bản ghi media + nhiều bản ghi xuất hiện (mỗi bài giữ caption/alt riêng). Định danh cuối là hợp nhất mọi ngữ cảnh.
- **AC-B5.** Xóa 1 bài không xóa media nếu ảnh đó còn xuất hiện ở bài khác (đếm tham chiếu). Hết tham chiếu mới xóa.
- **AC-B6.** Video dùng mã video YouTube làm khóa (chuẩn hoá các dạng embed/watch/youtu.be); file video dùng hash file. Một video xuất hiện ở nhiều bài chỉ trích keyframe + phụ đề **một lần**, các bài còn lại chỉ thêm bản ghi xuất hiện.

## Nhóm C — Định danh người (chỉ giảng viên trong danh sách chuẩn)

- **AC-C1.** Chỉ người có trong danh mục giảng viên mới được định danh. Người lạ → để trống, KHÔNG lưu embedding khuôn mặt.
- **AC-C2.** Cấm dùng ngưỡng cố định kiểu "70%". Ngưỡng phải hiệu chuẩn trên tập ảnh gán nhãn tay, ra 2 ngưỡng: tự động chấp nhận / cần duyệt tay / loại. Ngoài điểm cao nhất phải xét khoảng cách với điểm cao thứ hai.
- **AC-C3.** Điểm cao nhưng khoảng cách với ứng viên thứ hai quá nhỏ → đẩy sang duyệt tay, không tự chấp nhận.
- **AC-C4.** Ưu tiên thu hẹp bằng chữ trước: bài/caption/OCR nhắc tên ai thì chỉ đối chiếu với vài người đó (1:1), không dò cả danh sách (1:N).
- **AC-C5.** Mặt quá nhỏ, mờ, nghiêng nhiều, đeo khẩu trang → bỏ qua đối chiếu, chỉ ghi nhận "có N khuôn mặt".
- **AC-C6.** Tên do AI trích xuất phải kèm đúng đoạn trích bằng chứng; không có bằng chứng thì để trống, cấm bịa tên.
- **AC-C7.** Chuẩn hoá tên qua danh mục (học hàm/học vị, viết tắt, sai dấu, trùng tên), không để AI tự đặt tên.

## Nhóm D — Video (keyframe + subtitle-only)

- **AC-D1.** Không xử lý toàn bộ video. Luồng hình: cắt theo cảnh + lấy mẫu thưa vài giây, gom còn tối đa 20 keyframe rồi chạy như ảnh.
- **AC-D2.** Luồng tiếng subtitle-only: có phụ đề sẵn thì lấy, không có thì bỏ qua luôn, KHÔNG chạy nhận dạng giọng nói.
- **AC-D3.** Subtitle ưu tiên bản do người đăng tải, sau đó mới tới bản tự động; cắt ngắn tối đa 2000 ký tự.
- **AC-D4.** Video bị chặn (ví dụ YouTube đòi đăng nhập từ IP máy chủ) → bỏ qua êm, trang bài vẫn được index bằng chữ, hệ thống không sập.
- **AC-D5.** Người chỉ xuất hiện 2–3 giây có thể lọt khỏi keyframe — chấp nhận documented, không cam kết bắt được mọi người.

## Nhóm E — Crawl tăng dần (incremental)

- **AC-E1.** Chỉ xử lý cái mới/cái đổi. URL cũ tự bỏ qua; trang mới được xếp hàng.
- **AC-E2.** Trang đổi nội dung → xử lý lại; trang không đổi → bỏ qua (chỉ chạm thời gian kiểm tra). "Nội dung" là phần nội dung chính đã trích (chữ, bảng, danh sách media); bộ đếm lượt xem, giờ hiển thị, sidebar, tin liên quan thay đổi KHÔNG tính là sửa.
- **AC-E3.** Trang trả 404/410 → xóa bài + vector liên quan. Trang lỗi mạng/trống body nhưng không phải 404/410 → giữ lại, chỉ ghi lỗi, KHÔNG xóa.
- **AC-E4.** Cầu chì xóa hàng loạt: xóa ≥ 20 bài và > 20% số bài kiểm tra → dừng để người kiểm tra (nghi sập/chuyển cấu trúc site).
- **AC-E5.** Media tăng dần: chỉ xử lý media mới; bài sửa chỉ thêm bản ghi xuất hiện mới.
- **AC-E6.** Có lượt đối soát nhẹ hằng đêm (chỉ quét ID/hash, không tải lại toàn bộ): mọi chunk/media có vector, mọi media trỏ tới file còn tồn tại trên kho lưu trữ; lệch thì ghi nhận và tự sửa được.
- **AC-E7.** URL lỗi tạm thời (mất mạng, timeout, 5xx, 429) được thử lại với thời gian chờ tăng dần, tối đa 5 lần, sau đó vào danh sách chết để người kiểm tra. Không URL nào bị mất vĩnh viễn chỉ vì một lần lỗi. Bài đã xoá mà sau này xuất hiện lại phải được phát hiện lại.
- **AC-E8.** Lịch kiểm tra lại thích ứng: bài không đổi giãn chu kỳ gấp đôi (trần 30 ngày), bài đổi đặt lại chu kỳ ngắn; trang danh sách/seed được kiểm tra mỗi chu kỳ.
- **AC-E9.** Mỗi media có trạng thái OCR tường minh (chờ / xong / lỗi + số lần thử). OCR không ra chữ vẫn tính là xong. Không media nào bị OCR lại vô hạn; chạy lại bất kỳ bước nào cũng không sinh bản ghi hay vector trùng (idempotent).
- **AC-E10.** Lỗi ở một trang không làm hỏng các trang sau trong cùng chu kỳ (không dùng chung trạng thái giao dịch lỗi).

## Nhóm F — Chatbot trả lời (2 mức được, 2 mức cấm)

- **AC-F1.** Được trả lời "ảnh/video của sự kiện X" kèm bằng chứng.
- **AC-F2.** Được trả lời "ai tham dự sự kiện X" dạng danh sách liên quan kèm bằng chứng, không cam kết vị trí trong ảnh.
- **AC-F3.** Cấm trả lời "ai đứng ở vị trí nào trong ảnh".
- **AC-F4.** Cấm trả lời "ai trong ảnh này" với ảnh trẻ em/sinh viên.
- **AC-F5.** Mọi tên người trong câu trả lời phải có bằng chứng; không bằng chứng thì nói không biết.

## Nhóm G — Tìm kiếm kết hợp

- **AC-G1.** Truy vấn kết hợp: embedding chữ 384 chiều (E5) + embedding ảnh 512 chiều (CLIP) + metadata sự kiện, rồi xếp lại. Nhánh chữ→ảnh phải hiểu câu hỏi **tiếng Việt** (bộ mã hoá chữ đa ngôn ngữ cùng không gian với ảnh), không chỉ tiếng Anh.
- **AC-G2.** Chú thích ảnh cũng được tìm bằng chữ (để hỏi "thầy A" ra ảnh).
- **AC-G3.** Có bộ vàng để đo trước khi đổi model/ngưỡng/quy tắc: 50–100 câu hỏi, mỗi câu kèm URL đúng, mức media mong đợi (`none`/`optional`/`required`) và bộ lọc kỳ vọng nếu có; cùng 50–100 media gán nhãn tay. Chỉ số tối thiểu: recall@5, MRR@10 (mức tài liệu), tỉ lệ hiện ảnh sai ở câu `none`, độ phủ ảnh đúng ở câu `required`.

## Nhóm H — Trích nội dung trang (corpus sạch)

- **AC-H1.** Chỉ lấy vùng nội dung chính của trang; logo, menu, sidebar, footer, khối ngày đăng/lượt xem không vào chữ, ảnh hay video của bài.
- **AC-H2.** Ảnh/video nằm ở sidebar hoặc khung chung của site không được gán cho bài viết.
- **AC-H3.** Không nhận ra vùng nội dung chính (ví dụ site đổi giao diện) → ghi cảnh báo + đếm số trang rơi vào trường hợp này, không âm thầm lấy cả trang.
- **AC-H4.** Trang lỗi ứng dụng trả mã 200 (ví dụ lỗi ASP.NET, "Server Error") hoặc gần như rỗng không được index như nội dung; tính là lỗi.
- **AC-H5.** Trang danh sách theo tag (`/vn/tags?...`) không vào corpus.
- **AC-H6.** Media xuất hiện ở hơn N bài (mặc định 20, cấu hình được) coi là trang trí (logo, banner) → không index.
- **AC-H7.** Đoạn chữ lặp nguyên văn ở hơn N bài (boilerplate) không index.

## Nhóm O — Vận hành production (2 VM, 2 vCPU / 4 GB mỗi máy)

- **AC-O1.** Tổng giới hạn RAM các container trên mỗi VM ≤ RAM vật lý trừ 500 MB dự phòng; có swap làm đệm.
- **AC-O2.** Mỗi VM chỉ có một đường triển khai; worker không bao giờ chạy hai vòng crawl song song.
- **AC-O3.** Image build ngoài VM, gắn tag theo commit; rollback = triển khai lại tag trước, không build lại.
- **AC-O4.** Backup dữ liệu hằng ngày (giữ 7–14 ngày) và có lần thử khôi phục thành công gần nhất.
- **AC-O5.** Cổng cơ sở dữ liệu/cache không mở ra Internet; dịch vụ có mật khẩu/khoá; gọi LLM qua kênh mã hoá.
- **AC-O6.** Chu kỳ incremental lỗi hoặc không chạy quá 7 giờ → có cảnh báo tới người vận hành.

## Crawl incremental (sửa 2026-10-18)

- **AC-I1** Mỗi lượt tải lại trang 1 của mọi mục danh sách đã biết (seed, mục của các bài trong DB, trang danh sách từng gặp), không phụ thuộc `frontier:seen`; bài mới ở mục không có seed vẫn được phát hiện.
- **AC-I2** Trang danh sách còn bài lạ của chính mục đó → tải tiếp trang 2, 3… tới `crawl.listing_max_pages`; gặp trang không còn bài lạ thì dừng. Link mục khác ở menu / cột bên được đưa vào hàng đợi nhưng không kéo phân trang.
- **AC-I3** URL tải lỗi tạm thời (timeout, 5xx, thân rỗng, lỗi khi xử lý) không bị mất: được thử lại ở các lượt sau, tối đa `crawl.max_fetch_retries` lần; 404/410 là dứt điểm, không thử lại.
