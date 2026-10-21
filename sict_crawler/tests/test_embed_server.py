"""Dịch vụ embedding ONNX int8 (D22 tự host): tiền xử lý ảnh khớp JinaCLIPImageProcessor, chia lượt theo token,
Matryoshka, tokenizer bỏ padding 77, hợp đồng HTTP kiểu Jina — không cần model / onnxruntime."""
import base64
import io

import numpy as np
import pytest
from PIL import Image

from sict_crawler import embed_server as S

CFG = {"size": 512, "interpolation": "bicubic", "mean": [0.48145466, 0.4578275, 0.40821073],
       "std": [0.26862954, 0.26130258, 0.27577711]}


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_preprocess_constant_colour_and_shape():
    a = S.preprocess_image(_png(Image.new("RGB", (900, 400), (255, 0, 128))), CFG)
    assert a.shape == (3, 512, 512) and a.dtype == np.float32
    want = (np.array([255, 0, 128]) / 255.0 - np.array(CFG["mean"])) / np.array(CFG["std"])
    assert np.allclose(a[:, 256, 256], want, atol=1e-2)


def test_preprocess_resizes_shortest_side_then_center_crops():
    """Ảnh ngang 1024×512: cạnh ngắn đã 512 → không resize, cắt giữa 512 (bỏ 256 px mỗi bên)."""
    im = Image.new("RGB", (1024, 512), (0, 0, 0))
    im.paste((255, 255, 255), (256, 0, 768, 512))          # dải trắng đúng vùng giữa
    a = S.preprocess_image(_png(im), CFG)
    white = (1.0 - np.array(CFG["mean"])) / np.array(CFG["std"])
    assert np.allclose(a[:, :, 0], white[:, None], atol=1e-3) and np.allclose(a[:, :, -1], white[:, None], atol=1e-3)
    # ảnh dọc nhỏ (RGBA): phóng cạnh ngắn lên 512, cắt giữa theo chiều cao
    b = S.preprocess_image(_png(Image.new("RGBA", (100, 300), (10, 20, 30, 255))), CFG)
    assert b.shape == (3, 512, 512)


def test_token_batches_budget_and_same_length():
    assert S.token_batches([5, 100, 6, 100], budget=250, same_length=False) == [[0, 2], [1, 3]]
    assert S.token_batches([5, 100, 6, 100], budget=150, same_length=False) == [[0, 2], [1], [3]]
    assert S.token_batches([3000], budget=100, same_length=False) == [[0]]       # quá ngân sách vẫn chạy 1 đoạn
    assert S.token_batches([4, 4, 7, 4], budget=1000, same_length=True) == [[0, 1, 3], [2]]   # không pad


def test_finish_matryoshka_and_norm():
    v = np.arange(1, 1025, dtype=np.float32)
    full, cut = S.finish(v, 1024), S.finish(v, 64)
    assert len(full) == 1024 and len(cut) == 64
    assert abs(np.linalg.norm(cut) - 1) < 1e-5 and np.allclose(np.array(cut), v[:64] / np.linalg.norm(v[:64]))


def test_parse_items():
    assert S.parse_items({"input": "chào"}) == [{"text": "chào"}]
    assert S.parse_items({"input": ["a", {"image": "eA=="}]}) == [{"text": "a"}, {"image": "eA=="}]
    for bad in ({"input": []}, {"input": [{"text": "a", "image": "b"}]}, {"input": [{"x": 1}]}, {}):
        with pytest.raises(ValueError):
            S.parse_items(bad)


def test_load_tokenizer_drops_fixed_77_padding(tmp_path):
    tk = pytest.importorskip("tokenizers")
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    tok = tk.Tokenizer(WordLevel({"<pad>": 1, "a": 5, "[UNK]": 3}, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    tok.enable_padding(pad_id=1, pad_token="<pad>", length=77)    # như tokenizer.json của jina-clip-v2
    tok.enable_truncation(77)
    tok.save(str(tmp_path / "tokenizer.json"))
    t = S.load_tokenizer(tmp_path / "tokenizer.json")
    assert len(t.encode("a a a").ids) == 3
    assert len(t.encode(" ".join(["a"] * 500)).ids) == 500                     # cắt ở TEXT_MAX_LEN, không phải 77


class FakeEncoder:
    """Vector chữ = one-hot theo độ dài chữ, ảnh = one-hot theo byte đầu — kiểm thứ tự trộn chữ/ảnh."""

    def texts(self, texts):
        return [np.eye(1, 1024, len(t), dtype=np.float32)[0] * 3 for t in texts]

    def images(self, blobs):
        return [np.eye(1, 1024, 500 + b[0], dtype=np.float32)[0] for b in blobs]


def test_embed_keeps_input_order():
    items = [{"text": "ab"}, {"image": base64.b64encode(b"\x07").decode()}, {"text": "abcd"}]
    vecs, nt, ni = S.embed(FakeEncoder(), items, 1024)
    assert (nt, ni) == (2, 1)
    assert [int(np.argmax(v)) for v in vecs] == [2, 507, 4] and abs(np.linalg.norm(vecs[0]) - 1) < 1e-6


def test_http_contract_matches_embedder_client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    with TestClient(S.create_app(FakeEncoder())) as c:
        assert c.get("/healthz").json()["ok"] is True
        r = c.post("/v1/embeddings", json={"model": "jina-clip-v2", "normalized": True, "dimensions": 256,
                                           "input": [{"text": "xin chào"}, {"text": "a"}]})
        assert r.status_code == 200
        d = r.json()
        assert [x["index"] for x in d["data"]] == [0, 1] and len(d["data"][0]["embedding"]) == 256
        assert d["usage"]["texts"] == 2
        assert c.post("/v1/embeddings", json={"input": []}).status_code == 400
        assert c.post("/v1/embeddings", json={"input": ["a"], "dimensions": 4096}).status_code == 400
        m = c.get("/metrics").text                                                # D13: Alloy scrape
        assert 'sict_embed_items_total{kind="text"} 2.0' in m and 'sict_embed_errors_total{code="400"} 2.0' in m
        assert 'sict_embed_request_duration_seconds_count{kind="text"} 1.0' in m


def test_quantize_vocab_table_matches_fp32():
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper
    rng = np.random.default_rng(0)
    W = rng.normal(size=(12000, 16)).astype(np.float32)
    g = helper.make_graph([helper.make_node("Gather", ["W", "ids"], ["y"], axis=0)], "t",
                          [helper.make_tensor_value_info("ids", TensorProto.INT64, [1, None])],
                          [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, None, 16])],
                          [numpy_helper.from_array(W, "W")])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    assert S.quantize_vocab_table(m) == 1 and not any(i.name == "W" for i in m.graph.initializer)
    ids = np.array([[0, 5, 11999]], dtype=np.int64)
    y = ort.InferenceSession(m.SerializeToString(), providers=["CPUExecutionProvider"]).run(None, {"ids": ids})[0]
    cos = (y[0] * W[ids[0]]).sum(1) / np.linalg.norm(y[0], axis=1) / np.linalg.norm(W[ids[0]], axis=1)
    assert y.shape == (1, 3, 16) and cos.min() > 0.999


def test_query_waits_neither_for_bulk_nor_user_image():
    """Lô hàng loạt và ảnh người dùng giữ hết slot làn của chúng → câu hỏi chữ vẫn chạy được ngay."""
    lanes = S.Lanes(1)
    with lanes.take(S.BULK), lanes.take(S.USER_IMAGE):
        assert not lanes.take(S.BULK).acquire(blocking=False)   # lô thứ hai phải chờ
        q = lanes.take(S.QUERY)
        assert q.acquire(blocking=False)
        q.release()


def test_lane_of_request():
    assert S.lane_of([{"text": "Trụ sở trường ở đâu?"}]) == S.QUERY
    assert S.lane_of([{"image": "x"}]) == S.USER_IMAGE
    assert S.lane_of([{"text": "a"}, {"text": "b"}]) == S.BULK
    assert S.lane_of([{"image": "x"}, {"image": "y"}]) == S.BULK
    assert S.lane_of([{"text": "x" * S.QUERY_MAX_CHARS}]) == S.BULK


def _toy_vision(n_grid: int = 6, patch: int = 4, dim: int = 4):
    """Graph tối giản mang các hằng số phụ thuộc lưới như tháp ảnh jina (Conv patch, pos_embed, bảng RoPE, shape)."""
    from onnx import TensorProto, helper, numpy_helper
    n = n_grid * n_grid
    cos, sin = S.rope_tables(n_grid, dim, 16)
    rng = np.random.default_rng(0)
    inits = [numpy_helper.from_array(rng.standard_normal((1, n + 1, 8)).astype(np.float32), "clip.vision_model.pos_embed"),
             numpy_helper.from_array(cos.astype(np.float32), "rope_cos"),
             numpy_helper.from_array(sin.astype(np.float32), "rope_sin"),
             numpy_helper.from_array(np.array([-1, 8, n], dtype=np.int64), "shape_patches"),
             numpy_helper.from_array(np.array([-1, n + 1, 2, 4], dtype=np.int64), "shape_tokens"),
             numpy_helper.from_array(rng.standard_normal((8, 3, patch, patch)).astype(np.float32), "w")]
    nodes = [helper.make_node("Conv", ["pixel_values", "w"], ["c"], kernel_shape=[patch, patch], strides=[patch, patch]),
             helper.make_node("Reshape", ["c", "shape_patches"], ["out"])]
    size = n_grid * patch
    g = helper.make_graph(nodes, "toy", [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, ["b", 3, size, size])],
                          [helper.make_tensor_value_info("out", TensorProto.FLOAT, None)], inits)
    return helper.make_model(g)


def test_resize_vision_tower_rewrites_grid_constants():
    pytest.importorskip("onnx")
    from onnx import numpy_helper
    m = _toy_vision()
    pe0 = numpy_helper.to_array(m.graph.initializer[0]).copy()
    assert S.resize_vision_tower(m, 16, 16) == 4                          # 24 px → 16 px, patch 4: lưới 6 → 4
    init = {t.name: numpy_helper.to_array(t) for t in m.graph.initializer}
    assert init["clip.vision_model.pos_embed"].shape == (1, 17, 8)
    assert np.allclose(init["clip.vision_model.pos_embed"][0, 0], pe0[0, 0])   # token CLS giữ nguyên
    cos, sin = S.rope_tables(4, 4, 16)
    assert np.allclose(init["rope_cos"], cos) and np.allclose(init["rope_sin"], sin)
    assert init["shape_patches"].tolist() == [-1, 8, 16] and init["shape_tokens"].tolist() == [-1, 17, 2, 4]
    assert m.graph.input[0].type.tensor_type.shape.dim[2].dim_value == 16


def test_resize_vision_tower_refuses_unknown_rope_table():
    pytest.importorskip("onnx")
    from onnx import numpy_helper
    m = _toy_vision()
    m.graph.initializer[1].CopyFrom(numpy_helper.from_array(np.zeros((36, 8), dtype=np.float32), "rope_cos"))
    with pytest.raises(ValueError, match="RoPE"):
        S.resize_vision_tower(m, 16, 16)


def test_bicubic_matrix_rows_sum_to_one_and_identity_when_same_size():
    w = S.bicubic_matrix(36, 28)
    assert np.allclose(w.sum(1), 1.0)
    assert np.allclose(S.bicubic_matrix(5, 5), np.eye(5))


def test_query_image_size_routes_to_small_tower_and_validates():
    class Sized(FakeEncoder):
        def images(self, blobs, size=None):
            return [np.eye(1, 1024, (size or 0) % 1000, dtype=np.float32)[0] for _ in blobs]
    img = base64.b64encode(b"\x01").decode()
    items = S.parse_items({"input": [{"image": img}, {"image": img, "image_size": S.QUERY_IMAGE_SIZES[0]}]})
    vecs, _, ni = S.embed(Sized(), items, 1024)
    assert ni == 2 and [int(np.argmax(v)) for v in vecs] == [0, S.QUERY_IMAGE_SIZES[0]]
    with pytest.raises(ValueError, match="image_size"):
        S.parse_items({"input": [{"image": img, "image_size": 123}]})
