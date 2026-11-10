# Plan — Router ý định 4 phase: middleware → shadow LLM → hybrid → LangGraph (kèm HLD)

## Điều chỉnh theo chỉ thị owner 2026-11-08

Plan nhỏ điều tra/sửa Phase 3: [PHASE3_FIX_PLAN.md](PHASE3_FIX_PLAN.md).
Bằng chứng mới: g048 mất nguồn tại cả tạo ứng viên và chọn top-k; g057 tái hiện
trên cả hai ngữ cảnh; prompt router thử nghiệm bị loại vì không sửa lỗi và tăng
input token. Gateway báo 429 khi thử 2 request đồng thời trên key hiện tại.

Cập nhật 2026-11-09: owner chấp nhận độ trễ LLM để bật hybrid ở local/develop;
API local dùng code develop6f68c5e tại http://127.0.0.1:18011/, Redis DB14 riêng.
Gate latency vẫn chưa đạt về số đo; quyết định này cho phép dùng local, không
nghiệm thu các gate còn lại hay cho phép chạy VM/CD/S3. Xem STATUS §20.

Bổ sung theo chỉ thị owner (lượt sửa media/file): được sửa policy và merge develop
trên nhánh mới `fix/router-media-file-intent`. `required` và yêu cầu truy cập tài liệu
rõ ràng vẫn giữ; `optional` theo chủ đề và câu liệt kê được gọi LLM. Câu hỏi thông
tin về văn bản được phép hạ wants_file sau đề xuất hợp lệ/confidence ≥0,6. Không
thay nhãn, model, mặc định rules hay bật production. Đo lại đủ 88 câu golden_v2;
báo cả hồi quy, fallback, latency. Đây là sửa ranh giới guardrail đã được owner
cho phép qua yêu cầu điều tra → sửa theo đề xuất → PR vào develop.

Trạng thái mới nhất: PR52 đã merge develop6f68c5e; media87/88, file88/88,
OOS85/85 có nhãn;
452 passed/5 skip, CI xanh. 09/11: RAGAS controlled80/80 cặp đủ điểm,
faithfulness0,8993→0,8977 (g057 giảm); gate không tụt chưa đạt. g062 còn sai;
file hit trực tiếp4/9, khớp URL hoặc trang cha7/9 (cần duyệt độ phù hợp file);
cold latency hybrid p50 6,733s so với rules0,620s chưa đạt gate.
Code Phase2/3 đã có, giữ mode rules mặc định;
acceptance hybrid thật 59/60 (g004 trượt cả baseline), 429 test pass/5 skip.
Phase 4 A/B 88+88: retry không cải thiện file hit hoặc MRR → no-go.
Cold latency vẫn vượt mục tiêu; RAGAS controlled đủ cặp nhưng giảm điểm,
chưa nghiệm thu hybrid và chưa bật production; local đã bật theo owner.

Phần này cập nhật cách nghiệm thu cho code đã merge PR #51 vào develop
(`ea2b704`), ưu tiên khi khác các bước/định lượng lịch sử phía dưới.

- Chỉ đo bộ `eval/golden_v2.jsonl` (88 câu) theo yêu cầu mới nhất. Đã gỡ
  `golden_v1.jsonl`; giữ nguyên các bộ chuyên biệt, không chạy chúng trong lượt này.
- Giữ `qwen3.5-flash-sale`. Đo cục bộ được dùng timeout 30 s/8192 token theo chỉ
  thị owner (`LLM_ROUTER_MAX_NEW_TOKENS`, không tăng budget trả lời); default timeout 3 s và mode rules không tự đổi. Thử tắt thinking chưa
  cho thấy giảm latency; không áp dụng tùy chọn khi chưa có bằng chứng.
- Gate bảo toàn ≥99% đo trên quyết định cuối middleware trước rewrite, gồm guardrail
  luật, fallback và cache; đồng thời báo riêng raw LLM trên đúng các câu đã gọi.
  Không tính câu không gọi LLM là model đúng, không bỏ event thiếu/timeout.
- Gate sửa ≥80% áp dụng cho lỗi nằm trong phạm vi middleware được phép sửa: thực sự
  được gọi LLM và không bị chính sách bảo vệ buộc giữ nguyên. Luôn kèm tỷ lệ sửa
  trên TOÀN BỘ lỗi luật; nhóm không được gọi và nhóm LLM sửa đúng nhưng guardrail
  chặn phải có danh sách riêng. Nhóm không có mẫu ghi N/A, không ghi pass. Đây là
  thay đổi phạm vi gate; không tuyên bố model thô đã đạt gate cũ.
- Chỉ thị hiện tại vẫn giữ luật required và wants_file không bị LLM hạ. Ba false
  positive file và bảy lỗi optional trong lần đo hiện tại chưa được sửa; việc
  nới các guardrail hoặc mở rộng nhóm ambiguous cần thiết kế/đo riêng, không âm
  thầm đổi để lấy điểm. Không tự đổi nhãn golden theo dự đoán của model.
- Mục tiêu latency p50 tăng ≤1,5 s vẫn giữ; số đo hiện tại chưa đạt. Đo riêng cache
  lạnh và route cache hit, p50/p90, quota ≤1 call/request. A/B Phase 4 dùng cùng
  quyết định router cache để tách ảnh hưởng của retry khỏi LLM.
- Go Phase 4 vẫn cần ≥3% của 88 câu có lỗi route thực sự sửa được nhờ validator;
  thiếu file trong kết quả chưa đủ chứng minh sai route. A/B retry phải không làm
  tụt chất lượng và latency p50 không tăng quá 20%. Chưa qua gate thì giữ retry tắt.
- `recursion_limit=12` giới hạn bước graph; state chỉ cho retry tối đa một lần.
  Sufficient giữ luật D27, concept graph không dùng LLM.
- PR #51 đã merge develop, nhánh cũ đã xóa theo yêu cầu. Main/CD/VM/S3 chưa được
  thực hiện; bật hybrid production chỉ sau mốc thời gian và quyết định riêng của owner.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Ngày: 2026-11-07. Đè lên plan fingerprint 2026-11-06 (đã làm xong toàn bộ, PR #48 chờ merge —
bản gốc lưu tại `docs/superpowers/plans/2026-11-06-fingerprint-provenance.md`).

Trạng thái nền (đã đo, `eval/RESULTS.md` + `eval/results/multimodal_v1_2026-11-07.json`):
- Nhánh `feat/media-evidence-gate` (worktree `agent-a1dfc62220e497f8d`, commit d336b78) đã sửa
  lỗi m001 ở tầng truy xuất: `_doc_media` lấy ảnh/khung hình của chính các bài chữ đứng đầu,
  `gate` xếp theo hạng bài, `supported_media` giữ media gắn khi `sufficient` False.
  Trên media_v1: media_precision 0.674 → 0.798, none_clean 33/33, video_hit 12/15,
  golden_v1/v2 + person_v1 + list_v2 không đổi.
- Bộ 150 câu `eval/multimodal_v1.jsonl` chưa chạy trên nhánh đó → số failures thật sau A+C+D+E
  chưa biết. Baseline develop (2026-11-07): mode_ok 123/150, media_precision 0.561,
  wants_file 139/150, video_hit 9/17.

---

## HLD — Kiến trúc đích (chủ duyệt 2026-11-07: LangChain/LangGraph component là phần serving)

**Nguyên tắc phân tầng** — luật luôn chạy, LLM chỉ đề xuất, validator quyết định cuối;
điều phối bằng **StateGraph của LangGraph** (không HTTP middleware — hàm thuần test stdlib):

```
câu hỏi ──► [M-trước] ──► [Router] ──► [Truy xuất 4 tier parallel] ──► [M-sau/Validator] ──► [Trả lời]
             restore dấu     luật (~0 ms)   dense/BM25/phrase/graph    gom 1 điểm validate()
             cache q:v12     + LLM chỉ khi  + _doc_media (A)          OOS abstain
                             ambiguous      + gate theo bằng chứng    media đúng bài
                             + policy_merge                           wants_file thật
                                                                      citations pre-check
```

- **M-trước** (node đầu StateGraph, hàm thuần): normalize (accent restore, rewrite luật
  viết tắt), cache check, OOS pre-check (chỉ khi LLM shadow đo được — Phase 2+).
- **Router hybrid**: `classify_rules` mặc định; câu ambiguous mới gọi LLM router —
  output ép qua **Pydantic schema `RouteDecision`** (component LangChain-core: structured
  output thay parse JSON thủ công) → `router.decide` gộp có guardrail: không hạ
  `required`/`wants_file` luật đã bắt rõ; `oos=true` → bỏ nhánh media.
- **M-sau** = node `validate` (hàm thuần trong agent.py, được graph gọi) thay cho các hậu
  kiểm rải rác (`supported_media`, media_note, tương lai: OOS abstain, file thật) — điểm neo
  duy nhất test được bằng stdlib.
- **LangGraph là điều phối chính từ Phase 3**, không còn là Phase 4 "chỉ khi go": chủ dự án
  quyết 2026-11-07 dùng component LangChain/LangGraph thay middleware/luật rải rác để sàng
  lọc trước–sau từng bước. Đo thực tế (dev box): langgraph 1.2.17 + langchain-core 1.7.0
  đã có sẵn trong uv.lock (qua langchain-community của nhóm eval) — thêm vào nhóm api
  không tải bản mới; disk ~19 MB; import +3.4 s 1 lần khi khởi động; RAM API 84 → 122 MB
  (+38 MB, limit VM1 1.5 GB — vừa); chu kỳ validate→re-route chạy đúng qua
  `StateGraph` + `add_conditional_edges` + `recursion_limit`.
- **Quota LLM**: kiraai 20 calls/phút chia sẻ answering; hybrid ≤ 1 call/request, cache route
  theo hash câu hỏi, model flash (qwen3.5-flash-sale, không đụng model answering gpt-oss-120b).

**Sơ đồ node LangGraph (dựng ở Phase 3; Phase 1–2 viết node = hàm thuần trước):**

```
normalize ─► route ─► retrieve_text ─┬─► retrieve_media ─► evidence ─► validate ─┬─► answer ─► citations ─► END
   │           │                                                  ▲           │
   │ cache hit─┼──────────────────────────────────────────────────┼───────────┘ END sớm
   │           └─ ambiguous? ─► llm_route ─► policy_merge ─────────┘
   └─ validate fail + re-route được? ─► route lại (recursion_limit=2)
```

**Phạm vi 4 phase:**

| Phase | Việc | Trước/sau 10/11 | Gate |
|---|---|---|---|
| 0 | Chạy 150 multimodal_v1 trên nhánh A+C+D+E → danh sách failures thật | nay (~12 phút) | có số, chưa sửa |
| 1 | Consolidation: `validate()` 1 điểm; OOS; I2; wants_file (node = hàm thuần) | **trước 10/11** | 5 bộ + RAGAS không tụt |
| 2 | Shadow-mode LLM router (log-only, `RouteDecision` pydantic) | trước, nếu còn thời gian | agreement đo được |
| 3 | Hybrid router + dựng StateGraph điều phối; bật prod sau 10/11 | code trước, bật sau | quota + latency p90 + accept_api SSE |
| 4 | Chu kỳ validate→re-route + validator file thật (mở rộng graph) | sau 10/11 | eval chứng minh ≥ 3% câu cần re-route |

---

## Global Constraints

- Không pip; mọi lệnh qua `uv run`. Dependency serving mới duy nhất được phép:
  `langgraph>=1.2` + `langchain-core>=1.7` vào **nhóm `api`** (chủ duyệt 2026-11-07;
  đã đo: +38 MB RAM API, disk ~19 MB, import +3.4 s — trong limit VM1 1.5 GB).
  Không thêm langchain full / langchain-community vào nhóm api (chỉ langgraph + core).
- Route LLM ép qua Pydantic `RouteDecision` (pydantic>=2.7 đã có) — không parse JSON thủ công.
- Không dùng FastAPI HTTP middleware cho validation — validation là node/hàm thuần trong
  `agent.py` / graph, test stdlib như cả repo.
- Không đụng S3, không chạy gì trên VM prod ngoài deploy do chủ quyết (S3/VM rule).
- Luồng phát hành: nhánh → PR vào `develop`; develop→main là bước cuối trước go-live.
- Code comment / commit tiếng Việt. Mọi fix theo fix-policy: đo lại toàn bộ
  (multimodal_v1 + media_v1 + golden_v1/v2 + list_v2; RAGAS khi câu trả lời đổi), báo cả câu tụt.
- Cache prefix `q:v12` phải bump (→ `q:v13`) khi route/thứ tự kết quả đổi giữa các phase.
- API dev đo eval: container `sict-live-api` (EMBED_BACKEND=api, `*_v3`, cache Redis db 3) —
  khi đo A/B phải tắt cache (REDIS_URL trỏ cổng đóng) hoặc xoá `q:v12:*` giữa 2 lần chạy.
- Deadline: PR fix Phase 1 phải vào `develop` trước 01:00 ngày 2026-11-10.

## Review Focus

1. **Câu có ảnh gửi kèm + chữ chứa "ảnh"** (I2): kỳ vọng — "ảnh này" là đầu vào, không phải
   đòi xem ảnh → `media_mode` `optional`, `inputs` có `image`. Test: pin trong Task 1.
2. **Câu OOS đòi ảnh/video** (t5-10/11/12): kỳ vọng — không có media nào, trả lời
   "không có thông tin" thay vì 8 ảnh ngẫu nhiên. Test: pin trong Task 3.
3. **Âm tính giả wants_file** ("quy định chuẩn đầu ra" — câu fact không đòi file):
   kỳ vọng — mở rộng `_FILE` không bắt "quy định" trần. Test: pin trong Task 2.
4. **LLM router hỏng** (timeout 3 s / 429 / JSON lỗi): kỳ vọng — route luật dùng như trước,
   response không đổi, không thêm latency đo được. Test: pin trong Task 6 (`route_intent`
   trả None mọi đường lỗi) + Task 7 (hook bắt exception).
5. **Route LLM mâu thuẫn luật rõ ràng** ("xem ảnh" explicit nhưng LLM trả none):
   kỳ vọng — luật thắng, `required` giữ nguyên. Test: pin trong Task 8 (`policy_merge`).
6. **Shadow log không đụng response**: kỳ vọng — bật shadow không đổi 1 byte response
   (trừ log); tắt = 0 call LLM. Test: pin trong Task 7.
7. **Hybrid mode hỏng tại runtime** (Redis cache route chết, model flash gỡ khỏi gateway):
   kỳ vọng — rơi về route luật, request không lỗi. Test: pin trong Task 8.
8. **Graph lỗi giữa chừng** (node retrieve exception, recursion_limit chạm trần): kỳ vọng —
   trả kết quả dự phòng của Phase 1 (`agent.run` tuyến tính) hoặc lỗi 500 có log, không
   treo request. Test: pin trong Task 8.
9. **Pydantic `RouteDecision` từ chối JSON lạ** (thiếu field, sai Literal, confidence ngoài
   0–1): kỳ vọng — ValidationError bắt thành None → fallback luật, không crash. Test:
   pin trong Task 6.

---
## PHASE 0 — Đo lại 150 câu trên nhánh A+C+D+E (không sửa code)

**Mục đích:** fix-policy yêu cầu số thật trước khi sửa; baseline develop cũ không phản ánh
nhánh đã sửa. Kết quả = danh sách failures thật Phase 1 phải xử lý.
**Files:** không đổi code; chỉ chạy + ghi kết quả vào `eval/RESULTS.md`.

- [ ] **Step 0.1: Dựng API nhánh worktree** (cache tắt = Redis trỏ cổng đóng 6399):

```bash
docker run -d --name sict-meg-api --network host \
  -v /DATA/craw-sict/.claude/worktrees/agent-a1dfc62220e497f8d/sict_crawler:/app/sict_crawler \
  -v /DATA/craw-sict/config.yaml:/app/config.yaml \
  -v /DATA/craw-sict/_local_store:/app/_local_store \
  -e EMBED_BACKEND=api -e EMBED_API_BASE=http://localhost:8090/v1 \
  -e EMBED_API_KEY="$(grep EMBED_API_KEY /DATA/craw-sict/.env | cut -d= -f2)" \
  -e EMBED_DIM=1024 -e EMBED_MODEL=jina-clip-v2 -e EMBED_QUERY_IMAGE_SIZE=336 \
  -e EMBED_CACHE_PATH=/tmp/meg-embed-cache.sqlite \
  -e QDRANT_COLLECTION_TEXT=sict_text_v3 -e QDRANT_COLLECTION_IMAGE=sict_image_v3 \
  -e QDRANT_COLLECTION_OCR=sict_ocr_v3 -e REDIS_URL=redis://127.0.0.1:6399/3 \
  sict-api:latest
```

- [ ] **Step 0.2: Chạy bộ 150** — `uv run python scripts/eval_media.py --api
  http://localhost:8003 --set eval/multimodal_v1.jsonl
  --out eval/results/multimodal_v1_branch_2026-11-07.json` (~12 phút, từng câu 4–9 s).
- [ ] **Step 0.3: Chạy 4 bộ kiểm chứng** media_v1, golden_v1, golden_v2, list_v2 qua cùng API;
  so với bảng nhánh trong `eval/RESULTS.md` (media_precision 0.798, none_clean 33/33).
- [ ] **Step 0.4: Ghi RESULTS.md** — phân cụm failures: I2 (~8+4 dòng mode_ok), wants_file
  (~8 dòng t4 + t5-09), OOS (t5-10/11/12), còn lại (m017/m027 xếp hạng, m023 cosine,
  video_hit, false_abstain). Cắt container (`docker rm -f sict-meg-api`).
- [ ] **Step 0.5: Đối chiếu với Phase 1** — cụm nào biến mất thì bỏ khỏi Task tương ứng.

---

## PHASE 1 — Consolidation: node thuần trước, graph nối sau (trước 01:00 ngày 10/11)

**Điểm mới so với bàn đầu (chủ duyệt 2026-11-07):** các node (validate, OOS, I2,
wants_file) viết dưới dạng **hàm thuần test stdlib** — không FastAPI middleware; node
nào xong sẽ được StateGraph nối ở Phase 3. Trước deadline 10/11 không có langgraph trong
serving; nhánh này vẫn chạy đường `agent.run` tuyến tính hiện có (an toàn, rollback dễ),
graph là Phase 3.

**Nhánh:** `feat/router-phase1` từ `develop` (sau khi PR #48 merge; nếu chưa merge thì từ
`feat/index-fingerprint`). Nhánh `feat/media-evidence-gate` (worktree, commit d336b78) phải
merge vào `develop` TRƯỚC khi bắt đầu — phần lớn sửa nằm ở đó.

Expectations từ baseline develop khi đo trên nhánh (kiểm chứng lại ở Phase 0):
wants_file misses = mm-t4-01/05/06/07/08/09/11/12/14 + mm-t5-09; I2 mode_ok trượt = mm-i2
(8/12) + mm-i1-07/08/15/16; OOS t5-10 (8 ảnh lạc), t5-11 (8 video lạc), t5-12 (suất True, n_media 8).

### Task 1: I2 — "ảnh này" là đầu vào, không phải đòi xem ảnh

**Files:**
- Modify: `sict_crawler/service/retrieval/router.py:80-92` (`_ANH_ACCENT`, `classify_rules`,
  `decide`)
- Modify: `sict_crawler/service/retrieval/retriever.py:308-311` (truyền cờ có ảnh)
- Test: `tests/test_router.py` (thêm case)

**Interfaces:**
- Consumes: `router.Route` (hiện có, trường `inputs`).
- Produces: `classify_rules(question: str, has_image: bool = False) -> Route` —
  khi `has_image=True`, chữ "ảnh này/ảnh" KHÔNG nâng `media` lên `required` (câu nói về ảnh
  mình gửi, không xin ảnh); chỉ `_REQUIRED` (cụm đòi xem: "cho xem", "xem ảnh", "album"…)
  mới nâng; `route.inputs` có `"image"`.

- [ ] **Step 1: Test fail** — thêm vào tests/test_router.py:

```python
def test_image_input_not_media_request():
    r = router.classify_rules("Sự kiện trong ảnh này trao giải gì?", has_image=True)
    assert r.media == "optional"
    assert "image" in r.inputs
    assert router.classify_rules("ảnh lễ tốt nghiệp").media == "required"  # hành vi cũ
    r2 = router.classify_rules("cho xem thêm ảnh lễ tốt nghiệp", has_image=True)
    assert r2.media == "required"  # đòi XEM dù có ảnh kèm → vẫn required
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_router.py -k image_input -v`; Expected: FAIL
  (hiện `_ANH_ACCENT` bắt "ảnh này" → required).
- [ ] **Step 3: Implement** — `classify_rules` thêm `has_image: bool = False`; thêm
  `_INPUT_CONTEXT = re.compile(r"ảnh này|ảnh trên|trong ảnh|của ảnh|ảnh đính kèm|photo|picture")`
  (cụm quy chiếu ảnh đầu vào); `_ANH_ACCENT` không nâng khi `has_image and _INPUT_CONTEXT`.
  `router.decide` thêm `has_image` và chuyền vào classify; `retriever.py` (~dòng 309)
  truyền `has_image=bool(query_image_b64)`.
- [ ] **Step 4: Pass + suite** — `uv run pytest tests/test_router.py
  tests/test_multimodal_query.py -v` rồi full suite. Expected: PASS, không test cũ tụt.
- [ ] **Step 5: Commit** — `feat(router): ảnh gửi kèm là đầu vào, không tự nâng required (I2)`

### Task 2: wants_file — chỉ chặn âm tính giả "quy định" trần (thu hẹp sau Phase 0)

**Files:**
- Modify: `sict_crawler/service/retrieval/router.py:46-50` (`_FILE`)
- Test: `tests/test_router.py`

**Ruling Phase 0 (2026-11-07):** 9 câu t4 trượt ở baseline cũ **tự hết** sau #49 — KHÔNG mở
rộng mẫu từ (`đơn đăng ký|đơn xin|phiếu đăng ký|hướng dẫn sử dụng|tải ở đâu|…`). Chỉ còn t5-09
"quy dinh chuan dau ra ngoai ngu" (wf=True do "quy dinh" trần) là false positive cần chặn.
Giữ nguyên các mẫu `_FILE` hiện có; "quy định/quy dinh" chỉ nhận khi kèm
`file|biểu mẫu|download|đính kèm` trong 40 ký tự, hoặc đứng sau "tải" ở đầu câu.

**Chặn kỹ thuật (2026-11-07, đã thử):** bỏ `\bquy dinh\b` trần làm
`test_rules_on_golden_set` fail q18/q19/q20 — golden_v1 để `wants_file=True` (intent=file,
expected `/quy-che`). Luật thuần không phân biệt được "quy định … cho sinh viên" (golden)
với "quy dinh … cua truong" (t5-09). Đề xuất: **giữ nguyên `_FILE`** (golden thắng), chấp nhận
t5-09 là miss — sau #49 wants_file vẫn 147/150 (gate ≥147); hoặc chủ cập nhật nhãn golden
q18-q20 rồi mới siết luật. Không dùng mẹo dấu/không dấu.

- [ ] **Step 1: Test fail** — thêm vào tests/test_router.py:

```python
def test_wants_file_quy_dinh_needs_document_context():
    assert not R.classify_rules("quy định chuẩn đầu ra ngoại ngữ của trường").wants_file
    for q in ["tải quy định chuẩn đầu ra ngoại ngữ", "quy định kèm file biểu mẫu",
              "quy chế tuyển sinh", "tải về biểu mẫu đánh giá"]:
        assert R.classify_rules(q).wants_file, q
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_router.py -k quy_dinh -v`. Expected: FAIL.
- [ ] **Step 3: Implement** — bỏ `\bquy dinh\b` khỏi nhánh trần; thêm nhánh có ngữ cảnh tài liệu
  (trước/sau trong 40 ký tự) + `^\s*tai\b.{0,40}\bquy dinh\b`. Không thêm mẫu từ mới cho t4.
- [ ] **Step 4: Pass + đo** — `uv run pytest tests/test_router.py -v`; các câu wf của media_v1
  qua API nhánh (baseline 96/101 không tụt).
- [ ] **Step 5: Commit** — `fix(router): "quy định" trần không còn bật wants_file (t5-09)`

### Task 3: OOS — KHÔNG làm trong Phase 1 (thu hẹp sau Phase 0)

**Ruling Phase 0 (2026-11-07):** t5-10/11 **tự hết** nhờ luật C của #49 (0 media, abstain
True). Chỉ còn t5-12 "kết quả giải bóng đá vòng 30" (8 media, sufficient True — bài có thật
trong corpus); đây là OOS "có bằng chứng truy xuất", cần LLM `oos` ở Phase 2/3, **không phải
luật**. Phase 1 không sửa `agent.py` cho OOS; giữ regression test hiện có của t5-10/11 (nếu
chưa có thì thêm test khoá hành vi "required + thiếu bằng chứng → media rỗng + note" — chỉ test,
không sửa code). t5-12 chuyển sang Phase 2/3 (Task 6/8: `RouteDecision.oos` + `policy_merge`).

### Task 4: `agent.validate()` — gom hậu kiểm thành 1 điểm

**Files:**
- Modify: `sict_crawler/service/retrieval/agent.py` (trích `validate()`, `run` gọi nó)
- Test: `tests/test_agent.py`

**Interfaces:** `validate(question: str, tsrcs, msrcs, ctx, ev: dict, route) ->
tuple[list, list, str, Route]` trả (tsrcs, msrcs, media_note, route) — chứa luật C
(`supported_media`), note OOS Task 3, trích outputs khi media rỗng. Điểm neo duy nhất
Phase 3+ ghép thêm validator (file thật, citations).

- [ ] **Step 1: Test fail** — 4 trường hợp: (a) sufficient True → giữ nguyên; (b) False +
  media gắn → giữ media gắn; (c) False + required + rỗng sau C → note OOS + bỏ output;
  (d) False + optional → gắn hoặc rỗng, không note OOS.
- [ ] **Step 2: Run** — `uv run pytest tests/test_agent.py -k validate -v`. Expected: FAIL.
- [ ] **Step 3: Implement** — di chuyển khối luật C từ `run` vào `validate` (**không đổi
  logic** so với Task 3; chỉ trích hàm).
- [ ] **Step 4: Pass + full suite**; RAGAS chỉ khi context `/chat` đổi: chạy collect+score
  golden_v2 trên API nhánh, so faithfulness với lần đo gần nhất.
- [ ] **Step 5: Commit** — `refactor(agent): gom hậu kiểm vào validate() một điểm`

### Task 5: Gate toàn bộ Phase 1 + ghi RESULTS.md

**Files:** không đổi code; chỉ chạy + ghi.

- [ ] **Step 5.1:** `uv run pytest tests/ -q --ignore=tests/smoke_incremental.py` PASS.
- [ ] **Step 5.2:** 5 bộ trên API nhánh Phase 1 (cache tắt): multimodal_v1 + media_v1 +
  golden_v1 + golden_v2 + list_v2. Gate: multimodal mode_ok ≥ 135/150 (tăng từ ~123),
  wants_file ≥ 147/150, none_clean ≥ 42/51, mọi bộ khác bằng/hơn baseline nhánh.
- [ ] **Step 5.3:** `scripts/accept_api.py --api http://localhost:8003` — giữ số nhánh hiện hành.
- [ ] **Step 5.4:** Ghi `eval/RESULTS.md` bảng số + câu tốt lên/tụt kèm lý do; bump cache
  `q:v12` → `q:v13` trong `retriever.query_cache_key` (route đổi).
- [ ] **Step 5.5:** PR vào `develop`.

---

## PHASE 2 — Shadow-mode LLM router (trước 10/11 NẾU còn thời gian)

**Nguyên tắc:** log-only, bất đồng bộ, timeout 3 s, không đụng response. Đo agreement luật
vs LLM từng field trên 4 bộ → quyết định có bật hybrid không. Tắt = 0 call LLM
(`llm.router_shadow: bool = False`; env `LLM_ROUTER_SHADOW`).

### Task 6: `vlm.route_intent` — 1 call, Pydantic `RouteDecision`, model flash riêng

**Files:**
- Create: `sict_crawler/service/retrieval/route_schema.py` — `RouteDecision` (Pydantic,
  component langchain-core pattern: schema ép LLM trả đúng shape)
- Modify: `sict_crawler/service/retrieval/vlm.py` (thêm `route_intent`)
- Modify: `sict_crawler/core/config.py` (`LlmConfig`: `router_model: str = ""`
  — env `LLM_ROUTER_MODEL`, mặc định rỗng = dùng `llm.model`; `router_timeout: int = 3`
  — env `LLM_ROUTER_TIMEOUT`; `router_shadow: bool = False`)
- Modify: `pyproject.toml` — nhóm `api` thêm `"langgraph>=1.2,<2"`, `"langchain-core>=1.7,<2"`
  (chủ duyệt 2026-11-07; đo trước: +38 MB RAM, disk ~19 MB, import +3.4 s)
- Test: `tests/test_router_shadow.py` (mới, stdlib unittest + mock `_text_call`)

**Interfaces:**
- `route_schema.RouteDecision(BaseModel)`:
  `media: Literal["none","optional","required"]`; `wants_file: bool`;
  `oos: bool`; `confidence: float = Field(ge=0, le=1)`.
  `RouteDecision.from_llm(text: str) -> RouteDecision | None` — trích JSON từ text
  (tái dùng `parse_route_json` hiện có) rồi `model_validate`; ValidationError/miếu khớp
  → `None` (Review Focus 9).
- `vlm.route_intent(question: str, has_image: bool) -> dict | None` — trả dict 4 key
  của `RouteDecision` hoặc `None` (timeout/lỗi/schema chối → router dùng luật như cũ).
  Prompt: vai trò định tuyến SICT, 4–6 dòng quy tắc (required = muốn XEM ảnh/video;
  optional = sự kiện có thể minh hoạ; none = fact; wants_file = muốn văn bản/biểu mẫu;
  oos = ngoài phạm vi SICT; đặc biệt: câu nói về ảnh gửi kèm → media optional trừ khi
  đòi xem thêm); yêu cầu trả DUY NHẤT JSON. Dùng `_text_call(router_model or model)` —
  model flash qwen3.5-flash-sale (không đụng model answering gpt-oss-120b). Chú ý: truyền
  câu đã chuẩn hóa (accent restore + viết tắt mở rộng) vào prompt, không phải câu thô.
- Review Focus 4 (lỗi/timeout): `route_intent` bắt mọi exception, timeout 3 s qua
  `_text_call` (thêm tham số `timeout` nếu chưa có).

- [ ] **Step 1: Test fail** — `tests/test_router_shadow.py`: (a) mock JSON hợp lệ → dict đúng
  4 key; (b) mock text rác/JSON hỏng → None; (c) mock raise Timeout → None;
  (d) `RouteDecision` chối JSON thiếu field / confidence 1.5 → from_llm trả None;
  (e) pyproject nhóm api chứa langgraph + langchain-core (`tests/test_pyproject.py` —
  đọc pyproject.toml assert 2 chuỗi trong nhóm api).
- [ ] **Step 2: Run** — `uv run pytest tests/test_router_shadow.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** — `route_schema.py` (chỉ pydantic + Literal + Field);
  `uv add --group api "langgraph>=1.2,<2" "langchain-core>=1.7,<2"` (uv.lock đã có cả hai
  bản 1.2.17/1.7.0 — không tải mới); `route_intent` trong `vlm.py`; config 3 trường mới +
  env override theo quy ước `LlmConfig` (`LLM_*`).
- [ ] **Step 4: Pass + suite + đo image API** — `docker build` image api mới, chạy container
  test đo RSS idle (kỳ vọng ≤ 600 MB limit hiện tại; so với sict-live-api).
- [ ] **Step 5: Commit** —
  `feat(retrieval): RouteDecision pydantic + route_intent LLM model flash (Phase 2)`

### Task 7: Shadow hook trong `agent.run` + script đo agreement

**Files:**
- Modify: `sict_crawler/service/retrieval/agent.py` (gọi shadow sau route luật)
- Modify: `scripts/eval_media.py` (thêm cột log shadow: `--shadow` đọc từ log API)
- Test: `tests/test_router_shadow.py` (bổ sung)

**Interfaces:** khi `router_shadow=True`: `run` gọi `route_intent` trong `try/except`,
  ghi `log.append(f"shadow: {llm_route}")`, KHÔNG đổi route/response (Review Focus 6 —
  test so sánh response bật/tắt shadow bằng fixture, bằng nhau byte-for-byte trừ log).
  Eval: `eval_media.ask()` đọc thêm log shadow → cột `shadow_agree` (media/wants_file/oos
  khớp luật hay không).

- [ ] **Step 1: Test fail** — (a) shadow bật, LLM mock trả trái luật → response giữ route luật;
  (b) shadow bật, mock raise → response giữ nguyên, không exception;
  (c) cùng fixture bật/tắt → response bằng nhau (trừ `log`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** — hook trong `run` (sau `retriever.retrieve` đã có route luật):
  gọi intent duy nhất khi `has_text`; timeout tổng ≤ 3 s; response không đổi.
- [ ] **Step 4: Pass + đo agreement** — bật shadow với `LLM_ROUTER_MODEL=qwen3.5-flash-sale`,
  chạy 5 bộ (~200 calls, riêng rẽ với answering, rpm ≤ 15 như RAGAS): bảng agreement từng
  field; mục tiêu vào hybrid: LLM đúng ở ≥ 80% câu luật trượt mà không sai ở
  ≥ 99% câu luật đúng.
- [ ] **Step 5: Ghi RESULTS.md + commit** —
  `feat(agent): shadow-mode LLM router log-only (Phase 2)`

---

## PHASE 3 — Hybrid router + StateGraph điều phối (code viết trước; SAU 10/11 mới bật prod)

**Điều kiện vào (gate từ Phase 2):** agreement đạt mục tiêu; latency p50 câu ambiguous
+≤ 1.5 s đo qua `/query` cache tắt; quota: ≤ 1 call/request, cache route theo hash
(fold(question) + has_image, TTL 24 h, Redis db 3, prefix `rr:`).

### Task 8: Hybrid + policy_merge guardrail + dựng StateGraph

**Files:**
- Modify: `sict_crawler/service/retrieval/router.py` (`decide` mở rộng, `policy_merge`)
- Modify: `sict_crawler/service/retrieval/agent.py` (`run` → wrapper gọi graph; giữ nguyên
  chữ ký + format dict trả về — Phase 1 đã viết node, giờ nối StateGraph)
- Create: `sict_crawler/service/retrieval/graph.py` (StateDefinition + edges)
- Modify: `sict_crawler/core/config.py` (`llm.router_mode: str = "rules"`
  — `rules|shadow|hybrid`; env `LLM_ROUTER_MODE`)
- Test: `tests/test_router.py`, `tests/test_retrieval_graph.py` (mới — node thuần mock
  retriever, không cần dịch vụ; test recursion_limit=2; test cache-hit END sớm)

**Interfaces:**
- `policy_merge(rules: Route, llm: dict) -> Route` — guardrail deterministic:
  (1) luật `required` (từ khoá đòi xem rõ) không bị LLM hạ — giữ như `decide` hiện có;
  (2) luật `wants_file` không bị LLM hạ; (3) `llm.oos=True` → `route.oos=True`
  (trường mới trên `Route`, mặc định False) → node retrieve_media bỏ + `validate`
  buộc `sufficient=False`; (4) câu ambiguous mới gọi LLM: định nghĩa = luật `none`
  + không match `_EVENT`/`_LIST` + không phải câu chỉ-ảnh (chính là các câu luật đang
  trượt ở Phase 0/1); (5) confidence < 0.6 → bỏ kết quả LLM. Route cache theo hash câu hỏi.
- `graph.py`: StateDefinition TypedDict (question, restored, image_b64, route, rewrite
  lists, text_hits, media, sources, ev, llm_calls_used, note); nodes = hàm Phase 1
  (normalize, route, retrieve_text, retrieve_media, evidence, validate, answer, citations);
  edges đúng sơ đồ HLD; chu kỳ duy nhất validate→route (`add_conditional_edges`):
  validate fail + nguyên nhân re-route được (oos→none, file→wants_file mới phát hiện);
  `recursion_limit=2`. Lỗi node (Review Focus 8): graph nâng exception → `run` bắt, trả
  dict dự phòng của đường tuyến tính hoặc lỗi 500 có log — không treo request.
- Không node LLM judge — `sufficient` vẫn là luật (D27). `/chat` SSE stream qua
  `graph.stream()` (accept_api chạy lại toàn bộ).

- [ ] **Step 1: Test fail (policy_merge)** — 5 rule trên thành 5 assert; thêm test cache
  route (gọi 2 lần → `_text_call` mock chỉ được gọi 1 lần).
- [ ] **Step 2: Test fail (graph)** — nối StateGraph với node mock (smoke đã kiểm chứng
  kiểu này chạy đúng); assert: (a) đường vui chạy END qua answer; (b) validate fail →
  re-route đúng 1 lần → attempts=2 rồi END; (c) cache hit → bỏ qua retrieve, END sớm;
  (d) node raise → run trả dự phòng, không treo.
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement** — `policy_merge` + `Route.oos` + graph.py nối node thật
  (normalize/route/retrieve/evidence/validate đã có từ Phase 1–2) + `run` thành wrapper.
- [ ] **Step 5: Pass + đo** — 5 bộ trên API nhánh hybrid (cache truy vấn tắt, cache route
  bật): gate như Phase 1 (mode_ok ≥ 135/150, wants_file ≥ 147/150, không tụt bộ khác);
  latency p50/p90 câu none+không-list so với rules-only (+≤ 1.5 s p50); accept_api
  (SSE `/chat` qua graph.stream) giữ số nhánh.
- [ ] **Step 6: Commit** — `feat(retrieval): hybrid luật+LLM + StateGraph điều phối (Phase 3)`

### Task 9: Bật hybrid trên prod (SAU 01:00 10/11, theo deploy của chủ)

- [ ] **Step 9.1:** PR vào `develop`; develop→main; CD (`LLM_ROUTER_MODE=hybrid` trong
  `.env` VM1 — chỉ khi chủ duyệt).
- [ ] **Step 9.2:** Theo dõi 24 h: `sict_http_requests_total` + log shadow (giữ bật) —
  tỉ lệ lệch luật tăng bất thường → rollback env về `rules` (không cần deploy lại image).

---

## PHASE 4 — Mở rộng graph: chu kỳ re-route thông minh + validator file thật (sau 10/11)

**Go khi:** eval Phase 3 chứng minh cần chu kỳ `validate → route lại → truy xuất lại`
(≥ 3% câu của 5 bộ fail vì route sai mà có route đúng rõ ràng từ validator — ví dụ
wants_file phát hiện sau khi thấy nguồn là attachment); KHÔNG mở rộng khi các node tuyến
tính đã đủ.

### Task 10 (khi go): validator file thật + luật re-route từ bằng chứng

**Files:**
- Modify: `sict_crawler/service/retrieval/agent.py` (`validate` — thêm validator file)
- Modify: `sict_crawler/service/retrieval/graph.py` (điều kiện re-route theo nguyên nhân)
- Test: `tests/test_retrieval_graph.py` (bổ sung)

**Interfaces:**
- Validator file (giữ đề xuất brainstorm cũ): `wants_file=true` mà context không có nguồn
  `kind=file` → `media_note` báo thiếu tài liệu (không giả vờ thành công).
- Chu kỳ re-route theo nguyên nhân: validate fail vì "oos" → route lại `none`;
  fail vì "file" → route lại `wants_file`; fail khác → END (không lặp mù).
  `recursion_limit=2` giữ nguyên.

- [ ] **Step 1–4:** TDD từng luật re-route (test fail → implement → pass → commit).
- [ ] **Step 5:** A/B graph mở rộng vs Phase 3 trên 5 bộ (cùng index, cache tắt) —
  gate: số đo bằng hoặc hơn, latency p50 không tăng quá 20%.

---

## Ranh giới (cả 4 phase)

- KHÔNG đổi luật đủ bằng chứng `sufficient` thành LLM judge (D27) — LLM chỉ route + shadow.
- Dependency serving mới duy nhất: langgraph + langchain-core vào nhóm api (đã duyệt,
  đã đo RAM +38 MB); KHÔNG langchain full/community vào nhóm api.
- KHÔNG dùng FastAPI HTTP middleware cho validation (node/hàm thuần).
- KHÔNG bật hybrid trên prod trước 01:00 10/11; shadow chỉ chạy eval + log.
- KHÔNG đổi `sufficient` rule, `gate` ngưỡng, `_doc_media` — ngoài phạm vi plan này
  (m017/m027/m023 là lỗi xếp hạng chữ, xử lý bằng plan riêng nếu chủ duyệt).
- Mọi fix phải đo lại 5 bộ + RAGAS khi câu trả lời đổi (fix-policy); báo cả câu tụt.
- Không đụng S3/VM ngoài deploy chủ quyết; code comment/commit tiếng Việt.
