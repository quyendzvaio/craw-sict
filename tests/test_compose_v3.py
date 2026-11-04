"""Deploy index v3 (2026-11-05): worker compose đọc .env + bộ biến v3 như API; cutover_v3.sh không build trên VM.

Lỗi đã gặp khi rà kế hoạch bàn giao: `cron` trên VM2 không có env_file và không có EMBED_* / QDRANT_COLLECTION_*
→ chạy torch 384-d, ghi *_v2, không ghi được *_v3 1024-d (worker thử trên dev chạy bằng `docker run -e …` nên không lộ).
`cutover_v2.sh load` build image trên VM (trái D10) và nạp bằng image API (thiếu underthesea cho graphsync)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cutover_v3.sh"

V3_VARS = ("EMBED_BACKEND", "EMBED_API_BASE", "EMBED_API_KEY", "EMBED_MODEL", "EMBED_DIM", "EMBED_QUERY_IMAGE_SIZE",
           "EMBED_CACHE_PATH", "QDRANT_PORT", "QDRANT_GRPC_PORT", "QDRANT_COLLECTION_TEXT", "QDRANT_COLLECTION_IMAGE",
           "QDRANT_COLLECTION_OCR", "QDRANT_TEXT_DIM", "QDRANT_IMAGE_DIM", "CHUNK_STRUCTURED")


def _services(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())["services"]


@pytest.mark.parametrize("svc", ["cron", "svc-ocr"])
def test_worker_reads_env_file_like_api(svc):
    api = _services("docker-compose.yml")["api"]
    worker = _services("docker-compose.worker.yml")[svc]
    assert worker["env_file"] == api["env_file"] == [{"path": ".env", "required": False}]


@pytest.mark.parametrize("svc", ["cron", "svc-ocr"])
def test_worker_passes_v3_vars_from_env(svc):
    env = _services("docker-compose.worker.yml")[svc]["environment"]
    for k in V3_VARS:
        assert k in env, f"{svc} thiếu {k}"
        assert env[k].startswith("${" + k), f"{svc}: {k} phải lấy từ .env (${{{k}:-…}}), đang là {env[k]!r}"
    # bỏ trống = config.yaml (local v2); default chỉ đặt cho những gì không đổi ý nghĩa khi backend torch
    assert env["EMBED_BACKEND"] == "${EMBED_BACKEND:-}"
    assert env["QDRANT_COLLECTION_TEXT"] == "${QDRANT_COLLECTION_TEXT:-}"
    assert env["EMBED_API_BASE"] == "${EMBED_API_BASE:-http://embed:8090/v1}"
    assert env["EMBED_DIM"] == "${EMBED_DIM:-1024}"
    assert env["HF_HOME"] == "/cache/hf"


def test_embed_service_name_and_port_match_default_base():
    embed = _services("docker-compose.embed.yml")["embed"]
    assert embed["ports"] == ["${EMBED_BIND:-127.0.0.1}:${EMBED_PORT:-8090}:8090"]


def test_env_examples_carry_v3_block():
    worker = (ROOT / ".env.worker.example").read_text()
    for line in ("EMBED_BACKEND=api", "QDRANT_COLLECTION_TEXT=sict_text_v3", "QDRANT_COLLECTION_IMAGE=sict_image_v3",
                 "QDRANT_COLLECTION_OCR=sict_ocr_v3", "QDRANT_TEXT_DIM=1024", "QDRANT_IMAGE_DIM=1024",
                 "CHUNK_STRUCTURED=1", "EMBED_BIND="):
        assert f"\n{line}" in worker, f".env.worker.example thiếu dòng {line}"
    vm1 = (ROOT / ".env.example").read_text()
    for k in ("QDRANT_COLLECTION_OCR=sict_ocr_v3", "CHUNK_STRUCTURED=1", "API_MEM_LIMIT=600m"):
        assert k in vm1


def test_cutover_v3_syntax_and_no_build_on_vm():
    assert not (ROOT / "scripts" / "cutover_v2.sh").exists(), "cutover_v2.sh đã đổi thành cutover_v3.sh"
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    assert "docker compose build" not in text and "compose build" not in text        # D10: VM không build
    assert "CONFIRM_S3_UPLOAD" in text and "--upload-media" in text                   # chốt S3 giữ nguyên
    assert "v3-jina-clip-v2" in text
    load = text[text.index("  load)"):text.index("  verify)")]
    assert "docker-compose.worker.yml" in text and " cron \\" in load, "load phải nạp bằng image worker (underthesea)"
    assert "--skip-media-upload" in load and "run --rm --no-deps" in load
    assert "--artifacts" in load


def _env_check(tmp_path, role: str, env: str) -> subprocess.CompletedProcess:
    f = tmp_path / "x.env"
    f.write_text(env)
    return subprocess.run(["bash", str(SCRIPT), "env-check", role], capture_output=True, text=True,
                          env={**os.environ, "ENV_FILE": str(f), "LC_ALL": "C"})


SECRET = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
V3_BLOCK = ("EMBED_BACKEND=api\nEMBED_API_BASE=http://embed:8090/v1\nEMBED_MODEL=jina-clip-v2\nEMBED_DIM=1024\n"
            "QDRANT_COLLECTION_TEXT=sict_text_v3\nQDRANT_COLLECTION_IMAGE=sict_image_v3\nQDRANT_COLLECTION_OCR=sict_ocr_v3\n"
            "QDRANT_TEXT_DIM=1024\nQDRANT_IMAGE_DIM=1024\nCHUNK_STRUCTURED=1\n")
VM1_STORE = f"POSTGRES_PASSWORD={SECRET}\nREDIS_PASSWORD={SECRET}\nQDRANT_API_KEY={SECRET}\nSTORE_BIND=0.0.0.0\n"
VM2_STORE = (f"POSTGRES_DSN=postgresql+asyncpg://sict:{SECRET}@10.0.0.5:5432/sict_crawler\n"
             f"REDIS_URL=redis://:{SECRET}@10.0.0.5:6379/0\nQDRANT_HOST=10.0.0.5\nQDRANT_API_KEY={SECRET}\n")


@pytest.mark.parametrize("role, env, ok", [
    ("vm1", "", False),
    ("vm1", VM1_STORE, False),                                                       # store đủ, thiếu v3
    ("vm1", VM1_STORE + V3_BLOCK, False),                                            # thiếu EMBED_QUERY_IMAGE_SIZE
    ("vm1", VM1_STORE + V3_BLOCK + "EMBED_QUERY_IMAGE_SIZE=336\nAPI_MEM_LIMIT=600m\n", True),
    ("vm1", V3_BLOCK + "EMBED_QUERY_IMAGE_SIZE=336\n", False),                       # v3 đủ, thiếu mật khẩu store
    ("vm1", VM1_STORE + V3_BLOCK.replace("sict_text_v3", "sict_text_v2") + "EMBED_QUERY_IMAGE_SIZE=336\n", False),
    ("vm2", VM2_STORE + V3_BLOCK, False),                                            # thiếu EMBED_BIND
    ("vm2", VM2_STORE + V3_BLOCK + "EMBED_BIND=10.0.0.9\n", True),
    ("vm2", VM2_STORE + V3_BLOCK.replace("EMBED_BACKEND=api", "EMBED_BACKEND=torch") + "EMBED_BIND=10.0.0.9\n", False),
])
def test_env_check(tmp_path, role, env, ok):
    r = _env_check(tmp_path, role, env)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert SECRET not in r.stdout + r.stderr                                         # không in bí mật


def test_rollback_prints_only_without_confirm(tmp_path):
    f = tmp_path / "x.env"
    f.write_text(V3_BLOCK)
    r = subprocess.run(["bash", str(SCRIPT), "rollback"], capture_output=True, text=True,
                       env={**os.environ, "ENV_FILE": str(f), "LC_ALL": "C", "CONFIRM_ROLLBACK": ""})
    assert r.returncode == 0 and "sict_text_v2" in r.stdout
    assert f.read_text() == V3_BLOCK                                                  # không sửa .env


def test_upload_media_refused_without_confirm():
    r = subprocess.run(["bash", str(SCRIPT), "upload-media"], capture_output=True, text=True,
                       env={**os.environ, "CONFIRM_S3_UPLOAD": "", "LC_ALL": "C"})
    assert r.returncode == 2 and "CONFIRM_S3_UPLOAD" in r.stderr


def _compose_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _compose_available(), reason="cần docker compose v2")
@pytest.mark.parametrize("files", [
    ["docker-compose.yml", "docker-compose.worker.yml"],
    ["docker-compose.yml", "docker-compose.worker.yml", "docker-compose.embed.yml"],
    ["docker-compose.worker.yml", "docker-compose.embed.yml"],                       # VM2
])
def test_compose_config_renders_without_env(files):
    """Chỉ render (`config`), không up; .env vắng mặt (required: false) vẫn phải render được."""
    args = ["docker", "compose"]
    for f in files:
        args += ["-f", f]
    r = subprocess.run(args + ["--env-file", os.devnull, "config"], cwd=ROOT, capture_output=True, text=True,
                       timeout=60, env={**os.environ, "HOME": os.environ.get("HOME", "/tmp")})
    assert r.returncode == 0, r.stderr
    if "docker-compose.worker.yml" in files:
        rendered = yaml.safe_load(r.stdout)["services"]["cron"]["environment"]
        assert rendered["EMBED_API_BASE"] == "http://embed:8090/v1"
        assert rendered["EMBED_BACKEND"] == "" and rendered["QDRANT_COLLECTION_TEXT"] == ""
