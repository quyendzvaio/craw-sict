# AGENTS.md

Multi-modal RAG crawler for `https://sict.haui.edu.vn/vn` (crawl → PG + Qdrant + S3 → RAG API + chat UI).

**Read `CLAUDE.md` first** — it is the detailed, maintained architecture/rules doc. `plan.md` holds
owner decisions D1–D24 and is the source of truth when docs and code disagree. Code comments, docs and
commit messages are mostly Vietnamese; keep that convention.

## Hard rules (do not violate)

- **S3 / VM freeze (owner, 2026-10-14):** never upload to S3 and never run anything on the VMs
  (CD workflow, `scripts/cutover_v2.sh` VM steps, `loader --upload-media`) unless the owner explicitly
  says so. `upload-media` also requires `CONFIRM_S3_UPLOAD=yes`. Local runs use the local store (no AWS env).
- **uv only.** Never use `pip` and never add `requirements.txt`. Deps live in `pyproject.toml`
  dependency groups: `dev` (pytest), `api` (FastAPI), `ml` (torch/sentence-transformers/OCR/yt-dlp/playwright).
- **Never run `playwright install`** — playwright is a CDP client for the Obscura sidecar only.
- **Graph is rules-only.** LLM entity extraction (`graph_llm.py`) was removed 2026-10-18; do not reintroduce.
- No linter/formatter/typechecker is configured. CI only runs pytest. Do not assume `ruff`/`mypy` commands exist.
- Never commit secrets (`.env`, `cookies/*.txt`, `*.pem`) or data dirs (`.pgdata*`, `_snapshot/`, `_local_store/`, `.hf-cache/`).
- **Never commit without the owner saying so.** The working tree carries an uncommitted v3 batch (~89 modified/untracked
  files incl. `AGENTS.md`, `CLAUDE.md`); do not revert or stash it. Owner splits it into commits when ready.
- Never copy review data into the repo: `/DATA/craw-sict-data/review/` (entity review CSV) is deliberately outside the
  repo because it contains student names (PII). `sict_crawler/graph_catalog.yaml` is a draft that needs owner review.

## In-flight: v3 multimodal migration (uncommitted; index v3 not built yet)

- Replaces the two v2 models (E5 384-d text + CLIP 512-d image) with **one multimodal `jina-clip-v2` (1024-d)**;
  new index `sict_text_v3`/`sict_image_v3`. **v2 is live and must not be touched** — keep it for rollback.
- `embedder.py` has two backends via `embedding.backend`/`EMBED_BACKEND`: `torch` (v2) and `api` (v3: Jina cloud now;
  a self-hosted ONNX int8 service behind the same interface is planned but **not implemented**). `api` env:
  `EMBED_API_BASE/KEY/MODEL/DIM/CACHE_PATH/WORKERS/TPM/IMAGE_BATCH`; vectors are cached in sqlite by input hash.
- The v3 embedding run is blocked on the **Jina API key balance** (`AUTHZ_INSUFFICIENT_BALANCE`). Reruns resume from
  the cache. Never run the full compute before the owner agrees — `scripts/embed_pilot.py` samples and estimates tokens.
- Switch the API to v3 only after eval passes: `QDRANT_COLLECTION_TEXT/IMAGE=*_v3`,
  `QDRANT_TEXT_DIM=QDRANT_IMAGE_DIM=EMBED_DIM`, `EMBED_BACKEND=api`, `CHUNK_STRUCTURED=1`. Keep v2 to roll back.
- Router thresholds `t2i_min`/`t2c_min`/`i2i_min`/`i2t_min` (`config.py`, D21) are **provisional**, to be calibrated on
  the gold set. v2's E5/CLIP captions are not re-embedded after the new OCR, so v2 drifts slightly; v3 fixes it.

## Commands

```bash
uv sync --group dev --group api                  # add --group ml for embedding/OCR/video
docker compose up -d postgres redis qdrant        # local infra
uv run uvicorn sict_crawler.api:app --port 8000   # API + chat UI at /
uv run pytest sict_crawler/tests/ -q --ignore=sict_crawler/tests/smoke_incremental.py   # same as CI
uv run pytest sict_crawler/tests/test_safety.py::test_mass_delete_guard                 # single test
uv run python -m sict_crawler.tests.test_safety   # 3 tests also run as scripts in CI (also test_captions, test_agent)
```

Pipeline CLI (`python -m sict_crawler.main`, one mode per invocation): `--crawl-limit N`, `--full`,
`--incremental --check-edits N --crawl-limit N --annotate-ocr-n N`, `--refresh`, `--reset`.

Bulk (re)index path: `snapshot --out _snapshot/<date>` → `compute --snapshot S [--stages ...]` →
`loader --artifacts S/artifacts/v2-multilingual-e5-small`. Also `reconcile [--dry-run]` and
`graphsync rebuild|stats|review-export|review-import|block|alias`.

## Gotchas an agent will miss

- `.venv` has **no ML group**, so anything that embeds (live-Qdrant eval, `smoke_incremental`) can only run
  in the `sict-api:latest` / `sict-worker:gpu` images (`--network host`, repo mounted over `/app`, `HF_HOME=./.hf-cache`).
- `smoke_incremental.py` is excluded from CI, needs live PG + Qdrant `*_v2`, and writes to whatever DB it
  points at — dev only.
- Config loads `config.yaml` (override with `SICT_CONFIG`); env vars override yaml on deploy. Always call
  `embedder.get_device()`, never read `embedding.device` directly.
- Default collections are `*_v2`; `loader.check` accepts `_v2`/`_v3` and matches backend/model/dim/prefix against the
  artifact manifest. Only HTTP 404/410 deletes a doc, and a mass-delete guard aborts past thresholds.
  `--rechunk`, `--videos-backfill`, `--annotate-ocr` are v1-only and refuse v2/v3 collections.
- Eval before touching models/thresholds/chunking: `eval/golden_v1.jsonl` (70 q) with `scripts/eval_retrieval.py`
  (offline) / `eval_api.py` (over HTTP), `scripts/eval_graph.py` on `eval/graph_gold_v1.jsonl`. `scripts/eval_multimodal.py`
  does not exist yet; the BM25/jina/bge-m3 12-question comparison is `scripts/eval_compare.py` → `eval/results/COMPARE.md`.
- Point ids are deterministic uuid5, so re-index/loader overwrite instead of duplicating.
- Schema changes go through `db.init_db()` `create_all` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  (no Alembic yet); add new columns there.
- `test_behavior_contract.py` / `test_retrieval_response_contract.py` are spec tests derived from
  `tests/acceptance_*.md` (stdlib `unittest`); they encode policy and do not import the real code.
- Parser changes must ship with real-HTML fixtures in `sict_crawler/tests/fixtures/` (`test_parser_theme.py`).

## Architecture map

Ingest: `frontier` (Redis BFS) → `crawler.fetch` (httpx → Obscura → crawl4ai via `CRAWLER_BACKEND`) →
`parser` → `chunker` → `embedder` → `vector_store` + `db`/`models` + `storage`.
`indexer.index_page` is the single v2 write path (idempotent via `hash_v2`/`hash_struct`); `incremental.py`
reuses it. Query: `api.py` `/query` + `/chat` (SSE) → `agent.run` (rewrite → RRF dense+BM25 → optional LLM
judge → answer), `router.py` media intent, `filters.py` metadata, `graph.py`/`graphsync.py` entity graph
(PG is source of truth). `compute.py`+`indexing.py` are the shared rule set for bulk and incremental.
