"""Đánh giá RAG bằng RAGAS qua HTTP: câu trả lời thật của /chat + ngữ cảnh LLM nhận (/query `context`).

Hai bước, đều chạy tiếp được (bỏ qua id đã có trong file):
  1. collect — mỗi câu hỏi gọi /query (ngữ cảnh, nguồn) rồi /chat (câu trả lời) → <out>.answers.jsonl
  2. score   — chấm RAGAS bằng LLM (OpenAI-compatible) → <out>.scores.jsonl + bảng tổng kết

Chỉ số (ragas.metrics.collections):
  faithfulness         câu trả lời có bám ngữ cảnh không (bịa = thấp)
  answer_relevancy     câu trả lời có trả lời đúng câu hỏi không (cần embedding: dịch vụ embed :8090)
  context_precision    nguồn liên quan có đứng đầu không (không cần đáp án, 1 lượt LLM / nguồn)
  context_recall       ngữ cảnh có đủ các ý của đáp án không — chỉ bộ liệt kê (expected_items làm đáp án)

LLM chấm mặc định là LLM_* trong .env (cùng model trả lời → điểm có thiên vị tự chấm; đổi bằng RAGAS_LLM_*).
Model miễn phí kiraai giới hạn 20 lượt / phút cho cả API lẫn chấm → --rpm (mặc định 15) và chạy 2 bước tuần tự.

  uv run --group eval python scripts/eval_ragas.py collect --out eval/results/ragas/2026-10-26
  uv run --group eval python scripts/eval_ragas.py score --out eval/results/ragas/2026-10-26
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SEP = "\n\n---\n\n"           # sources.build_context nối các phần bằng chuỗi này
ECHO = "(LLM chưa trả lời"   # routes./chat: LLM lỗi / hết lượt → trả lại ngữ cảnh


def load_env(path: str) -> dict:
    env = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return {**env, **os.environ}


def questions(args) -> list[dict]:
    rows = []
    for path, kind in ((args.golden, "golden"), (args.list, "list")):
        if not path:
            continue
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            g = json.loads(line)
            if kind == "list" and g.get("kind") == "out_of_scope":
                continue   # không có đáp án để chấm
            ref = None
            if g.get("expected_items"):
                ref = "Các mục cần có: " + "; ".join(alts[0] for alts in g["expected_items"]) + "."
            rows.append({"id": f"{kind}:{g.get('id', len(rows))}", "set": kind, "question": g["question"],
                         "filters": g.get("filters"), "reference": ref})
    return rows[:args.limit] if args.limit else rows


def done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(x)["id"] for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


def post(api: str, path: str, body: dict, timeout: int):
    req = urllib.request.Request(f"{api}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def chat(api: str, q: dict, top_k: int, timeout: int) -> str:
    deltas, answer = [], None
    with post(api, "/chat", {"question": q["question"], "top_k": top_k, "filters": q["filters"]}, timeout) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])
            if "delta" in d:
                deltas.append(d["delta"])
            if "answer" in d:
                answer = d["answer"]
    return answer or "".join(deltas)


def collect(args) -> None:
    out = Path(f"{args.out}.answers.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    have = done_ids(out)
    gap = 60.0 / args.rpm
    for q in questions(args):
        if q["id"] in have:
            continue
        for attempt in range(4):
            t = time.time()
            with post(args.api, "/query", {"question": q["question"], "top_k": args.top_k,
                                           "filters": q["filters"]}, args.timeout) as r:
                res = json.loads(r.read())
            ans = chat(args.api, q, args.top_k, args.timeout)
            if not ans.startswith(ECHO):
                break
            print(f"{q['id']} LLM không trả lời (hết lượt?), chờ 60 s", flush=True)
            time.sleep(60)
        else:
            print(f"{q['id']} bỏ qua: LLM không trả lời sau 4 lần", flush=True)
            continue
        contexts = [c for c in res.get("context", "").split(SEP) if c.strip()]
        row = {**q, "answer": ans, "contexts": contexts, "sufficient": res.get("sufficient"),
               "list_question": res.get("list_question"), "urls": [s["url"] for s in res.get("text", [])]}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{q['id']} {time.time() - t:.1f}s nguồn={len(contexts)} trả lời={len(ans)} ký tự", flush=True)
        time.sleep(max(0.0, gap - (time.time() - t)))   # /chat tốn 1 lượt LLM của cùng hạn mức


class RateLimiter:
    """≤ rpm lượt / phút (cửa sổ trượt 60 s) cho mọi lượt gọi LLM chấm."""

    def __init__(self, rpm: int):
        self.rpm, self.ts, self.lock = rpm, [], asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                self.ts = [t for t in self.ts if now - t < 60]
                if len(self.ts) < self.rpm:
                    self.ts.append(now)
                    return
                await asyncio.sleep(60 - (now - self.ts[0]) + 0.05)


STATS = {"calls": 0, "429": 0, "errors": 0, "llm_s": 0.0}   # lượt gọi LLM chấm (in sau mỗi câu)


def throttled_client(base: str, key: str, rpm: int):
    """AsyncOpenAI có giới hạn tốc độ + chờ lại khi 429 (thông báo kiraai: "Vui lòng đợi N giây")."""
    from openai import AsyncOpenAI, RateLimitError
    cli = AsyncOpenAI(base_url=base, api_key=key, max_retries=0, timeout=180)
    create, limiter = cli.chat.completions.create, RateLimiter(rpm)

    async def create_throttled(*a, **kw):
        for _ in range(8):
            await limiter.wait()
            t = time.monotonic()
            try:
                r = await create(*a, **kw)
                STATS["calls"] += 1
                STATS["llm_s"] += time.monotonic() - t
                return r
            except RateLimitError as e:
                STATS["429"] += 1
                m = re.search(r"(\d+)\s*giây", str(e))
                await asyncio.sleep(int(m.group(1)) + 1 if m else 30)
            except Exception:
                STATS["errors"] += 1
                raise
        return await create(*a, **kw)
    cli.chat.completions.create = create_throttled   # gán trước llm_factory: instructor bọc hàm này
    return cli


def make_embeddings(base: str, model: str):
    """Embedding cho answer_relevancy qua dịch vụ embed tự host (Jina-style, nhận chuỗi thuần)."""
    import httpx
    from ragas.embeddings.base import BaseRagasEmbedding

    class ServiceEmbedding(BaseRagasEmbedding):
        def _body(self, texts):
            return {"model": model, "input": [{"text": t} for t in texts], "normalized": True}

        def embed_text(self, text: str, **kw) -> list[float]:
            return self.embed_texts([text])[0]

        def embed_texts(self, texts: list[str], **kw) -> list[list[float]]:
            r = httpx.post(f"{base}/embeddings", json=self._body(texts), timeout=120)
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]

        async def aembed_text(self, text: str, **kw) -> list[float]:
            return (await self.aembed_texts([text]))[0]

        async def aembed_texts(self, texts: list[str], **kw) -> list[list[float]]:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(f"{base}/embeddings", json=self._body(texts))
                r.raise_for_status()
                return [d["embedding"] for d in r.json()["data"]]
    return ServiceEmbedding()


async def score_all(args) -> None:
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecisionWithoutReference, ContextRecall, Faithfulness
    env = load_env(args.env_file)
    base = env.get("RAGAS_LLM_BASE") or env["LLM_API_BASE"]
    model = env.get("RAGAS_LLM_MODEL") or env["LLM_MODEL"]
    llm = llm_factory(model, client=throttled_client(base, env.get("RAGAS_LLM_KEY") or env["LLM_API_KEY"], args.rpm),
                      max_tokens=args.max_tokens)
    emb = make_embeddings(args.embed_base, env.get("EMBED_MODEL", "jina-clip-v2"))
    metrics = {"faithfulness": Faithfulness(llm=llm), "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=emb),
               "context_precision": ContextPrecisionWithoutReference(llm=llm), "context_recall": ContextRecall(llm=llm)}
    use = [m for m in args.metrics.split(",") if m]
    rows = [json.loads(x) for x in Path(f"{args.out}.answers.jsonl").read_text(encoding="utf-8").splitlines() if x]
    out = Path(f"{args.out}.scores.jsonl")
    have = done_ids(out)
    print(f"chấm {len(rows) - len(have & {r['id'] for r in rows})}/{len(rows)} câu, LLM chấm {model}", flush=True)

    async def one(r):
        res = {}
        for name in use:
            m = metrics[name]
            try:
                if name == "faithfulness":
                    v = await m.ascore(user_input=r["question"], response=r["answer"], retrieved_contexts=r["contexts"])
                elif name == "answer_relevancy":
                    v = await m.ascore(user_input=r["question"], response=r["answer"])
                elif name == "context_precision":
                    v = await m.ascore(user_input=r["question"], response=r["answer"],
                                       retrieved_contexts=r["contexts"])
                elif r.get("reference"):   # context_recall: chỉ câu có đáp án (bộ liệt kê)
                    v = await m.ascore(user_input=r["question"], retrieved_contexts=r["contexts"],
                                       reference=r["reference"])
                else:
                    continue
                res[name] = round(float(v.value), 4)
            except Exception as e:   # 1 chỉ số lỗi không làm hỏng cả câu; ghi lại để biết
                res[name] = None
                res.setdefault("errors", {})[name] = f"{type(e).__name__}: {str(e)[:200]}"
        row = {"id": r["id"], "set": r["set"], "question": r["question"], **res}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False)[:300], flush=True)
        print(f"  LLM: {STATS['calls']} lượt, {STATS['429']} lần 429, {STATS['errors']} lỗi, "
              f"trung bình {STATS['llm_s'] / max(STATS['calls'], 1):.1f} s / lượt", flush=True)

    sem = asyncio.Semaphore(args.concurrency)

    async def guarded(r):
        async with sem:
            await one(r)
    await asyncio.gather(*(guarded(r) for r in rows if r["id"] not in have))
    summary(out, use)


def summary(path: Path, use: list[str]) -> None:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    table = {}
    for s in ("golden", "list", "all"):
        part = [r for r in rows if s == "all" or r["set"] == s]
        if not part:
            continue
        table[s] = {"n": len(part)}
        for m in use:
            vals = [r[m] for r in part if r.get(m) is not None]
            if vals:
                table[s][m] = round(sum(vals) / len(vals), 3)
                table[s][f"{m}_n"] = len(vals)
    print(json.dumps(table, ensure_ascii=False, indent=1))
    Path(str(path).replace(".scores.jsonl", ".summary.json")).write_text(
        json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["collect", "score", "summary"])
    ap.add_argument("--out", required=True, help="tiền tố file kết quả (VD eval/results/ragas/2026-10-26)")
    ap.add_argument("--api", default="http://localhost:8003")
    ap.add_argument("--golden", default="eval/golden_v1.jsonl")
    ap.add_argument("--list", default="eval/list_v1.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--rpm", type=int, default=15, help="trần lượt gọi LLM / phút (kiraai miễn phí: 20)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--metrics", default="faithfulness,answer_relevancy,context_precision,context_recall")
    ap.add_argument("--embed-base", default="http://localhost:8090/v1")
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args()
    if args.step == "collect":
        collect(args)
    elif args.step == "score":
        asyncio.run(score_all(args))
    else:
        summary(Path(f"{args.out}.scores.jsonl"), [m for m in args.metrics.split(",") if m])


if __name__ == "__main__":
    sys.exit(main())
