# Router Phase 2–4: cấu hình và gate

Nhánh này giữ `LLM_ROUTER_MODE=rules` mặc định. Không bật hybrid hoặc thử lại trên
production khi chưa hoàn thành các gate trong `codex/PLAN.md`; S3/VM vẫn đóng băng.

- `LLM_ROUTER_MODE=shadow` hoặc `LLM_ROUTER_SHADOW=1`: tuyến luật như cũ; router LLM
  chạy nền, giới hạn 2 worker và 8 lượt đang chờ, không làm thay đổi response. Khi
  hàng đợi đầy hoặc LLM rewrite đã bật, bỏ lượt shadow để tránh thêm lượt LLM. Log `shadow_route` chỉ có hash của câu hỏi và cờ ảnh,
  kết quả luật và LLM; không ghi câu hỏi. Có thể đo bằng
  `uv run python scripts/eval_router_shadow.py --shadow-log LOG --set SET`.
  Báo cáo giữ cả các dòng không có sự kiện và nêu `total`, `matched`,
  `missing_events`; không suy ra gate từ một tập chỉ gồm các dòng khớp log.
- `LLM_ROUTER_MODEL`: model riêng cho router; rỗng dùng `LLM_MODEL`.
  `LLM_ROUTER_TIMEOUT` mặc định 3 giây, là hạn tổng của request, kể cả khi server
  gửi từng phần liên tục. Lỗi, timeout hoặc schema sai → dùng luật.
- `LLM_ROUTER_MODE=hybrid` là đường thử nghiệm, dùng StateGraph và cache quyết định
  LLM 24 giờ theo provider, model, câu đã chuẩn hóa và cờ ảnh; kết quả graph có cache 10 phút riêng theo cấu hình và index. Gọi LLM cho câu không có
  yêu cầu media `required`, bao gồm optional theo chủ đề và câu liệt kê. Truy xuất hybrid không đọc/ghi cache
  kết quả luật; lỗi graph → dự phòng bằng luật. Không gọi shadow thứ hai khi hybrid.
- `LLM_ROUTER_REROUTE=1` chỉ có tác dụng với hybrid và là thử nghiệm Phase 4: nếu
  cần file nhưng không có file đủ bằng chứng, thử thêm một lượt chỉ tìm attachment,
  tôn trọng filter caller. Nếu file thử lại không đủ bằng chứng, giữ nguồn ban đầu
  và báo thiếu tài liệu. Mặc định tắt; chưa có bằng chứng đạt ngưỡng ≥3% của plan.

`recursion_limit=12` là giới hạn bước graph, không phải số lần truy xuất; state giới
hạn vòng thử lại một lần. `recursion_limit=2` trong plan không đủ cho tuyến ba node.

Đo thử 19 hàng multimodal (18 câu khác nhau) với `qwen3.5-flash-sale`, timeout 3 s, cache query tắt:
18/18 request shadow không có kết quả LLM hợp lệ do timeout. Một lượt chẩn đoán timeout 30 s trả
JSON hợp lệ sau 6,15 s. Chưa có số agreement để qua gate Phase 2; không thể tuyên bố
hybrid hoặc Phase 4 đủ điều kiện bật. Không thay model hay ngưỡng theo một mẫu nhỏ.

Kiểm tra `rules` trên API dev sau refactor (cùng index v3, cache tắt): các counts,
MRR, media precision và tiêu chí từng câu của multimodal_v1, media_v1, golden_v1 và
golden_v2 bằng Phase 1; list_v2 coverage 0,916 và đủ 37/42 cũng giữ nguyên. Dữ liệu
chi tiết nằm ngoài repo tại `/DATA/tmp/sict-phase1-eval-2026-11-08/`.

Giới hạn còn mở: hybrid dùng StateGraph điều phối cache, route, retrieve, validate,
answer và citations; `/chat` chuyển các sự kiện từ `graph.stream()` thành SSE, còn
`rules` giữ tuyến cũ. Chưa chạy đủ năm bộ và RAGAS ở hybrid thật vì model router
chưa qua gate; chưa chứng minh ≥3% câu cần re-route hoặc chạy A/B Phase 4. Không coi
Phase 3/4 đã nghiệm thu hoặc bật production.

Kiểm tra cục bộ sau review: image `Dockerfile.api` build thành công; Python 3.11.18
import được graph. API hybrid dùng LLM giả lập đã trả `/query` 200 với route
`optional`; trường hợp OOS trả không nguồn, `sufficient=false`, và `/chat` SSE có
lời từ chối, citations và `[DONE]`. Kiểm tra container hybrid đang hoạt động chiếm
265,1 MiB (đo một lần; không phải benchmark tải). Đây là kiểm thử với LLM giả lập,
không thay thế eval chất lượng model thật.

Acceptance trên API dev: tuyến rules và hybrid dùng LLM giả lập đều đạt 52/54;
U6 q15 trượt như baseline Phase 1, `/metrics` báo down vì Redis cố ý tắt trong lượt
đo. Đây không phải đánh giá chất lượng câu trả lời của LLM thật.

Sau review cache: khóa kết quả graph còn bao gồm ngân sách context, ngưỡng evidence,
cấu hình retrieval/router/graph và model nhúng, để đổi cấu hình không dùng lại context
cũ. Trên Redis cục bộ riêng và LLM giả lập: hai response giống nhau, lượt đầu 5,867 s,
lượt sau 0,005 s (một phép thử, không phải p50/p90). Các acceptance rules/hybrid mock
đều 52/54 với cùng hai điểm mở nêu trên; chưa phải gate hybrid thật.
Luồng `/chat` hybrid đã kiểm tra lại với LLM giả lập phát hai mẩu SSE: API phát đủ
hai delta theo thứ tự, citations và `[DONE]`; trường hợp OOS không gọi LLM trả lời.

Test cuối sau sửa review và bổ sung `PydanticOutputParser`: `uv run --locked pytest
 tests/ -q --ignore=tests/smoke_incremental.py -p no:cacheprovider` → 421 passed,
5 skipped; `uv lock --check` đạt. Các cảnh báo FastAPI/asyncpg và Qdrant local vẫn
xuất hiện trong log. Ba script test safety/captions/agent đạt.

Lượt đo tiếp theo theo chỉ thị owner: `qwen3.5-flash-sale`, timeout 30 s, thử
`LLM_MAX_NEW_TOKENS=8192` cục bộ, không đổi default. Trước đó một lượt 2048 token
trả HTTP 200, finish reason `length`, content rỗng; một lượt 8192 token hoàn tất
JSON sau 8,64 s. Đây là chẩn đoán từng lượt, không phải benchmark độ trễ.

Đã dừng sớm lượt 8192 sau 46 request vì không thể đạt gate bảo toàn 99% với kết quả
đã quan sát: 63/459 dòng có event, 396 chưa có; trường file có 6 dòng luật đúng
nhưng LLM sai trên tối đa 409 dòng có nhãn (trần 98,53%). Tính theo câu hỏi riêng
biệt, OOS có 3 câu luật đúng nhưng LLM sai trên tối đa 292 câu có nhãn (trần
98,97%). Mẫu được ghép theo hash câu hỏi và cờ ảnh; log kết quả chi tiết ở
`/DATA/tmp/sict_phase234_shadow_30_8192_final.json`, ngoài repo. Trong 63 dòng
khớp, 4 không có quyết định LLM; không suy ra tỷ lệ availability hoàn chỉnh vì đã
dừng sớm. Một số bất đồng OOS là câu hỏi về học phí, chuẩn đầu ra và nhân sự của
trường. Gate sửa ≥80% câu luật sai, latency p50/p90, A/B hybrid và RAGAS chưa được
đánh giá đầy đủ; Phase 3/4 không được coi là đã nghiệm thu.

Script báo cáo đã được test với trường hợp thiếu event. Full pytest sau thay đổi:
422 passed, 5 skipped; `uv lock --check` đạt.

## Điều tra trên golden_v2 (2026-11-08)

Theo yêu cầu owner, chỉ đo `golden_v2` (88 câu); `golden_v1.jsonl` đã được gỡ,
chuyển default các script, test và bước verify sang v2. Bộ v2 thiếu trường `intent`
của v1 nên đánh giá offline nhóm theo `case` khi không có `intent`. Test rules giữ
trường hợp `g062` chưa nhận diện required là tồn tại từ trước. Bộ dữ liệu chuyên
biệt khác được giữ lại. Kết quả trước đó gộp nhiều bộ không còn dùng làm kết luận
cho yêu cầu điều tra này.

Model qwen3.5-flash-sale, timeout 30 s, tối đa 8192 token, cache tắt: prompt gốc có
event cho 88/88 câu, 86 quyết định hợp lệ và 2 không khả dụng (g062/g088; log cũ
không ghi đủ metadata để xác định nguyên nhân từng lượt). Tính các câu luật đúng:
media giữ 77/79 quyết định hợp lệ, file 77/83 và OOS 74/78. Các trường hợp luật
sai được LLM sửa lần lượt 7/8, 3/3 và 2/5. OOS không có nhãn cho 3 câu không đủ
căn cứ; không coi là đúng hay sai. Log hash và kết quả chi tiết nằm ngoài repo tại
`/DATA/tmp/sict_shadow_golden_v2_results.json`.

Thử nghiệm đối chứng giữ model, nhiệt độ, giới hạn token, timeout và tiền xử lý;
chỉ thêm một câu vào prompt nói phạm vi gồm thông tin liên quan HaUI và thiếu dữ
liệu không đồng nghĩa ngoài phạm vi. Cả 88 phản hồi hợp lệ. Tỷ lệ giữ trên số câu
luật đúng: media 78/80, file 80/85, OOS 79/80; cả ba vẫn dưới 99%. Trên 83 cặp
OOS có nhãn và quyết định hợp lệ ở cả hai lượt, 5 câu cải thiện, 0 câu xấu đi;
OOS sửa được 4/5 câu luật sai. g012 vẫn bị gán nhầm OOS, g077 vẫn không được phát
hiện OOS. Phép đo một lượt mỗi prompt gợi ý hướng dẫn phạm vi ảnh hưởng kết quả,
không tách hoàn toàn biến động giữa các lần gọi model; chưa áp dụng prompt thử.
Chi tiết so sánh ở `/DATA/tmp/sict_scope_analysis.json`.

Đánh giá mô phỏng policy hybrid hiện tại, dùng cùng các quyết định shadow: lớp
luật bảo vệ các bất đồng file/media trên bộ này, còn ba câu OOS g015/g022/g072 có
thể thành hồi quy với prompt gốc; prompt thử không còn ba hồi quy đó. Đây là mô
phỏng chính sách, không phải eval API hybrid hoặc RAGAS. Một số câu trong phạm vi
không nhắc SICT/HaUI, và nhãn phụ thuộc nội dung corpus mà router không được xem;
đó là giới hạn đầu vào cần kiểm chứng thêm, không đủ bằng chứng coi một nguyên
nhân duy nhất là chắc chắn. Với prompt thử, thời gian gọi model trực tiếp p50
7,011 s, p90 11,868 s, 88 lượt; không phải phần tăng latency p50 API.

Kiểm tra sau chuyển default: 423 passed, 5 skipped; 3 script test và `bash -n`
đạt. API dev rules chạy default golden_v2: 78 câu có URL, recall@5 0,910,
MRR@10 0,725, media mode 0,909, media đúng bài 7/8. Đó là kiểm tra cục bộ, không
phải A/B hybrid. Không bật production hay thay prompt mặc định.

## Sửa middleware và đo API thật (theo chỉ thị tiếp theo của owner)

Đã áp dụng câu hướng dẫn phạm vi HaUI đã thử nghiệm vào `route_intent`, giữ cơ chế
gộp bảo vệ media/file và fallback khi confidence thấp. Chính sách `scope-v2` ghi
log `middleware_route` gồm hash, đề xuất LLM, quyết định cuối, nguồn quyết định và
thời gian; không ghi câu hỏi. Cache route dùng `rr:v2`; cả cache route và graph
bao gồm phiên bản chính sách và ngân sách token, tránh lấy quyết định prompt cũ.
Hybrid vẫn chạy route trước bước rewrite/truy xuất; tuyến rules mặc định được giữ.

Đo qua `/query` thật trên API cục bộ, cùng v3 index và top_k=8, cache tắt: đủ 88/88
response, đủ 88 event; 24 câu dùng luật, 64 câu gọi router qwen3.5-flash-sale, tất
cả quyết định hợp lệ. Timeout 30 s/8192 token chỉ dùng cho môi trường đo. Quyết
định cuối bảo toàn media 80/80, file 85/85, OOS 80/80 câu luật đúng: đạt phần bảo
toàn ≥99% trên bộ này. Lỗi luật được sửa: media 1/8, file 0/3, OOS 4/5. Ba câu
không có nhãn OOS tiếp tục không được tính. Gate sửa ≥80% chưa đạt cho media/file.

Đầu ra LLM riêng trên 64 câu đủ điều kiện được gọi (không coi 24 câu bỏ qua là
model đúng): bảo toàn media 63/63, file 56/62, OOS 56/56. Gate raw LLM vẫn chưa
đạt ở file; không thay thế hay tuyên bố hoàn thành gate shadow trong PLAN.

Đối chứng rules mới cùng thời điểm: cả hai có URL đúng top5 70/78 và MRR 0,7297;
các nguồn văn bản ở câu answerable không thay đổi. g062 từ không ảnh sang 8 ảnh,
8/8 thuộc bài đúng. Bốn câu OOS được phát hiện là g075/g076/g078/g079; g077 còn
bỏ sót. g081 (case no_answer) bị gán OOS, không chấm đúng/sai OOS vì thiếu nhãn;
phải báo riêng, không suy từ answerable=false. Chưa chạy RAGAS sinh câu trả lời.

Độ trễ `/query`: rules p50 0,6135 s, hybrid p50 6,2025 s; trung vị chênh lệch
theo từng cặp câu 5,47 s. Chưa đạt mục tiêu tăng p50 ≤1,5 s; đây là hai lượt trên
API cục bộ, không phải benchmark tải production. Kết quả chi tiết ngoài repo:
`/DATA/tmp/sict_middleware_analysis.json`, `sict_middleware_api_results.jsonl` và
`sict_middleware_rules_baseline.jsonl`. Test RED→GREEN cho hướng dẫn prompt và
audit middleware, full pytest 425 passed/5 skipped, 3 script CI và lock check đạt.
Các container thử đã dọn; chưa commit hoặc bật production.

## Tiếp tục sau PR #51 — hiệu chỉnh gate và kiểm tra Phase 4

Nhánh làm việc mới từ develop `ea2b704`: `feat/router-gate-calibration`. Nhánh
`feat/router-phases-234` đã xóa local/remote theo yêu cầu owner; main không đổi.
PLAN trong `/codex` đã được owner cho phép điều chỉnh: chỉ golden_v2, gate bảo
  toàn đo final middleware, raw LLM báo riêng, lỗi không được gọi hoặc bị chính
sách bảo vệ phải công khai. Không coi gate model thô cũ là đã pass.

Bổ sung báo cáo có thể chạy lại bằng script trong repo:

```bash
uv run python scripts/eval_router_shadow.py --middleware-log /path/api.log \
  --set eval/golden_v2.jsonl --out /path/report.json
```

`summary` là final; `raw_summary` chỉ gồm các câu đã gọi/dùng cache LLM;
`blocked_llm_fixes` nêu LLM sửa đúng nhưng policy giữ sai, `unconsulted_rule_misses`
nêu lỗi luật thuộc nhóm không gọi LLM. Trên log đủ 88 câu: file g005/g071 bị policy
chặn, file g052 không gọi; media g007/g017/g018/g032/g033/g036/g057 không gọi.
Điều này giải thích vì sao không thể đạt sửa 80% TOÀN BỘ lỗi khi vẫn giữ nguyên
chính sách và nhóm ambiguous hiện tại. Lỗi không được phép sửa tiếp tục là backlog.

Pilot 12 câu golden_v2, mỗi câu 2 chế độ, đảo thứ tự, cùng model/8192 token và
rate limit: mặc định p50 6,753 s / median 2914 completion token; thêm
`enable_thinking=false` p50 6,9235 s / 3111 token. Cả 24 phản hồi hợp lệ, không có
bằng chứng giảm latency; không thêm tùy chọn này vào serving. Đây là mẫu chẩn đoán,
không phải benchmark toàn bộ bộ vàng. Tài liệu upstream có mô tả tham số này:
https://www.alibabacloud.com/help/en/model-studio/deep-thinking
không suy ra endpoint proxy đang dùng bắt buộc phải tiếp nhận nó.

A/B Phase 4 trên đủ 88 câu mỗi lượt, route cache được seed bằng 64 quyết định đã
đo trước đó, cùng model/cấu hình/caller filters; không phát sinh router LLM mới.
Graph cache riêng theo reroute flag, mỗi câu được gọi một lần sau khi xóa cache
kết quả thử nghiệm riêng. Bật retry có 3 lượt thử lại (g048/g050/g071), không tìm
thêm file phù hợp; cả ba giữ nguồn ban đầu và báo thiếu tài liệu. URL top5 70/78,
MRR 0,7297, file hit 4/9 và sufficient đúng 82/88 ở cả hai lượt. p50 0,569 →
0,5985 s (+5,18%). Có retry ở 3/88 câu không đồng nghĩa sửa được lỗi route ở 3/88:
chưa có câu nào cải thiện, nên Phase 4 **no-go** theo điều kiện ≥3% sửa được bằng
validator; giữ cờ retry tắt. Dữ liệu ngoài repo `/DATA/tmp/sict_reroute_analysis.json`.

Thêm `LLM_ROUTER_MAX_NEW_TOKENS` để tách budget router khỏi model trả lời; 0 (default)
kế thừa `llm.max_new_tokens`, không đổi request mặc định. Đo router 8192 token
không còn cần tăng budget model trả lời 2048 token. Cache dùng budget router hiệu
lực; phép đo cache warm không thay cho mục tiêu latency cache lạnh.

Acceptance thật sau khi tách budget (API hybrid, retry tắt, gpt-oss-120b-sale
trả lời): 59/60. Cả 5 lượt chat/SSE và citations đạt, metrics đạt; U1 g004 thiếu
URL đích. Lượt rules đối chứng cùng index cũng thiếu URL này, không có hồi quy
middleware đã quan sát. Đây là kiểm tra hợp đồng/luồng, chưa thay thế RAGAS về
faithfulness. Full pytest 429 passed/5 skipped; ba script CI, lock và diff check đạt.
Code mới trên `feat/router-gate-calibration` chưa commit/push. Đã dọn toàn bộ
container/cache thử nghiệm; API live giữ nguyên. Cold latency và RAGAS hybrid là
các mục còn mở; không coi các phase đã nghiệm thu production.

## Sửa ranh giới ý định media/file theo chỉ thị owner

Nhánh `fix/router-media-file-intent` từ develop `ea2b704`. Nguyên nhân ở middleware
trước rewrite: `optional` do tên sự kiện và mọi câu liệt kê bị loại khỏi nhóm gọi
LLM; còn phép OR `wants_file` giữ cả tín hiệu tên văn bản nhập nhằng. Vì thế 7/8
lỗi media không được xét và 3 false positive file không thể sửa (hai đề xuất đúng
bị chặn, một câu bị loại từ đầu).

Sửa theo lớp lỗi, không theo ID hay tên sự kiện: hybrid gọi LLM cho optional/list,
chỉ fast-path yêu cầu media required. Prompt phân biệt dữ kiện/số lượng/ngày/địa
điểm/danh sách với yêu cầu minh họa và với tra cứu tài liệu theo tên. Policy giữ
required và ảnh đầu vào; cho phép đề xuất hợp lệ sửa file ở câu hỏi thông tin về
văn bản. Giữ yêu cầu lấy/tải rõ ràng và truy vấn tên tài liệu; không cho model tự
nâng câu hỏi thông tin thành file khi không có luật tài liệu hoặc động từ truy cập.
Phân biệt dấu trong tải/tại; lỗi/schema sai/confidence thấp vẫn fallback luật.

`rr:v3` và `intent-v3` cô lập route/graph cache khỏi quyết định cũ; cache query luật
không đổi vì hybrid đã bỏ qua nó. Mode rules mặc định, model qwen3.5-flash-sale,
threshold bằng chứng, index và cờ retry không đổi. Thay đổi này không đồng nghĩa
file đã tồn tại trong index hoặc đã tìm được file đúng; file hit phải đo riêng.

Review độc lập phát hiện thiếu động từ tải giữa câu và nhầm tại/file trong câu
factual; đã tái hiện 6 ca RED rồi sửa GREEN. Thêm regression cho LLM tự nâng lịch
thành file (RED→GREEN). Full suite bản cuối: 452 passed, 5 skipped, 5 warnings sẵn
có; ba script test CI, uv lock --check, diff --check đạt. Đang đo gate API bản cuối.

Lượt API cuối trên commit `e0cf976`: đủ88 response/event, 81 LLM hợp lệ +7 rules,
không timeout/fallback. Prompt bổ sung phạm vi tuyển dụng/thực tập/hợp tác doanh
nghiệp sau lượt trung gian từng từ chối nhầm g012; bản cuối không còn hồi quy này.
Media đúng87/88 (sửa7/8 lỗi luật, giữ80/80 câu luật đúng); file đúng88/88 (sửa3/3,
giữ85/85); OOS đúng85/85 có nhãn (sửa5/5, giữ80/80), ba câu thiếu nhãn OOS không
được tính. Gate sửa≥80% và bảo toàn≥99% đạt cho quyết định cuối trên bộ này;
**không** suy ra media tổng thể≥99%:87/88=98,86%. g062 ảnh không dấu vẫn none.
Raw model riêng trên81 câu: file giữ77/78 câu luật đúng, chưa đạt99%; một proposal
nâng file sai bị chặn. Không coi các câu không gọi model là model đúng.

Đối chứng rules develop cùng index/top_k8, cache tắt: URL top5 đều70/78; MRR
0,7297009→0,7382479, g005 từ hạng3→1, không có câu tụt hạng. File hit vẫn4/9,
không giải quyết thiếu attachment bằng thay nhãn ý định. Media đúng nguồn theo
expected_urls45/47 so với60/68 (ít ảnh không được yêu cầu hơn); sufficient đúng
79/88→82/88. Latency API cold p50 0,631→9,3215s; p90 1,29→15,003s: **chưa đạt**
latency gate, model giữ nguyên, production vẫn rules. Hai lượt trung gian không
thay cho kết quả cuối; dữ liệu/đối chiếu ở /DATA/tmp/sict_intent_*. Chỉ chạy golden_v2.

RAGAS faithfulness đang chạy phép đối chứng có kiểm soát: cùng câu hỏi/context/
sufficient/OOS dùng chung mẫu trả lời và điểm; cặp đổi context sinh/chấm riêng.
Nguồn lấy từ response API thật, sinh bằng gpt-oss-120b-sale qua cùng stream_answer
và citations.enforce, chấm deepseek-v4-flash-sale. Đây không phải hai lượt /chat
độc lập, không đo nhiễu sinh câu trả lời; câu answerable=false không chấm RAGAS.
