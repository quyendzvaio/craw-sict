"""Cutover từ trạng thái máy dev (quyết định chủ dự án 2026-11-05): `cutover_v3.sh export | restore | verify-restore |
upload-media-store`.

Artifact 2026-10-12 thiếu bài mới, nhãn bảng `[Bảng: …]` và graph khái niệm của máy dev → production nhận nguyên
pg_dump + snapshot Qdrant *_v3 của máy dev. Các test này không chạm store thật: `docker` / `curl` / `uv` là script giả
trong PATH, ghi lại argv để kiểm cổng CONFIRM và không có bí mật trên dòng lệnh."""
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cutover_v3.sh"
SECRET = "f00dfeedcafebabe0123456789abcdef"
COLS = ("sict_text_v3", "sict_image_v3", "sict_ocr_v3")
V3_BLOCK = ("EMBED_BACKEND=api\nEMBED_API_BASE=http://10.0.0.9:8090/v1\nEMBED_MODEL=jina-clip-v2\nEMBED_DIM=1024\n"
            "QDRANT_COLLECTION_TEXT=sict_text_v3\nQDRANT_COLLECTION_IMAGE=sict_image_v3\nQDRANT_COLLECTION_OCR=sict_ocr_v3\n"
            "QDRANT_TEXT_DIM=1024\nQDRANT_IMAGE_DIM=1024\nCHUNK_STRUCTURED=1\nEMBED_QUERY_IMAGE_SIZE=336\n"
            "API_MEM_LIMIT=600m\n")
VM1_ENV = f"POSTGRES_PASSWORD={SECRET}\nREDIS_PASSWORD={SECRET}\nQDRANT_API_KEY={SECRET}\nSTORE_BIND=0.0.0.0\n" + V3_BLOCK
TABLES = {"documents": 2303, "media": 6240, "entities": 4938}
QINFO = {"points": 16628, "status": "green", "vectors": {"distance": "Cosine", "size": 1024},
         "sparse_vectors": {"bm25": {"modifier": "idf"}}, "payload_schema": {"kind": {"data_type": "keyword"}}}

# docker giả: ghi argv; trả lời đủ cho restore (server_version_num, đếm bảng, docker info); stdin pg_restore bỏ đi
FAKE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "docker $*" >> "$FAKE_LOG"
case "$*" in
  *server_version_num*) echo 160015 ;;
  *query_to_xml*) printf '%s\n' "$FAKE_TABLES" ;;
  info*) echo "$FAKE_ROOT" ;;
  *pg_restore*) cat > /dev/null ;;
esac
exit 0
'''
# curl giả: ghi argv; với -H @file ghi NỘI DUNG header vào log riêng (để kiểm key đi qua header, không qua argv)
FAKE_CURL = r'''#!/usr/bin/env bash
printf '%s\n' "curl $*" >> "$FAKE_LOG"
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -H) if [ "${2#@}" != "$2" ]; then cat "${2#@}" >> "$FAKE_HDR"; echo >> "$FAKE_HDR"; fi; shift 2 ;;
    -o|-X|-d|-F) shift 2 ;;
    http*) url=$1; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  */points/count) echo '{"result":{"count":16628}}' ;;
  */snapshots/upload*) echo '{"result":true,"status":"ok"}' ;;
  */collections/*) printf '%s\n' "$FAKE_COLL" ;;
  */) echo '{"title":"qdrant","version":"1.19.1"}' ;;
esac
exit 0
'''
FAKE_FAIL = "#!/usr/bin/env bash\nprintf '%s\\n' \"$(basename \"$0\") $*\" >> \"$FAKE_LOG\"\nexit 97\n"


def _bin(tmp_path: Path, docker: str = FAKE_FAIL, curl: str = FAKE_FAIL) -> Path:
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    for name, body in (("docker", docker), ("curl", curl), ("uv", FAKE_FAIL)):
        p = b / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return b


def _run(tmp_path: Path, args: list[str], bin_dir: Path, **env) -> subprocess.CompletedProcess:
    base = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LC_ALL": "C",
            "FAKE_LOG": str(tmp_path / "calls.log"), "FAKE_HDR": str(tmp_path / "headers.log"),
            "CONFIRM_RESTORE": "", "CONFIRM_S3_UPLOAD": ""}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                          env={**base, **env})


def _calls(tmp_path: Path) -> str:
    p = tmp_path / "calls.log"
    return p.read_text() if p.exists() else ""


def test_syntax_and_steps():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    for step in ("export", "restore", "verify-restore", "upload-media-store", "pack", "load", "backup", "verify"):
        assert f"\n  {step})" in text, f"thiếu bước {step}"
    usage = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True).stdout
    assert all(s in usage for s in ("export", "restore", "upload-media-store", "pack"))


def test_export_reads_only_v3_and_cleans_its_snapshots():
    text = SCRIPT.read_text()
    exp = text[text.index("\n  export)"):text.index("\n  restore)")]
    assert '"sict_text_v3 sict_image_v3 sict_ocr_v3"' in text                      # chỉ 3 collection v3
    assert "pg_dump -U \"$PG_USER\" -Fc" in exp                                     # custom format
    assert "trap cleanup_snapshots EXIT" in exp and "-X DELETE" in exp             # xoá snapshot do chính nó tạo
    assert "DELETE /collections/${cs%%/*}/snapshots/${cs#*/}" in exp
    assert "/points/delete" not in text and "-X DELETE -o" in exp                   # không xoá point / collection
    assert exp.count("-X DELETE") == 1
    assert "free_kb" in exp and "MIN_FREE_GB" in exp                                # kiểm chỗ trống trước khi ghi
    assert "sha256" in exp and "manifest.json" in exp


def test_export_refuses_non_empty_out(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "old.dump").write_text("x")
    r = _run(tmp_path, ["export"], _bin(tmp_path), OUT=str(out))
    assert r.returncode == 1 and "đã có dữ liệu" in r.stderr
    assert _calls(tmp_path) == ""                                                   # không gọi docker / curl


@pytest.mark.parametrize("confirm", ["", "no", "YES"])
def test_restore_refused_without_confirm(tmp_path, confirm):
    r = _run(tmp_path, ["restore"], _bin(tmp_path), CONFIRM_RESTORE=confirm, OUT=str(tmp_path / "none"))
    assert r.returncode == 2 and "CONFIRM_RESTORE=yes" in r.stderr
    assert _calls(tmp_path) == ""


@pytest.mark.parametrize("confirm", ["", "no"])
def test_upload_media_store_refused_without_confirm(tmp_path, confirm):
    store = tmp_path / "store" / "media"
    store.mkdir(parents=True)
    (store / "ab.jpg").write_bytes(b"x")
    r = _run(tmp_path, ["upload-media-store"], _bin(tmp_path), CONFIRM_S3_UPLOAD=confirm,
             SICT_LOCAL_STORE=str(tmp_path / "store"))
    assert r.returncode == 2 and "CONFIRM_S3_UPLOAD" in r.stderr
    assert _calls(tmp_path) == ""                                                   # không gọi uv / python S3


def test_upload_media_store_uses_storage_keys():
    text = SCRIPT.read_text()
    up = text[text.index("\n  upload-media-store)"):text.index("\n  pack)")]
    assert "from sict_crawler.repository.storage import Storage" in up              # cùng bucket / prefix Storage
    assert "st.cfg.prefix_media" in up and "relative_to(root)" in up                # key = media/<sha256>.<ext>
    assert "if st.local" in up                                                      # không AWS → từ chối
    assert "k not in have" in up                                                    # key có rồi thì bỏ qua


def _export_dir(tmp_path: Path, tables: dict | None = None, corrupt: bool = False) -> Path:
    out = tmp_path / "dev-state"
    out.mkdir()

    def put(name: str, data: bytes) -> dict:
        (out / name).write_bytes(data)
        return {"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    info = {k: v for k, v in QINFO.items() if k != "status"}
    m = {"format": "sict-dev-state/1",
         "postgres": {"version": "PostgreSQL 16.15", "major": 16, "db": "sict_crawler", **put("sict_crawler.dump", b"PGDMP"),
                      "tables": tables or TABLES},
         "qdrant": {"version": "1.19.1", "collections": {c: {**put(f"{c}.snapshot", c.encode()), **info} for c in COLS}}}
    (out / "manifest.json").write_text(json.dumps(m))
    if corrupt:
        (out / "sict_image_v3.snapshot").write_bytes(b"truncated")
    return out


def _restore(tmp_path: Path, out: Path, pg_tables: dict) -> subprocess.CompletedProcess:
    env_file = tmp_path / "vm1.env"
    env_file.write_text(VM1_ENV)
    return _run(tmp_path, ["restore"], _bin(tmp_path, FAKE_DOCKER, FAKE_CURL), CONFIRM_RESTORE="yes", OUT=str(out),
                ENV_FILE=str(env_file), PG_CONTAINER="sict-restoretest-pg", QDRANT_URL="http://localhost:56333",
                SKIP_BACKUP_CHECK="yes", FAKE_TABLES=json.dumps(pg_tables), FAKE_ROOT=str(tmp_path),
                FAKE_COLL=json.dumps({"result": {"status": "green", "config": {"params": {
                    "vectors": QINFO["vectors"], "sparse_vectors": QINFO["sparse_vectors"]}},
                    "payload_schema": {"kind": {"data_type": "keyword", "points": 5}}}}))


def test_restore_flow_and_no_secret_on_command_line(tmp_path):
    r = _restore(tmp_path, _export_dir(tmp_path), TABLES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "khớp manifest" in r.stdout
    calls = _calls(tmp_path)
    assert SECRET not in calls and SECRET not in r.stdout + r.stderr                # không lên argv / không in
    assert f"api-key: {SECRET}" in (tmp_path / "headers.log").read_text()          # key đi qua header @/dev/fd
    assert "DROP DATABASE IF EXISTS sict_crawler WITH (FORCE)" in calls
    assert "--no-owner --no-privileges --exit-on-error --single-transaction" in calls
    for c in COLS:
        assert f"http://localhost:56333/collections/{c}/snapshots/upload?wait=true&priority=snapshot&checksum=" in calls
    assert "docker compose" not in calls                                            # PG_CONTAINER → docker exec


def test_restore_detects_count_mismatch(tmp_path):
    r = _restore(tmp_path, _export_dir(tmp_path), {**TABLES, "documents": 2277})
    assert r.returncode == 1 and "Postgres lệch manifest" in r.stderr


def test_restore_refuses_corrupt_copy_before_touching_stores(tmp_path):
    r = _restore(tmp_path, _export_dir(tmp_path, corrupt=True), TABLES)
    assert r.returncode == 1 and "sha256 lệch manifest" in r.stderr
    assert "DROP DATABASE" not in _calls(tmp_path) and "upload" not in _calls(tmp_path)


def test_qdrant_key_only_via_header_fd():
    """Mọi lời gọi curl tới Qdrant đi qua qcurl: key đọc từ ENV_FILE, đưa vào header bằng process substitution."""
    text = SCRIPT.read_text()
    assert "curl -sS --fail-with-body -H @<(printf 'api-key: %s' \"$qkey\")" in text
    lines = [ln for ln in text.splitlines() if "curl " in ln and not ln.lstrip().startswith("#")]
    for ln in lines:
        assert "QDRANT_API_KEY" not in ln and "api-key: $" not in ln, ln
    assert "REDISCLI_AUTH=" not in text and "PGPASSWORD=" not in text
