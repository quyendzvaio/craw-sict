"""Self-check chọn device embedding: auto/cpu/cuda + fallback CPU (không cần torch thật/GPU).

Run: python -m tests.test_device  |  pytest tests/test_device.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.core.config import get_settings
from sict_crawler.service.embedding import embedder

_MISSING = object()


def _resolve(device: str, cuda: bool | None) -> str:
    """cuda=True/False → torch giả báo có/không GPU; None → không import được torch."""
    if cuda is None:
        fake = None  # sys.modules[name] = None → import raises ImportError
    else:
        fake = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: cuda))
    old_torch = sys.modules.get("torch", _MISSING)
    cfg = get_settings().embedding
    old_device = cfg.device
    try:
        sys.modules["torch"] = fake
        cfg.device = device
        embedder._device = None
        return embedder.get_device()
    finally:
        cfg.device = old_device
        embedder._device = None
        if old_torch is _MISSING:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = old_torch


def test_auto_picks_gpu_when_available():
    assert _resolve("auto", cuda=True) == "cuda"


def test_auto_falls_back_to_cpu():
    assert _resolve("auto", cuda=False) == "cpu"
    assert _resolve("auto", cuda=None) == "cpu"   # image CPU-only / thiếu torch


def test_forced_cuda_without_gpu_falls_back():
    assert _resolve("cuda", cuda=False) == "cpu"
    assert _resolve("cuda:1", cuda=None) == "cpu"
    assert _resolve("cuda:1", cuda=True) == "cuda:1"


def test_forced_cpu_never_touches_gpu():
    assert _resolve("cpu", cuda=True) == "cpu"


def test_env_override():
    from sict_crawler.core.config import EmbeddingConfig
    os.environ["EMBED_DEVICE"] = "CPU"
    try:
        assert EmbeddingConfig(device="auto").device == "cpu"
    finally:
        os.environ.pop("EMBED_DEVICE", None)
    assert EmbeddingConfig().device == "auto"


if __name__ == "__main__":
    test_auto_picks_gpu_when_available()
    test_auto_falls_back_to_cpu()
    test_forced_cuda_without_gpu_falls_back()
    test_forced_cpu_never_touches_gpu()
    test_env_override()
    print("OK — embedding device auto/cpu/cuda + fallback")
