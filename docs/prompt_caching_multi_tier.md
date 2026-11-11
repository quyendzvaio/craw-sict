# Prompt caching nhiều tầng (multi-tier) — spec thiết kế

**Mục đích:** Giảm độ trễ và số lần gọi LLM cho router + answering, giữ kết quả giống hệt (bit-for-bit) khi cache hit; mặc định bật từ 2026-11-11 (chủ duyệt), tắt bằng `PROMPT_CACHE_ENABLED=0`.

**Nguyên tắc không phá:**
- Default bật/tắt bằng biến môi trường riêng (`PROMPT_CACHE_ENABLED`), env ghi đè default.
- Cache key chứa đầy đủ ngữ cảnh phân biệt (câu hỏi + ảnh + model + max_tokens + index fingerprint + phiên bản policy), tránh phục vụ kết quả sai.
- Không cache câu trả lời sinh cuối cùng (`answer`) nếu owner yêu cầu cân nhắc bản quyền/personal data — chỉ cache *prompt-tùy-token* (token-level / response-template) và *route decision* đã có.
- Không thay LangGraph/StateGraph, không thêm dependency mới (chỉ dùng stdlib + `hashlib`, `json`).

---

## 1. Ba tầng cache

| Tầng | Lưu gì | Tuổi thọ | Key gồm | Khi invalidate |
|---|---|---|---|---|
| **T1: Route** | `RouteDecision` (media/wants_file/oos/confidence) từ luật + LLM shadow | TTL 24h (hoặc theo `ROUTE_CACHE_TTL`) | hash(câu hỏi, ảnh, model router, `ROUTE_POLICY_VERSION`) | Đổi policy version, đổi model router, bump `q:v` |
| **T2: Prompt / câu trả lời sinh** | Câu trả lời LLM (hoặc template token-level) cho `/chat` | TTL 12h | hash(câu hỏi, ảnh, model answer, max_tokens, ngữ cảnh số lượng nguồn, version hệ thống) | Đổi prompt answer, đổi model, đổi ngưỡng sufficient |
| **T3: Embedding query** | Vector truy vấn (đã có qua `embedder` cache sqlite) | Theo dịch vụ | hash(input, model, size) | Không đổi trong spec này |

---

## 2. Thiết kế key (để tránh cache collision)

```
route_key   = "pc:v1:route:"   + sha256(fold(question) + "|" + image_hash + "|" + router_model + "|" + ROUTE_POLICY_VERSION + "|" + query_cache_key)
prompt_key  = "pc:v1:prompt:"  + sha256(fold(question) + "|" + image_hash + "|" + answer_model + "|" + str(max_new_tokens) + "|" + len(context_sources) + "|" + index_fingerprint + "|" + prompt_template_version)
```

`query_cache_key` (`q:v13` sau Task 5) đang chứa index fingerprint + filters + collection; tái sử dụng để không code lại.

---

## 3. Vị trí chèn vào luồng serving (hiện tại)

Luồng hiện tại (dạng hàm thuần, graph là Phase 3):
```
normalize → route (luật + LLM shadow) → retrieve_text → retrieve_media → evidence → validate → answer → citations → END
```

Caching chèn vào:
- **Trước `route`:** tra T1 route cache → nếu hit và hợp lệ → bỏ qua LLM shadow/luật route, dùng route đã cache.
- **Trước `answer`:** tra T2 prompt cache → nếu hit và ngữ cảnh không đổi → dùng câu trả lời cache, bỏ qua gọi LLM answer (chỉstream lại template nếu cần).

Nhờ StateGraph (Phase 3) có thể đóng gói các node `route`/`answer` vào một wrapper cache, không cần sửa từng hàm散stedt.

---

## 4. Cấu hình (thêm vào `LlmConfig`, nhóm `api`, không đụng default production)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `PROMPT_CACHE_ENABLED` | `true` (2026-11-11; ban đầu `false`) | Bật/tắt toàn bộ |
| `PROMPT_CACHE_ROUTE_TTL_S` | `86400` | TTL route cache |
| `PROMPT_CACHE_PROMPT_TTL_S` | `43200` | TTL prompt/answer cache |
| `PROMPT_CACHE_TEMPLATE_VERSION` | `"v1"` | Phiên bản template câu trả lời |
| `PROMPT_CACHE_MAX_ENTRIES` | `10000` | Giới hạn số entry (Redis già có `volatile-ttl`, nên TTL là chính) |

Redis db 3 (cùng với query cache) hoặc db riêng `prompt_cache` — chọn db 3 để tận dụng cơ chế best-effort đã có (`repository/cache.py`).

---

## 5. Hành động khi cache miss / hit

```
route():
    key = route_key(question, image, router_model, policy_version)
    cached = cache.get(key)
    if cached and enabled:
        return RouteDecision.from_json(cached)   # không gọi LLM, không latency LLM
    route = compute_route(...)                   # luật + LLM shadow (hiện tại)
    cache.set(key, route.to_json(), ttl=ROUTE_TTL)
    return route

answer(question, context, image, model, max_tokens):
    key = prompt_key(...)
    cached = cache.get(key)
    if cached and enabled:
        return stream_cached(cached)             # trả lại giống hệt
    answer = llm_stream(...)                     # gọi thực
    cache.set(key, answer, ttl=PROMPT_TTL)
    return answer
```

---

## 6. Gate nghiệm thu (trước khi bật production)

- [ ] Cache hit không đổi câu trả lời (so sánh chuỗi trả lời cache vs không cache trên 88 câu golden_v2).
- [ ] TTL hết hạn → gọi lại LLM, kết quả giống cache cũ (deterministic).
- [ ] Khi đổi prompt answer / model → key mới, không phục vụ cache cũ.
- [ ] Latency p50 khi cache hit giảm đáng kể (mục tiêu: không chậm hơn baseline).
- [ ] Không làm hỏng `test_rules_on_golden_set`, `test_router_shadow`, suite đầy đủ.

---

## 7. File dự kiến tạo / sửa

- `sict_crawler/core/config.py`: thêm 5 biến cấu hình vào `LlmConfig` (mặc định bật từ 2026-11-11 theo chủ duyệt; ban đầu tắt). Kiểm tra trước khi bật: test suite 26/26 (key phân biệt đầy đủ, stream hit/miss đúng, tắt → không chạm Redis).
- `sict_crawler/service/retrieval/prompt_cache.py` (mới): hàm tạo key, wrapper `route_cached` / `answer_cached` (stdlib + Redis cache hiện có).
- `tests/test_prompt_cache.py` (mới): key uniqueness, TTL, invalidate khi đổi policy/model, miss→hit.
- `docs/prompt_caching_multi_tier.md`: spec này (lưu vào docs).

**Lưu ý:** Spec này chỉ thiết kế, chưa code. Code sẽ được triển khai sau khi owner duyệt spec.
