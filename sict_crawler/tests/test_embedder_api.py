"""Backend api của embedder (D22): định dạng yêu cầu, tiền tố query, thứ tự, cache, retry, ảnh hỏng. Không mạng."""
import base64
import io

import httpx
from PIL import Image

from sict_crawler import embedder
from sict_crawler.config import get_settings


class _Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._b, self.headers, self.text = code, body or {}, headers or {}, str(body)

    def json(self):
        return self._b


def _png(w=64, h=48, color=(200, 30, 30)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format="PNG")
    return b.getvalue()


def _setup(tmp_path, monkeypatch, handler):
    e = get_settings().embedding
    saved = {k: getattr(e, k) for k in ("backend", "api_base", "api_key", "dim", "cache_path", "api_retries")}
    e.backend, e.api_base, e.api_key, e.dim, e.api_retries = "api", "http://embed.test/v1", "k", 4, 3
    e.cache_path = str(tmp_path / "cache.sqlite")
    embedder._cache_db = None
    embedder.USAGE.update(requests=0, tokens=0, image_tokens=0, cache_hits=0)
    monkeypatch.setattr(httpx, "post", handler)
    monkeypatch.setattr(embedder.time, "sleep", lambda s: None)
    return e, saved


def _restore(e, saved):
    for k, v in saved.items():
        setattr(e, k, v)
    embedder._cache_db = None


def _echo(calls):
    def post(url, json, headers, timeout):
        calls.append((url, json, headers))
        rows = [{"index": i, "embedding": [float(len(str(x))), float(i), 0.0, 1.0]}
                for i, x in enumerate(json["input"])]
        return _Resp(200, {"data": rows[::-1], "usage": {"total_tokens": 10}})   # đảo thứ tự: phải sắp lại theo index
    return post


def test_text_request_prefix_and_order(tmp_path, monkeypatch):
    calls = []
    e, saved = _setup(tmp_path, monkeypatch, _echo(calls))
    try:
        vecs = embedder.embed_texts(["a", "bb", "ccc"])
        assert [v[1] for v in vecs] == [0.0, 1.0, 2.0]                       # theo thứ tự đầu vào
        url, body, headers = calls[0]
        assert url == "http://embed.test/v1/embeddings" and headers["Authorization"] == "Bearer k"
        assert body["model"] == e.model and body["dimensions"] == 4 and body["normalized"] is True
        assert body["input"] == [{"text": "a"}, {"text": "bb"}, {"text": "ccc"}]   # passage: không tiền tố
        embedder.embed_query("hỏi")
        assert calls[1][1]["input"] == [{"text": e.query_prefix + "hỏi"}]
        assert embedder.embed_text_clip(["x"])[0] == embedder.embed_texts(["x"], kind="query")[0]
    finally:
        _restore(e, saved)


def test_cache_dedup_and_hits(tmp_path, monkeypatch):
    calls = []
    e, saved = _setup(tmp_path, monkeypatch, _echo(calls))
    try:
        embedder.embed_texts(["a", "a", "b"])
        assert len(calls) == 1 and len(calls[0][1]["input"]) == 2            # trùng nội dung chỉ gửi một lần
        embedder.embed_texts(["a", "b"])
        assert len(calls) == 1 and embedder.USAGE["cache_hits"] == 2         # lần sau lấy từ cache
        embedder.embed_texts(["a"], kind="query")                            # tiền tố khác → khoá khác
        assert len(calls) == 2
    finally:
        _restore(e, saved)


def test_images_skip_bad_and_resize(tmp_path, monkeypatch):
    calls = []
    e, saved = _setup(tmp_path, monkeypatch, _echo(calls))
    try:
        big = _png(1600, 800)
        vecs, ok = embedder.embed_image_bytes([_png(), b"<html>not an image</html>", big])
        assert ok == [0, 2] and len(vecs) == 2                               # ảnh hỏng bị bỏ, chỉ số khớp đầu vào
        sent = [Image.open(io.BytesIO(base64.b64decode(x["image"]))) for x in calls[0][1]["input"]]
        assert max(sent[1].size) == embedder.IMAGE_MAX_SIDE and sent[1].size == (512, 256)
        assert embedder.embed_image_bytes([b"junk"]) == ([], [])
        assert embedder.embed_image_base64([base64.b64encode(_png()).decode()])[0]
    finally:
        _restore(e, saved)


def test_retry_then_success_and_hard_failure(tmp_path, monkeypatch):
    seq = [_Resp(429, headers={"retry-after": "1"}), _Resp(503), None]
    def post(url, json, headers, timeout):
        r = seq.pop(0)
        return r if r else _Resp(200, {"data": [{"index": 0, "embedding": [1, 0, 0, 0]}]})
    e, saved = _setup(tmp_path, monkeypatch, post)
    e.api_retries = 5
    try:
        assert embedder.embed_texts(["x"]) == [[1, 0, 0, 0]]
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(400, {"error": "bad"}))
        try:
            embedder.embed_texts(["y"])
            assert False, "lỗi 4xx phải là lỗi cứng"
        except embedder.EmbedError:
            pass
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(429))
        try:
            embedder.embed_texts(["z"])
            assert False, "hết lượt thử phải là lỗi cứng"
        except embedder.EmbedError:
            pass
    finally:
        _restore(e, saved)


def _loader(manifest):
    from sict_crawler import loader
    ld = loader.Loader.__new__(loader.Loader)
    ld.cfg, ld.manifest = get_settings(), manifest
    return ld


def test_loader_check_api_backend(tmp_path):
    e, q = get_settings().embedding, get_settings().qdrant
    saved = (e.backend, q.collection_text, q.collection_image, q.text_dim, q.image_dim)
    good = {"backend": "api", "text_model": e.model, "image_model": e.model, "dim": e.dim,
            "query_prefix": e.query_prefix}
    try:
        e.backend, q.collection_text, q.collection_image, q.text_dim, q.image_dim = \
            "api", "sict_text_v3", "sict_image_v3", e.dim, e.dim
        _loader(good).check()                                                   # khớp → qua
        for bad in ({**good, "dim": 512}, {**good, "text_model": "khac"}, {**good, "query_prefix": "x"}):
            try:
                _loader(bad).check()
                assert False, bad
            except SystemExit:
                pass
        q.image_dim = 512                                                       # số chiều collection lệch
        try:
            _loader(good).check()
            assert False
        except SystemExit:
            pass
        q.image_dim, q.collection_text = e.dim, "sict_text"                    # collection v1 không bị ghi đè
        try:
            _loader(good).check()
            assert False
        except SystemExit:
            pass
        e.backend, q.collection_text = "torch", "sict_text_v3"
        try:
            _loader(good).check()                                               # artifact api nạp bằng cấu hình torch
            assert False
        except SystemExit:
            pass
    finally:
        e.backend, q.collection_text, q.collection_image, q.text_dim, q.image_dim = saved


def test_cached_image_skips_prepare_and_request(tmp_path, monkeypatch):
    calls = []
    e, saved = _setup(tmp_path, monkeypatch, _echo(calls))
    try:
        embedder.embed_image_bytes([_png()])
        monkeypatch.setattr(embedder, "prepare_image", lambda b: (_ for _ in ()).throw(AssertionError("prepare")))
        vecs, ok = embedder.embed_image_bytes([_png()])                          # khoá theo byte gốc: có cache thì khỏi resize
        assert ok == [0] and len(vecs) == 1 and len(calls) == 1
    finally:
        _restore(e, saved)


def test_rerank_text_embeds_only_captions_on_rrf_order(tmp_path, monkeypatch):
    from sict_crawler import query_rewriter
    seen = []
    e = get_settings().embedding
    monkeypatch.setattr(e, "backend", "api")
    monkeypatch.setattr(embedder, "embed_texts", lambda t, kind="passage": seen.append(list(t)) or [[1.0, 0.0] for _ in t])
    hits = [{"title": "A", "content": "x", "score": 0.5}, {"title": "B", "caption": "y", "kind": "media_caption", "score": 0.4}]
    out = query_rewriter.rerank_text(hits, "q", top_n=5, reorder=False, qvec=[1.0, 0.0])
    assert seen == [["B. y"]] and "raw_score" not in out[0] and out[1]["raw_score"] == 1.0   # chữ thường không cần raw_score
    assert [h["title"] for h in out] == ["A", "B"]                                              # giữ thứ tự RRF


def test_config_accepts_owner_env_names(monkeypatch):
    from sict_crawler.config import EmbeddingConfig
    for k in ("EMBED_API_BASE", "EMBED_API_KEY", "EMBED_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BASEURL_EMBEDDING", "https://api.jina.ai/v1/embeddings")
    monkeypatch.setenv("APIKEY_EMBEDDING_MODEL", "k123")
    monkeypatch.setenv("EMBEDDING_MODEL", "https://jina.ai?sui=embeddings&model=jina-clip-v2")
    e = EmbeddingConfig(backend="api")
    assert (e.api_base, e.api_key, e.model) == ("https://api.jina.ai/v1", "k123", "jina-clip-v2")


def test_client_side_tpm_throttle(tmp_path, monkeypatch):
    calls = []
    e, saved = _setup(tmp_path, monkeypatch, _echo(calls))
    slept = []
    clock = [1000.0]
    monkeypatch.setattr(embedder.time, "time", lambda: clock[0])
    monkeypatch.setattr(embedder.time, "sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(e, "api_tpm", 10000)
    embedder._sent.clear()
    try:
        embedder.embed_image_bytes([_png()])              # 4.000
        embedder.embed_image_bytes([_png(color=(1, 2, 3))])  # 8.000
        assert slept == []
        embedder.embed_image_bytes([_png(color=(9, 9, 9))])  # vượt 10.000/phút → chờ hết cửa sổ
        assert slept and sum(slept) >= 59 and len(calls) == 3
    finally:
        embedder._sent.clear()
        _restore(e, saved)


def test_parallel_batches_keep_order_cache_and_throttle(tmp_path, monkeypatch):
    import threading
    import time as _t
    seen, lock = [], threading.Lock()

    def post(url, json, headers, timeout):
        with lock:
            seen.append(threading.current_thread().name)
        _t.sleep(0.05)
        rows = [{"index": i, "embedding": [float(len(x["text"])), 0.0, 0.0, 1.0]} for i, x in enumerate(json["input"])]
        return _Resp(200, {"data": rows[::-1], "usage": {"total_tokens": 10}})
    e, saved = _setup(tmp_path, monkeypatch, post)
    e.api_workers, e.api_batch = 4, 5
    try:
        texts = ["x" * (i + 1) for i in range(40)]                           # 8 lượt gọi
        vecs = embedder.embed_texts(texts)
        assert [v[0] for v in vecs] == [float(i + 1) for i in range(40)]      # thứ tự đầu vào giữ nguyên
        assert len(set(seen)) > 1 and embedder.USAGE["requests"] == 8         # thật sự chạy nhiều luồng, đếm đủ
        n = len(seen)
        assert embedder.embed_texts(texts) == vecs and len(seen) == n         # lần 2 toàn bộ từ cache
    finally:
        e.api_workers = 1
        _restore(e, saved)


def test_timeout_grows_with_images_in_batch(tmp_path, monkeypatch):
    """Lô ảnh trên dịch vụ tự host 2 vCPU (~9 s/ảnh) không được hết timeout rồi gửi lại khi server vẫn đang tính."""
    seen = []

    def post(url, json, headers, timeout):
        seen.append(timeout)
        return _Resp(200, {"data": [{"index": i, "embedding": [1.0, 0.0, 0.0, 0.0]} for i in range(len(json["input"]))]})
    e, saved = _setup(tmp_path, monkeypatch, post)
    try:
        embedder.embed_texts(["một câu"])
        embedder.embed_image_bytes([_png(color=(i, 0, 0)) for i in range(8)])
        assert seen[0] == e.timeout
        assert seen[1] >= 8 * embedder.IMAGE_TIMEOUT_S > e.timeout
    finally:
        _restore(e, saved)
