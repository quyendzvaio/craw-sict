"""Fingerprint index (core/fingerprint.py): ổn định, không phụ thuộc thứ tự key, đủ thành phần quyết định index."""
from sict_crawler.core import fingerprint
from sict_crawler.core.config import get_settings
from sict_crawler.service.graph import concepts
from sict_crawler.service.index import indexing


def test_stable_across_calls():
    assert fingerprint.index_fingerprint() == fingerprint.index_fingerprint()


def test_components_match_index_rules():
    c = fingerprint._components()
    cfg = get_settings()
    assert c["spec"] == indexing.SPEC_VERSION
    assert c["graph_rules"] == concepts.RULES_VERSION
    assert c["embedding"]["backend"] == cfg.embedding.backend
    assert c["embedding"]["model"] == cfg.embedding.model
    assert c["collections"]["text"] == cfg.qdrant.collection_text
    assert c["collections"]["image"] == cfg.qdrant.collection_image
    assert c["collections"]["ocr"] == cfg.qdrant.collection_ocr


def test_fingerprint_order_independent():
    c = fingerprint._components()
    reversed_c = {k: c[k] for k in reversed(list(c))}
    assert fingerprint._fingerprint(c) == fingerprint._fingerprint(reversed_c)


def test_version_dict_shape():
    v = fingerprint.index_version()
    assert set(v) == {"fingerprint", "collections", "spec", "graph_rules", "embedding"}
    assert set(v["collections"]) == {"text", "image", "ocr"}
    assert v["embedding"]["dim"] == get_settings().embedding.dim
    assert v["fingerprint"] == fingerprint.index_fingerprint()
