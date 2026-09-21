"""Probe chất lượng response định kỳ (không cần LLM key, chỉ đo retrieval).

Gọi POST /query với bộ câu hỏi vàng (4 personas), ghi metrics ra JSONL:
  latency, attempts, sufficient, n_text, n_media, top scores, empty.

Chạy tay:  python3 scripts/quality_probe.py [--api URL] [--top-k N]
Cron VM1 (30p): */30 * * * * /usr/bin/python3 /home/ubuntu/craw-sict/scripts/quality_probe.py >>/home/ubuntu/quality-probe.log 2>&1
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.request

GOLDEN = [
    # (persona, question) — phủ 4 nhóm user trong test plan
    ("teacher", "Quy chế đào tạo tín chỉ hiện hành có gì?"),
    ("teacher", "Danh sách giảng viên khoa Công nghệ thông tin?"),
    ("teacher", "Kế hoạch giảng dạy học kỳ này?"),
    ("stranger", "Trường SICT là gì, ở đâu?"),
    ("stranger", "Cơ sở vật chất trường thế nào?"),
    ("stranger", "Thư viện ảnh của trường?"),
    ("parent", "Tuyển sinh 2025 có ngành gì?"),
    ("parent", "Học phí bao nhiêu?"),
    ("parent", "Học bổng cho tân sinh viên?"),
    ("parent", "Truyền thống nhà trường?"),
    ("student", "Ngành An toàn thông tin học những gì?"),
    ("student", "Điểm chuẩn năm ngoái?"),
    ("student", "Câu lạc bộ sinh viên có gì?"),
    ("student", "Video giới thiệu trường?"),
]

# Ngưỡng cảnh báo (đỏ khi vượt)
GATES = {
    "empty_rate": 0.25,      # >25% câu hỏi trả rỗng → báo
    "sufficient_rate": 0.60,  # <60% sufficient → báo
    "p50_latency_s": 30.0,   # p50 cold >30s → báo (warm phải <2s)
}


def ask(api: str, question: str, top_k: int, timeout: int = 150) -> dict:
    body = json.dumps({"question": question, "top_k": top_k}).encode()
    req = urllib.request.Request(api.rstrip("/") + "/query", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        dt_s = time.time() - t0
        texts, media = d.get("text", []), d.get("media", [])
        return {
            "ok": True, "latency_s": round(dt_s, 2),
            "attempts": d.get("attempts", 1),
            "sufficient": d.get("sufficient", True),
            "n_text": len(texts), "n_media": len(media),
            "top_text": round(texts[0]["score"], 4) if texts else 0.0,
            "top_media": round(media[0]["score"], 4) if media else 0.0,
            "empty": not texts and not media,
        }
    except Exception as e:
        return {"ok": False, "latency_s": round(time.time() - t0, 2),
                "error": str(e)[:200], "empty": True,
                "attempts": 0, "sufficient": False,
                "n_text": 0, "n_media": 0, "top_text": 0.0, "top_media": 0.0}


def main() -> int:
    api = "http://localhost:8003"
    top_k = 5
    args = sys.argv[1:]
    if "--api" in args:
        api = args[args.index("--api") + 1]
    if "--top-k" in args:
        top_k = int(args[args.index("--top-k") + 1])
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    for persona, q in GOLDEN:
        m = ask(api, q, top_k)
        rows.append({"ts": ts, "persona": persona, "question": q, **m})
        print(f"[{persona}] {m.get('latency_s')}s att={m.get('attempts')} "
              f"suf={m.get('sufficient')} t={m.get('n_text')}/{m.get('top_text')} "
              f"m={m.get('n_media')}/{m.get('top_media')} :: {q[:50]}",
              flush=True)
    n = len(rows)
    empty_rate = sum(1 for r in rows if r["empty"]) / n
    suf_rate = sum(1 for r in rows if r.get("sufficient")) / n
    lats = sorted(r["latency_s"] for r in rows if r.get("ok"))
    p50 = lats[len(lats) // 2] if lats else -1
    summary = {"ts": ts, "n": n, "empty_rate": round(empty_rate, 3),
               "sufficient_rate": round(suf_rate, 3), "p50_latency_s": p50,
               "alerts": [
                   a for a, bad in [
                       ("EMPTY_RATE_HIGH", empty_rate > GATES["empty_rate"]),
                       ("SUFFICIENT_LOW", suf_rate < GATES["sufficient_rate"]),
                       ("LATENCY_HIGH", p50 < 0 or p50 > GATES["p50_latency_s"]),
                   ] if bad]}
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if summary["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
