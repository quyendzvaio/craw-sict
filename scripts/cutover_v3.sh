#!/usr/bin/env bash
# Cutover production sang index v3 (D8/D16/D22: jina-clip-v2 1024-d, 3 collection *_v3, embed int8 tự host trên VM2)
# — từng bước do người vận hành gọi, KHÔNG gộp tự động. Collection cũ (v1/v2) và cột cũ của Postgres không bị đụng
# → rollback = đổi QDRANT_COLLECTION_* trong .env rồi `docker compose up -d api`.
#
#   Đường chính (quyết định chủ dự án 2026-11-05: production nhận ĐÚNG trạng thái index v3 của máy dev):
#   (máy dev) ./scripts/cutover_v3.sh export            pg_dump + snapshot Qdrant sict_{text,image,ocr}_v3 + manifest.json
#                                                       → $OUT (_cutover/dev-state) → chép lên VM1 cùng script này
#   (máy dev có AWS) CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v3.sh upload-media-store
#                                                       file media của local store (SICT_LOCAL_STORE) lên S3, cùng key
#                                                       Storage dùng (media/<sha256>.<ext>), key có rồi thì bỏ qua
#   (VM1)     CONFIRM_RESTORE=yes ./scripts/cutover_v3.sh restore
#                                                       thay DB sict_crawler bằng dump + nạp 3 snapshot vào Qdrant, rồi
#                                                       so số đếm với manifest (verify-restore chạy lại riêng bước so)
#   Đường thay thế (artifact compute 2026-10-12, KHÔNG có dữ liệu mới sau đó):
#   (máy dev) ./scripts/cutover_v3.sh pack              đóng gói artifact v3 + HTML snapshot (~330 MB) → chép lên VM2
#   (VM2)     ./scripts/cutover_v3.sh load              nạp PG + *_v3 bằng IMAGE WORKER (loader gọi graphsync cần
#                                                       underthesea, chỉ image worker có), KHÔNG build, KHÔNG upload media
#   (máy có snapshot + AWS) CONFIRM_S3_UPLOAD=yes ./scripts/cutover_v3.sh upload-media
#                                                       ~1,9 GB ảnh + frame lên S3 (key media/<sha256>.<ext>, có rồi thì bỏ qua)
#   Chung:
#   (VM1|VM2) ./scripts/cutover_v3.sh env-check vm1|vm2 .env có đủ biến v3 (chỉ in TÊN biến) + store_auth.sh check
#   (VM1)     ./scripts/cutover_v3.sh backup            pg_dump + snapshot Qdrant các collection đang có → ~/backup-v3-<ngày>/
#   (dev/VM1) ./scripts/cutover_v3.sh verify            golden_v2 / list_v2 + accept_api qua HTTP (API=…)
#   (VM1)     ./scripts/cutover_v3.sh rollback          in hướng dẫn; CONFIRM_ROLLBACK=yes [ROLLBACK_TO=v2|v1] thì làm thật
#
# Thứ tự production (docs/deploy_v3.md): CD vm2 + deploy_embed → dừng cron → export (dev) → upload-media-store
# → backup (VM1) → dừng API → restore (VM1) → CD vm1 (API đọc .env v3) → verify → bật cron.
# Deploy code = workflow CD (D10: VM chỉ pull image GHCR, không build).
# Ảnh v3 hiện qua /media/s/<sha256> chỉ sau khi media lên S3 → upload TRƯỚC khi API v3 lên thì không có ảnh hỏng.
# Không bao giờ in giá trị .env (mật khẩu / key) — chỉ tên biến; key Qdrant đi qua header đọc từ fd, không lên argv.
set -euo pipefail

SNAP=${SNAP:-_snapshot/2026-10-12}
ART=${ART:-$SNAP/artifacts/v3-jina-clip-v2}
PKG=${PKG:-_cutover/v3-load.tgz}
API=${API:-http://localhost:8003}
ENV_FILE=${ENV_FILE:-.env}
QDRANT_URL=${QDRANT_URL:-http://localhost:6333}   # Qdrant đích (VM1: compose qdrant; thử: cổng khác)
WORKER_COMPOSE=${WORKER_COMPOSE:-docker-compose.worker.yml}
cd "$(dirname "$0")/.."
export ENV_FILE

val() {   # giá trị của khoá trong ENV_FILE (dòng cuối thắng như docker compose); không source file, không in ra
  local v
  v=$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  printf '%s' "$v"
}

say() { printf '\n→ %s\n' "$*"; }
run() { printf '$ %s\n' "$*"; "$@"; }

env_check() {   # chỉ báo TÊN biến thiếu / sai; giá trị không bao giờ in
  local role=$1 fail=0 k
  [ -f "$ENV_FILE" ] || { echo "v3: không thấy $ENV_FILE"; exit 1; }
  expect() {   # expect <biến> <giá trị mong đợi>
    if [ "$(val "$1")" != "$2" ]; then echo "v3: $1 phải là $2"; fail=1; fi
  }
  nonempty() { [ -n "$(val "$1")" ] || { echo "v3: $1 chưa đặt"; fail=1; }; }
  expect EMBED_BACKEND api
  nonempty EMBED_API_BASE
  expect EMBED_MODEL jina-clip-v2
  expect EMBED_DIM 1024
  expect QDRANT_COLLECTION_TEXT sict_text_v3
  expect QDRANT_COLLECTION_IMAGE sict_image_v3
  expect QDRANT_COLLECTION_OCR sict_ocr_v3
  expect QDRANT_TEXT_DIM 1024
  expect QDRANT_IMAGE_DIM 1024
  expect CHUNK_STRUCTURED 1
  case "$role" in
    vm1)
      nonempty EMBED_QUERY_IMAGE_SIZE
      [ "$(val API_MEM_LIMIT)" = 600m ] || echo "v3: gợi ý API_MEM_LIMIT=600m (API backend api chỉ ~200 MiB; trả RAM cho VM1)"
      ;;
    vm2)
      nonempty EMBED_BIND
      ;;
    *) echo "dùng: $0 env-check vm1|vm2"; exit 2 ;;
  esac
  [ $fail = 0 ] && echo "v3: $ENV_FILE $role đủ biến index v3" || exit 1
  # D12: mật khẩu / API key store (store_auth.sh cũng đọc ENV_FILE, không in giá trị)
  bash scripts/store_auth.sh check "$role"
}

qcurl() {   # qcurl <url gốc Qdrant> <đường dẫn> [tham số curl…]; API key qua header đọc từ ENV_FILE, không lên dòng lệnh
  local base=$1 path=$2; shift 2
  local qkey; qkey="$(val QDRANT_API_KEY)"
  curl -sS --fail-with-body -H @<(printf 'api-key: %s' "$qkey") "$base$path" "$@"
}
qdrant_curl() { qcurl "$QDRANT_URL" "$@"; }   # Qdrant của máy đang đứng (VM1: compose qdrant)

# --- trạng thái máy dev (export / restore) --------------------------------------------------------------------
OUT=${OUT:-_cutover/dev-state}
V3_COLLECTIONS=${V3_COLLECTIONS:-"sict_text_v3 sict_image_v3 sict_ocr_v3"}
SRC_PG_CONTAINER=${SRC_PG_CONTAINER:-craw-sict-postgres-1}   # export đọc từ đây (chỉ pg_dump + SELECT)
SRC_QDRANT=${SRC_QDRANT:-http://localhost:6333}              # export: chỉ tạo / tải / xoá snapshot của chính nó
PG_DB=sict_crawler
PG_USER=sict

pg() {   # pg <lệnh…> trong container postgres đích: PG_CONTAINER (docker exec) hoặc service `postgres` của compose
  if [ -n "${PG_CONTAINER:-}" ]; then docker exec -i "$PG_CONTAINER" "$@"; else docker compose exec -T postgres "$@"; fi
}
src_pg() { docker exec -i "$SRC_PG_CONTAINER" "$@"; }

# Số dòng mọi bảng public trong 1 câu SQL → JSON {"bảng": n}. $1 = pg | src_pg
pg_counts() {
  "$1" psql -X -At -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" -c "
    select coalesce(json_object_agg(table_name, n order by table_name), '{}'::json) from (
      select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', table_name),
                                false, true, '')))[1]::text::bigint as n
      from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE') t" </dev/null
}

# Mô tả 1 collection: số point đếm chính xác + cấu hình vector / sparse + payload index (kiểu + tham số, bỏ số đếm)
qinfo() {   # qinfo <url gốc> <collection> → JSON một dòng
  local cfg cnt
  cfg=$(qcurl "$1" "/collections/$2") || return 1
  cnt=$(qcurl "$1" "/collections/$2/points/count" -X POST -H 'Content-Type: application/json' -d '{"exact": true}') || return 1
  python3 -c '
import json, sys
c = json.loads(sys.argv[1])["result"]; p = c["config"]["params"]
schema = {k: {kk: vv for kk, vv in v.items() if kk != "points"} for k, v in (c.get("payload_schema") or {}).items()}
print(json.dumps({"points": json.loads(sys.argv[2])["result"]["count"], "status": c["status"],
                  "vectors": p.get("vectors"), "sparse_vectors": p.get("sparse_vectors"), "payload_schema": schema},
                 sort_keys=True))' "$cfg" "$cnt"
}

free_kb() { df -Pk "$1" | awk 'NR==2 {print $4}'; }

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

mf() {   # mf <biểu thức python trên m = manifest> → in giá trị (dùng trong restore)
  python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); v=eval(sys.argv[2]); print(v if isinstance(v,(str,int)) else json.dumps(v, sort_keys=True))' \
    "$OUT/manifest.json" "$1"
}

# So trạng thái đích (PG qua `pg`, Qdrant qua QDRANT_URL) với manifest; lệch → exit 1
verify_state() {
  local c actual
  say "so số dòng Postgres với manifest"
  actual=$(pg_counts pg)
  python3 - "$OUT/manifest.json" "$actual" <<'PY'
import json, sys
want = json.load(open(sys.argv[1]))["postgres"]["tables"]; got = json.loads(sys.argv[2])
bad = [f"  {t}: manifest {want.get(t)} ≠ đích {got.get(t)}" for t in sorted(set(want) | set(got)) if want.get(t) != got.get(t)]
for t in sorted(want):
    print(f"  {t:<20} {want[t]:>8} {'ok' if want[t] == got.get(t) else 'LỆCH'}")
if bad:
    sys.exit("Postgres lệch manifest:\n" + "\n".join(bad))
PY
  say "so Qdrant với manifest (points đếm chính xác, cấu hình vector, payload index)"
  for c in $(mf '" ".join(m["qdrant"]["collections"])'); do
    actual=$(qinfo "$QDRANT_URL" "$c") || { echo "Qdrant đích không có collection $c" >&2; exit 1; }
    python3 - "$OUT/manifest.json" "$c" "$actual" <<'PY'
import json, sys
want = json.load(open(sys.argv[1]))["qdrant"]["collections"][sys.argv[2]]; got = json.loads(sys.argv[3])
bad = [k for k in ("points", "vectors", "sparse_vectors", "payload_schema") if want[k] != got[k]]
print(f"  {sys.argv[2]:<14} points {got['points']:>6} / manifest {want['points']:>6}  status {got['status']}  "
      + ("ok" if not bad else "LỆCH: " + ", ".join(bad)))
if bad:
    sys.exit(1)
PY
  done
  echo "khớp manifest"
}

case "${1:-}" in
  export)
    # Máy dev: trạng thái index v3 đang phục vụ (PG + 3 collection *_v3) → $OUT. Chỉ ĐỌC store dev: pg_dump, SELECT,
    # tạo / tải / xoá snapshot do chính lệnh này tạo (không đụng point / collection / snapshot khác).
    if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT")" ]; then
      echo "$OUT đã có dữ liệu (export trước?) — xoá thư mục đó hoặc đặt OUT=…" >&2; exit 1
    fi
    docker inspect "$SRC_PG_CONTAINER" >/dev/null || { echo "không thấy container $SRC_PG_CONTAINER (SRC_PG_CONTAINER=…)" >&2; exit 1; }
    for c in $V3_COLLECTIONS; do
      qcurl "$SRC_QDRANT" "/collections/$c" -o /dev/null || { echo "Qdrant nguồn $SRC_QDRANT không có $c" >&2; exit 1; }
    done
    mkdir -p "$OUT/.parts"
    # Chỗ trống: dump ≤ cỡ DB; snapshot tạo trong container Qdrant nguồn rồi tải về (máy dev: cùng ổ) → đòi
    # cỡ DB + MIN_FREE_GB (mặc định 5) lúc đầu, rồi trước mỗi lần tải: cỡ snapshot + 1 GB.
    pg_bytes=$(src_pg psql -X -At -U "$PG_USER" -d "$PG_DB" -c "select pg_database_size('$PG_DB')" </dev/null)
    need_kb=$(( pg_bytes / 1024 + ${MIN_FREE_GB:-5} * 1024 * 1024 ))
    free=$(free_kb "$OUT")
    if [ "$free" -lt "$need_kb" ]; then
      echo "thiếu chỗ ở $OUT: còn $((free / 1024)) MB, cần ≥ $((need_kb / 1024)) MB (DB $((pg_bytes / 1048576)) MB + MIN_FREE_GB)" >&2
      exit 1
    fi
    say "chỗ trống $OUT: $((free / 1048576)) GB (cần ≥ $((need_kb / 1048576)) GB); DB $PG_DB $((pg_bytes / 1048576)) MB"

    created=()
    cleanup_snapshots() {   # chỉ xoá snapshot do lệnh này tạo
      local cs
      for cs in "${created[@]}"; do
        if qcurl "$SRC_QDRANT" "/collections/${cs%%/*}/snapshots/${cs#*/}?wait=true" -X DELETE -o /dev/null; then
          echo "  đã xoá snapshot ${cs#*/} khỏi Qdrant nguồn"
        else
          echo "cảnh báo: không xoá được snapshot $cs trong Qdrant nguồn — xoá tay: DELETE /collections/${cs%%/*}/snapshots/${cs#*/}" >&2
        fi
      done
      created=()
    }
    trap cleanup_snapshots EXIT

    say "đếm trước (PG mọi bảng + Qdrant points chính xác)"
    pg_counts src_pg > "$OUT/.parts/pg_before.json"
    for c in $V3_COLLECTIONS; do qinfo "$SRC_QDRANT" "$c" > "$OUT/.parts/q_$c.json"; done

    say "pg_dump -Fc $PG_DB ← container $SRC_PG_CONTAINER"
    src_pg pg_dump -U "$PG_USER" -Fc "$PG_DB" </dev/null > "$OUT/$PG_DB.dump"

    for c in $V3_COLLECTIONS; do
      say "snapshot Qdrant $c"
      r=$(qcurl "$SRC_QDRANT" "/collections/$c/snapshots?wait=true" -X POST)
      read -r name size checksum < <(python3 -c '
import json, sys
r = json.loads(sys.argv[1])["result"]; print(r["name"], r.get("size") or 0, r.get("checksum") or "-")' "$r")
      [ -n "${name:-}" ] || { echo "Qdrant không trả tên snapshot $c: $r" >&2; exit 1; }
      created+=("$c/$name")
      free=$(free_kb "$OUT")
      if [ "$free" -lt $(( size / 1024 + 1048576 )) ]; then
        echo "thiếu chỗ để tải snapshot $c ($((size / 1048576)) MB, còn $((free / 1024)) MB)" >&2; exit 1
      fi
      qcurl "$SRC_QDRANT" "/collections/$c/snapshots/$name" -o "$OUT/$c.snapshot"
      sha=$(sha256_of "$OUT/$c.snapshot")
      if [ "$checksum" != "-" ] && [ "$sha" != "$checksum" ]; then
        echo "sha256 snapshot $c tải về ($sha) ≠ checksum Qdrant ($checksum)" >&2; exit 1
      fi
      echo "  $c: $((size / 1048576)) MB, sha256 khớp checksum Qdrant"
    done
    cleanup_snapshots

    say "đếm sau — phải trùng lúc trước (store bị ghi giữa chừng → bản export không nhất quán: chờ worker nghỉ rồi chạy lại)"
    [ "$(pg_counts src_pg)" = "$(cat "$OUT/.parts/pg_before.json")" ] || { echo "Postgres đổi trong lúc export" >&2; exit 1; }
    for c in $V3_COLLECTIONS; do
      [ "$(qinfo "$SRC_QDRANT" "$c" | python3 -c 'import json,sys; print(json.load(sys.stdin)["points"])')" \
        = "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["points"])' "$OUT/.parts/q_$c.json")" ] \
        || { echo "Qdrant $c đổi số point trong lúc export" >&2; exit 1; }
    done

    say "media: key trong bảng media ↔ file trong local store (${SICT_LOCAL_STORE:-./_local_store}) cho upload-media-store"
    src_pg psql -X -At -U "$PG_USER" -d "$PG_DB" -c "select s3_key from media where coalesce(s3_key, '') <> ''" \
      </dev/null > "$OUT/.parts/media_keys.txt"
    src_pg psql -X -At -U "$PG_USER" -d "$PG_DB" -c "select version()" </dev/null > "$OUT/.parts/pg_version.txt"
    qcurl "$SRC_QDRANT" / > "$OUT/.parts/qdrant_version.json"
    git rev-parse HEAD > "$OUT/.parts/git.txt" 2>/dev/null || echo "-" > "$OUT/.parts/git.txt"

    COLS="$V3_COLLECTIONS" DB="$PG_DB" STORE="${SICT_LOCAL_STORE:-./_local_store}" python3 - "$OUT" <<'PY'
import datetime, hashlib, json, os, sys
from pathlib import Path
out = Path(sys.argv[1]); parts = out / ".parts"

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def fileinfo(name: str) -> dict:
    p = out / name
    return {"file": name, "bytes": p.stat().st_size, "sha256": sha(p)}

ver = (parts / "pg_version.txt").read_text().strip()
store = Path(os.environ["STORE"])
keys = [k for k in (parts / "media_keys.txt").read_text().split("\n") if k]
missing = [k for k in keys if not (store / k).is_file()]
media_dir = store / "media"
local = [p for p in media_dir.rglob("*") if p.is_file()] if media_dir.is_dir() else []
cols = {}
for c in os.environ["COLS"].split():
    info = json.loads((parts / f"q_{c}.json").read_text())
    info.pop("status", None)
    cols[c] = {**fileinfo(f"{c}.snapshot"), **info}
tables = json.loads((parts / "pg_before.json").read_text())
m = {
    "format": "sict-dev-state/1",
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "git_commit": (parts / "git.txt").read_text().strip(),
    "postgres": {"version": ver, "major": int(ver.split()[1].split(".")[0]), "db": os.environ["DB"],
                 **fileinfo(f"{os.environ['DB']}.dump"), "tables": tables},
    "qdrant": {"version": json.loads((parts / "qdrant_version.json").read_text()).get("version"), "collections": cols},
    "media": {"keys_in_pg": len(keys), "missing_in_local_store": len(missing), "missing_sample": missing[:10],
              "local_store_files": len(local), "local_store_bytes": sum(p.stat().st_size for p in local)},
}
(out / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print(f"  documents {tables.get('documents')}  media {tables.get('media')}  entities {tables.get('entities')}  "
      + "  ".join(f"{c} {v['points']}" for c, v in cols.items()))
print(f"  media trong PG {len(keys)}, thiếu trong local store {len(missing)}; local store {len(local)} file")
PY
    rm -rf "$OUT/.parts"
    ls -lh "$OUT"; du -sh "$OUT"
    say "chép lên VM1 (CD không chép script này): scp scripts/cutover_v3.sh ubuntu@<IP-VM1>:craw-sict/scripts/ &&" \
        "scp -r $OUT ubuntu@<IP-VM1>:craw-sict/_cutover/"
    say "trên VM1 (sau backup, API + cron dừng): OUT=_cutover/$(basename "$OUT") CONFIRM_RESTORE=yes ./scripts/cutover_v3.sh restore"
    ;;
  restore)
    # VM1: thay DB sict_crawler bằng dump + nạp 3 snapshot *_v3 vào Qdrant (collection khác: v1 / v2 giữ nguyên).
    if [ "${CONFIRM_RESTORE:-}" != "yes" ]; then
      echo "Từ chối: restore XOÁ DB $PG_DB và thay ${V3_COLLECTIONS// /, } bằng bản export — cần CONFIRM_RESTORE=yes" \
           "(chỉ sau bước backup, khi API + cron đã dừng và chủ dự án đã cho phép)." >&2
      exit 2
    fi
    [ -f "$OUT/manifest.json" ] || { echo "không thấy $OUT/manifest.json (OUT=…)" >&2; exit 1; }
    say "kiểm sha256 file export với manifest"
    python3 - "$OUT" <<'PY'
import hashlib, json, sys
from pathlib import Path
out = Path(sys.argv[1]); m = json.loads((out / "manifest.json").read_text())
items = [m["postgres"]] + list(m["qdrant"]["collections"].values())
for it in items:
    h = hashlib.sha256()
    with (out / it["file"]).open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    if h.hexdigest() != it["sha256"]:
        sys.exit(f"{it['file']}: sha256 lệch manifest (chép thiếu / hỏng) — chép lại")
    print(f"  {it['file']}: ok")
PY
    env_check vm1
    if [ "${SKIP_BACKUP_CHECK:-}" != "yes" ]; then
      last=$(ls -dt ~/backup-v3-*/ 2>/dev/null | head -1 || true)
      if [ -z "$last" ] || [ ! -s "${last}sict_crawler.dump" ]; then
        echo "chưa có backup (~/backup-v3-*/sict_crawler.dump) — chạy ./scripts/cutover_v3.sh backup trước" >&2; exit 1
      fi
      echo "backup gần nhất: $last"
    fi
    if [ -z "${PG_CONTAINER:-}" ] && docker compose ps --status running api 2>/dev/null | grep -q api \
        && [ "${ALLOW_API_RUNNING:-}" != "yes" ]; then
      echo "API đang chạy — dừng trước (docker compose stop api) để không phục vụ DB đang thay (hoặc ALLOW_API_RUNNING=yes)" >&2
      exit 2
    fi
    src_major=$(mf 'm["postgres"]["major"]')
    dst_num=$(pg psql -X -At -U "$PG_USER" -d postgres -c 'show server_version_num' </dev/null)
    [ $(( dst_num / 10000 )) -ge "$src_major" ] || { echo "Postgres đích $dst_num cũ hơn nguồn (major $src_major)" >&2; exit 1; }
    src_q=$(mf 'm["qdrant"]["version"]')
    dst_q=$(qdrant_curl / | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
    if [ "${src_q%.*}" != "${dst_q%.*}" ] && [ "${ALLOW_QDRANT_VERSION_MISMATCH:-}" != "yes" ]; then
      echo "Qdrant đích $dst_q khác nguồn $src_q (snapshot chỉ chắc chắn nạp được cùng major.minor)" >&2; exit 1
    fi
    total=$(mf 'm["postgres"]["bytes"] + sum(c["bytes"] for c in m["qdrant"]["collections"].values())')
    root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /)
    free=$(free_kb "$root")
    if [ "$free" -lt $(( total * 2 / 1024 + 1048576 )) ]; then
      echo "thiếu chỗ ở $root: còn $((free / 1024)) MB, cần ≥ 2 × $((total / 1048576)) MB + 1 GB" >&2; exit 1
    fi

    say "Postgres: DROP + CREATE $PG_DB rồi pg_restore (không owner / quyền: mọi đối tượng thuộc $PG_USER)"
    pg psql -X -q -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres \
      -c "DROP DATABASE IF EXISTS $PG_DB WITH (FORCE)" -c "CREATE DATABASE $PG_DB OWNER $PG_USER" </dev/null
    pg pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner --no-privileges --exit-on-error --single-transaction \
      < "$OUT/$(mf 'm["postgres"]["file"]')"

    for c in $(mf '" ".join(m["qdrant"]["collections"])'); do
      say "Qdrant: nạp snapshot $c (priority=snapshot: thay collection cùng tên nếu có; checksum Qdrant tự kiểm)"
      f="$OUT/$(mf "m['qdrant']['collections']['$c']['file']")"
      sum=$(mf "m['qdrant']['collections']['$c']['sha256']")
      r=$(qdrant_curl "/collections/$c/snapshots/upload?wait=true&priority=snapshot&checksum=$sum" -X POST \
            -F "snapshot=@$f") || { echo "nạp snapshot $c lỗi: $r" >&2; exit 1; }
      echo "  $c: $r"
    done
    verify_state
    say "tiếp: CD vm1 (API đọc .env v3) → verify → bật cron VM2. Redis giữ nguyên (docs/deploy_v3.md mục 1b)."
    ;;
  verify-restore)
    verify_state
    ;;
  upload-media-store)
    # Máy dev: file media trong local store (SICT_LOCAL_STORE) → S3 với đúng key Storage dùng (media/<sha256>.<ext>).
    store=${SICT_LOCAL_STORE:-./_local_store}
    if [ "${CONFIRM_S3_UPLOAD:-}" != "yes" ]; then
      echo "Từ chối: upload S3 cần CONFIRM_S3_UPLOAD=yes (chỉ chạy khi chủ dự án đã cho phép)." >&2
      [ -d "$store/media" ] && echo "local store $store/media: $(find "$store/media" -type f | wc -l) file" >&2
      exit 2
    fi
    [ -d "$store/media" ] || { echo "không thấy $store/media (SICT_LOCAL_STORE=…)" >&2; exit 1; }
    SICT_LOCAL_STORE="$store" run uv run python - <<'PY'
"""Upload media local store → S3: liệt kê key đã có (list_objects_v2) rồi chỉ upload key thiếu, cuối cùng liệt kê lại."""
import os
import sys
from pathlib import Path

from sict_crawler.repository.storage import Storage

st = Storage()
if st.local:
    sys.exit("không có AWS credentials (AWS_PROFILE hoặc AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) → Storage ở chế độ "
             "local, không có gì để upload")
root = Path(os.environ["SICT_LOCAL_STORE"])
prefix = st.cfg.prefix_media
files = {p.relative_to(root).as_posix(): p for p in sorted((root / prefix).rglob("*")) if p.is_file()}


def remote() -> set[str]:
    keys: set[str] = set()
    for page in st.s3.get_paginator("list_objects_v2").paginate(Bucket=st.cfg.bucket, Prefix=prefix):
        keys.update(o["Key"] for o in page.get("Contents", []))
    return keys


have = remote()
todo = [k for k in files if k not in have]
print(f"bucket {st.cfg.bucket}: {len(files)} file local, {len(files) - len(todo)} đã có, upload {len(todo)}")
for i, k in enumerate(todo, 1):
    st.s3.upload_file(str(files[k]), st.cfg.bucket, k)
    if i % 200 == 0 or i == len(todo):
        print(f"  {i}/{len(todo)}")
missing = [k for k in files if k not in remote()]
if missing:
    sys.exit(f"{len(missing)} key vẫn thiếu trên S3, vd {missing[:5]} — chạy lại")
print(f"uploaded {len(todo)}; S3 có đủ {len(files)} key media của local store")
PY
    ;;
  pack)
    [ -d "$ART" ] || { echo "không thấy artifact $ART (ART=…)"; exit 1; }
    mkdir -p "$(dirname "$PKG")"
    run tar czf "$PKG" --exclude="$ART/ocr_cache.jsonl" "$ART" "$SNAP/html"
    ls -lh "$PKG"
    say "chép $PKG lên VM2 (~/craw-sict/, nơi chạy loader bằng image worker): scp $PKG ubuntu@<IP-VM2>:craw-sict/"
    say "trên VM2: tar xzf $(basename "$PKG") && ./scripts/cutover_v3.sh env-check vm2 && ./scripts/cutover_v3.sh load"
    ;;
  env-check)
    env_check "${2:-}"
    ;;
  backup)
    d=~/backup-v3-$(date +%Y%m%d-%H%M)
    mkdir -p "$d"
    say "pg_dump → $d/sict_crawler.dump"
    docker compose exec -T postgres pg_dump -U sict -Fc sict_crawler > "$d/sict_crawler.dump"
    say "snapshot Qdrant mọi collection đang có (v1 / v2 / v3 nếu đã nạp) → volume qdata (/qdrant/storage/snapshots)"
    cols="$(qdrant_curl /collections | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin)["result"]["collections"]))' 2>/dev/null || true)"
    [ -n "$cols" ] || echo "cảnh báo: không liệt kê được collection (Qdrant chưa chạy hoặc sai QDRANT_API_KEY trong $ENV_FILE)"
    for c in $cols; do
      qdrant_curl "/collections/$c/snapshots" -X POST -o "$d/qdrant-$c.json" \
        && echo "  snapshot $c: ok" || echo "cảnh báo: không tạo được snapshot Qdrant $c"
    done
    ls -lh "$d"
    say "kiểm: pg_restore --list $d/sict_crawler.dump | head; chép $d ra ngoài VM1 (máy dev / S3 khi được phép)"
    ;;
  load)
    # Chạy trên VM2: image worker (group graph: underthesea cho graphsync.rebuild mà loader gọi sau khi ghi point).
    # Không build trên VM (D10): image = WORKER_IMAGE trong .env do CD ghi (ghcr.io/…:<sha>); chưa có thì pull.
    [ -f "$PKG" ] && { say "giải nén $PKG"; run tar xzf "$PKG"; }
    [ -d "$ART" ] || { echo "không thấy artifact $ART (chạy pack ở máy dev rồi chép lên, hoặc ART=…)"; exit 1; }
    env_check vm2
    img="$(val WORKER_IMAGE)"
    if [ -z "$img" ]; then
      echo "WORKER_IMAGE chưa có trong $ENV_FILE (CD vm2 ghi vào, D10) — nạp bằng image local sict-worker:latest? Đặt WORKER_IMAGE=… trong $ENV_FILE để chắc chắn." >&2
      img=sict-worker:latest
    fi
    docker image inspect "$img" >/dev/null 2>&1 || run docker compose -f "$WORKER_COMPOSE" pull cron
    if docker compose -f "$WORKER_COMPOSE" ps --status running cron 2>/dev/null | grep -q cron; then
      if [ "${ALLOW_CRON_RUNNING:-}" != "yes" ]; then
        echo "cron đang chạy — dừng trước để không incremental song song với loader: docker compose -f $WORKER_COMPOSE stop cron (hoặc ALLOW_CRON_RUNNING=yes)" >&2
        exit 2
      fi
    fi
    say "nạp PG + Qdrant *_v3 bằng image worker ($img); loader so manifest với EMBED_*/QDRANT_* trong $ENV_FILE, lệch → SystemExit"
    say "thời gian ước lượng: nạp ~3 phút + context ảnh 1–2 phút + graph khái niệm lần đầu ~13 phút / 1 lõi"
    extra=()
    [ "${LOAD_RECREATE:-}" = "yes" ] && extra+=(--recreate)
    run docker compose -f "$WORKER_COMPOSE" run --rm --no-deps -v "$PWD/_snapshot:/app/_snapshot:ro" cron \
      python -m sict_crawler.pipeline.loader --artifacts "$ART" --skip-media-upload "${extra[@]}"
    say "kiểm: log loader 'ok: true', points = expected; rồi CD vm1 (API đọc .env v3) → verify → bật cron"
    ;;
  verify)
    say "golden_v2 (mong: url_hit ≥ 70/80)"
    run python3 scripts/eval_api.py --api "$API" --golden eval/golden_v2.jsonl
    say "list_v2 (mong: coverage ≥ 0,90, sufficient_acc ≥ 0,94)"
    run python3 scripts/eval_api.py --api "$API" --list eval/list_v2.jsonl
    say "nghiệm thu U1–U6 + trích dẫn + /metrics (exit 1 khi FAIL)"
    run python3 scripts/accept_api.py --api "$API"
    ;;
  upload-media)
    if [ "${CONFIRM_S3_UPLOAD:-}" != "yes" ]; then
      echo "Từ chối: upload S3 cần CONFIRM_S3_UPLOAD=yes (chỉ chạy khi chủ dự án đã cho phép)." >&2
      exit 2
    fi
    run python -m sict_crawler.pipeline.loader --artifacts "$ART" --snapshot "$SNAP" --media-only --upload-media
    ;;
  rollback)
    to=${ROLLBACK_TO:-v2}
    case "$to" in
      v2) ct=sict_text_v2; ci=sict_image_v2 ;;
      v1) ct=sict_text;    ci=sict_image ;;
      *) echo "ROLLBACK_TO=v2|v1"; exit 2 ;;
    esac
    cat <<EOF
Rollback API về index $to (không mất dữ liệu: collection $ct/$ci + cột cũ của Postgres còn nguyên; chỉ đổi cấu hình):
  1. VM1 $ENV_FILE: QDRANT_COLLECTION_TEXT=$ct  QDRANT_COLLECTION_IMAGE=$ci  QDRANT_COLLECTION_OCR=  (trống)
     QDRANT_TEXT_DIM=384  QDRANT_IMAGE_DIM=512  EMBED_BACKEND=torch  API_MEM_LIMIT=1500m (API tự nạp E5 + CLIP)
     → docker compose up -d api      (image giữ nguyên; code v3 đọc được collection cũ)
  2. VM2: docker compose -f $WORKER_COMPOSE stop cron   (worker v3 từ chối ghi collection v1; với v2 thì sửa .env VM2 giống bước 1)
  3. Image cũ (nếu cần): CD với tag cũ, hoặc API_IMAGE=\$API_IMAGE_PREV trong .env rồi docker compose up -d api.
  4. Postgres: bảng / cột v3 chỉ thêm mới, code cũ bỏ qua. Khôi phục hẳn: pg_restore bản dump ở bước backup.
Làm thật bước 1 ngay: CONFIRM_ROLLBACK=yes ROLLBACK_TO=$to ./scripts/cutover_v3.sh rollback
EOF
    [ "${CONFIRM_ROLLBACK:-}" = "yes" ] || exit 0
    upsert() { if grep -q "^$1=" "$ENV_FILE"; then sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi; }
    cp "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%d-%H%M)"
    upsert QDRANT_COLLECTION_TEXT "$ct"; upsert QDRANT_COLLECTION_IMAGE "$ci"; upsert QDRANT_COLLECTION_OCR ""
    upsert QDRANT_TEXT_DIM 384; upsert QDRANT_IMAGE_DIM 512; upsert EMBED_BACKEND torch; upsert API_MEM_LIMIT 1500m
    say "đã sửa $ENV_FILE (bản cũ: $ENV_FILE.bak-*) → recreate API"
    run docker compose up -d api
    for i in $(seq 1 30); do curl -sf -m 10 "$API/app.js" -o /dev/null && break; sleep 10; done
    run curl -sf -m 10 "$API/app.js" -o /dev/null && echo "API lên lại với index $to"
    ;;
  *)
    sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 1 ;;
esac
