"""D12 (2026-11-01): Postgres / Redis / Qdrant có mật khẩu / API key, cổng store chỉ mở theo STORE_BIND.

Repo đã public → mật khẩu ghi cứng trong compose là mật khẩu ai cũng biết. Các test này giữ compose khỏi
quay lại trạng thái cũ, và kiểm script chặn deploy khi `.env` trên VM còn thiếu bí mật."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_safety import _minimal_data

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "store_auth.sh"


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())["services"]


def test_qdrant_api_key_from_env(monkeypatch):
    from sict_crawler.core.config import _build

    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    assert _build(_minimal_data()).qdrant.api_key == ""
    monkeypatch.setenv("QDRANT_API_KEY", "k" * 32)
    assert _build(_minimal_data()).qdrant.api_key == "k" * 32


def test_client_sends_api_key_only_when_set(monkeypatch):
    from sict_crawler.core import config
    from sict_crawler.repository import vector_store

    seen = []
    monkeypatch.setattr(vector_store, "QdrantClient", lambda **kw: seen.append(kw) or object())
    for key, expected in (("", None), ("k" * 32, "k" * 32)):
        monkeypatch.setenv("QDRANT_API_KEY", key)
        monkeypatch.setattr(config, "_settings", None)                  # đọc lại env
        monkeypatch.setattr(vector_store, "_client", None)
        vector_store.get_client()
        assert seen[-1]["api_key"] == expected


def test_vm1_stores_bind_and_auth():
    svc = _compose("docker-compose.yml")
    for name in ("postgres", "redis", "qdrant"):
        for p in svc[name]["ports"]:
            assert p.startswith("${STORE_BIND:-127.0.0.1}:"), f"{name} mở cổng {p} trên mọi địa chỉ"
    assert svc["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD")
    assert "--requirepass" in svc["redis"]["command"]
    assert svc["qdrant"]["environment"]["QDRANT__SERVICE__API_KEY"] == "${QDRANT_API_KEY:-}"
    env = svc["api"]["environment"]
    assert "${POSTGRES_PASSWORD" in env["POSTGRES_DSN"] and "${REDIS_PASSWORD" in env["REDIS_URL"]
    assert env["QDRANT_API_KEY"] == "${QDRANT_API_KEY:-}"


def test_worker_gets_qdrant_key():
    for name in ("cron", "svc-ocr"):
        assert _compose("docker-compose.worker.yml")[name]["environment"]["QDRANT_API_KEY"] == "${QDRANT_API_KEY:-}"


def _check(tmp_path, role: str, env: str) -> subprocess.CompletedProcess:
    (tmp_path / ".env").write_text(env)
    return subprocess.run(["bash", str(SCRIPT), "check", role], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C"})


STRONG = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


@pytest.mark.parametrize("env, ok", [
    ("", False),
    (f"POSTGRES_PASSWORD=sict\nREDIS_PASSWORD={STRONG}\nQDRANT_API_KEY={STRONG}\nSTORE_BIND=0.0.0.0\n", False),
    (f"POSTGRES_PASSWORD={STRONG}\nREDIS_PASSWORD=\nQDRANT_API_KEY={STRONG}\nSTORE_BIND=0.0.0.0\n", False),
    (f"POSTGRES_PASSWORD=short\nREDIS_PASSWORD={STRONG}\nQDRANT_API_KEY={STRONG}\nSTORE_BIND=0.0.0.0\n", False),
    (f"POSTGRES_PASSWORD={STRONG}\nREDIS_PASSWORD={STRONG}\nQDRANT_API_KEY={STRONG}\n", False),       # VM2 không tới được
    (f"POSTGRES_PASSWORD={STRONG}\nREDIS_PASSWORD={STRONG}\nQDRANT_API_KEY={STRONG}\nSTORE_BIND=0.0.0.0\n", True),
])
def test_check_vm1(tmp_path, env, ok):
    r = _check(tmp_path, "vm1", env)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert STRONG not in r.stdout + r.stderr                       # không in bí mật ra log CD


@pytest.mark.parametrize("env, ok", [
    ("POSTGRES_DSN=postgresql+asyncpg://sict:sict@10.0.0.5:5432/sict_crawler\n"
     f"REDIS_URL=redis://:{STRONG}@10.0.0.5:6379/0\nQDRANT_HOST=10.0.0.5\nQDRANT_API_KEY={STRONG}\n", False),
    (f"POSTGRES_DSN=postgresql+asyncpg://sict:{STRONG}@10.0.0.5:5432/sict_crawler\n"
     f"REDIS_URL=redis://10.0.0.5:6379/0\nQDRANT_HOST=10.0.0.5\nQDRANT_API_KEY={STRONG}\n", False),
    (f"POSTGRES_DSN=postgresql+asyncpg://sict:{STRONG}@10.0.0.5:5432/sict_crawler\n"
     f"REDIS_URL=redis://:{STRONG}@10.0.0.5:6379/0\nQDRANT_HOST=10.0.0.5\n", False),
    (f"POSTGRES_DSN=postgresql+asyncpg://sict:{STRONG}@10.0.0.5:5432/sict_crawler\n"
     f"REDIS_URL=redis://:{STRONG}@10.0.0.5:6379/0\nQDRANT_HOST=10.0.0.5\nQDRANT_API_KEY={STRONG}\n", True),
])
def test_check_vm2(tmp_path, env, ok):
    r = _check(tmp_path, "vm2", env)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert STRONG not in r.stdout + r.stderr
