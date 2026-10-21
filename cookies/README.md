# Cookie YouTube cho worker (KHÔNG commit file .txt)

1. Trên trình duyệt đã đăng nhập YouTube: xuất cookies Netscape
   (extension "Get cookies.txt LOCALLY" → Export) được file `cookies.txt`.
2. Vào GitHub repo → Settings → Secrets → Actions → sửa secret
   `COOKIES_TXT`: dán toàn bộ nội dung file vào.
3. Vào Actions → `cookies` → Run workflow → target `vm2`.
   Workflow chép secret thành `~/craw-sict/cookies/cookies.txt` trên VM2
   (quyền 600), cron/worker đọc qua `$YT_COOKIES`, không cần restart
   (mount thư mục nên file mới có hiệu lực ngay).
4. Cookie hết hạn (YouTube lại đòi đăng nhập trong log worker) → lặp lại
   bước 2–3. Cookie không bao giờ vào git history.
