"""Nghiệm thu hệ thống qua HTTP (sau deploy/cutover, cạnh scripts/eval_api.py): UC U1–U6, trích dẫn D24, media, /metrics.

Chỉ cần API đang chạy (stdlib; Pillow nếu có thì ảnh truy vấn được biến đổi để không trùng byte với ảnh trong corpus):
  U1 text→text      câu golden media_mode=none có expected_urls: bài đúng trong top 5
  U2 text→ảnh       câu golden media_mode=required: có media, media thuộc bài đúng; GET /media/s/{key} trả ảnh
  U3 ảnh→ảnh        ảnh lấy từ U2 (biến đổi): ảnh gốc hạng 1 hoặc có ảnh cùng bài
  U4 ảnh→text       cùng ảnh, câu hỏi rỗng: bài chứa ảnh trong top 3 đoạn chữ
  U5 text+ảnh→ảnh   "tìm thêm ảnh khác của sự kiện": có ảnh cùng bài
  U6 text+ảnh→text  "ảnh này thuộc sự kiện nào": bài chứa ảnh trong top 3
  S8 trích dẫn      /chat (SSE) có sự kiện citations; [n] trong câu trả lời đều hợp lệ; trả lời có nội dung thì ≥ 1 trích dẫn
  M  /metrics       có sict_http_requests_total, sict_pipeline_metrics_up 1

Usage: python3 scripts/accept_api.py --api http://localhost:8003 [--golden eval/golden_v1.jsonl] [--out f.json]
Thoát 1 nếu có ca FAIL.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import time
import urllib.request

UC_IMAGE_QUESTIONS = (("U5 text+ảnh→ảnh", "Tìm thêm ảnh khác của sự kiện trong ảnh này"),
                      ("U6 text+ảnh→text", "Ảnh này thuộc sự kiện nào, diễn ra khi nào?"))


def norm(u: str | None) -> str:
    return re.sub(r"^https?://", "", u or "").rstrip("/").lower()


def post(api: str, path: str, body: dict, timeout: int) -> tuple[float, bytes]:
    req = urllib.request.Request(api + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return time.time() - t0, r.read()


def get(api: str, path: str, timeout: int = 30) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(api + path, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def perturb(raw: bytes) -> bytes:
    """Cắt 85 %, thu 60 %, JPEG q65 (như thử int8 2026-10-20); không có Pillow thì gửi nguyên ảnh."""
    try:
        from PIL import Image
    except ImportError:
        return raw
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    im = im.crop((int(w * .075), int(h * .075), int(w * .925), int(h * .925)))
    im = im.resize((max(64, int(im.width * .6)), max(64, int(im.height * .6))))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=65)
    return buf.getvalue()


def doc_of(s: dict) -> str:
    return norm(s.get("parent_url") or s.get("url"))


def rank(items: list[dict], pred) -> int | None:
    return next((i + 1 for i, s in enumerate(items) if pred(s)), None)


def check_citations(answer: str, sources: list[dict], cites: list[dict] | None) -> tuple[bool, str]:
    used = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    bad = sorted(n for n in used if not 1 <= n <= len(sources))
    if cites is None:
        return False, "thiếu sự kiện citations"
    if bad:
        return False, f"[n] ảo {bad} (có {len(sources)} nguồn)"
    if sources and answer.strip() and not used and not cites:
        return False, "trả lời có nội dung nhưng không có trích dẫn"
    return True, f"{len(used)} trích dẫn trong câu, {len(cites)} trong sự kiện citations"


def chat(api: str, question: str, timeout: int) -> tuple[float, str, list[dict], list[dict] | None]:
    req = urllib.request.Request(api + "/chat", data=json.dumps({"question": question, "top_k": 8}).encode(),
                                 headers={"Content-Type": "application/json"})
    t0, answer, sources, cites = time.time(), "", [], None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            line = line.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ev = json.loads(line[6:])
            if "text" in ev and "media" in ev:
                sources = ev["text"] + ev["media"]
            elif "delta" in ev:
                answer += ev["delta"]
            elif "answer" in ev:
                answer = ev["answer"]
            elif "citations" in ev:
                cites = ev["citations"]
    return time.time() - t0, answer, sources, cites


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8003")
    ap.add_argument("--golden", default="eval/golden_v1.jsonl")
    ap.add_argument("--n-text", type=int, default=6, help="số câu U1 (eval_api.py chạy đủ 70 câu)")
    ap.add_argument("--n-chat", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    gold = [json.loads(x) for x in open(a.golden, encoding="utf-8") if x.strip()]
    rows: list[dict] = []

    def record(uc: str, ok: bool, sec: float, detail: str, ref: str = "") -> None:
        rows.append({"uc": uc, "ok": ok, "sec": round(sec, 2), "ref": ref, "detail": detail})
        print(f"{'OK  ' if ok else 'FAIL'} {uc:18s} {sec:6.1f}s  {ref:5s} {detail}", flush=True)

    def query(q: str, image: bytes | None = None) -> tuple[float, dict]:
        body = {"question": q, "top_k": 8}
        if image:
            body["image"] = base64.b64encode(image).decode()
        sec, raw = post(a.api, "/query", body, a.timeout)
        return sec, json.loads(raw)

    for g in [g for g in gold if g["media_mode"] == "none" and g.get("expected_urls")][:a.n_text]:
        exp = {norm(u) for u in g["expected_urls"]}
        sec, res = query(g["question"])
        r = rank(res["text"], lambda s: norm(s["url"]) in exp or doc_of(s) in exp)
        cites_ok = all(1 <= c["n"] <= len(res["text"]) + len(res["media"]) for c in res.get("citations") or [])
        record("U1 text→text", bool(r and r <= 5) and cites_ok, sec, f"bài đúng @{r}, citations hợp lệ={cites_ok}", g["id"])

    for g in [g for g in gold if g["media_mode"] == "required" and g.get("expected_urls")]:
        exp = {norm(u) for u in g["expected_urls"]}
        sec, res = query(g["question"])
        media = [m for m in res["media"] if m.get("media_key")]
        good = [m for m in media if doc_of(m) in exp or norm(m["url"]) in exp]
        record("U2 text→ảnh", bool(good), sec, f"{len(media)} media, {len(good)} thuộc bài đúng, "
               f"mode={res['media_mode']}", g["id"])
        if not good or good[0].get("kind") == "video_frame":
            continue
        m = good[0]
        status, ctype, raw = get(a.api, f"/media/s/{m['media_key']}")
        record("U2 GET media", status == 200 and ctype.startswith("image/") and len(raw) > 500, 0,
               f"{status} {ctype} {len(raw)} byte", g["id"])
        img, key, doc = perturb(raw), m["media_key"], doc_of(m)
        sec, res = query("", img)
        ri, rd = rank(res["media"], lambda s: s.get("media_key") == key), rank(res["media"], lambda s: doc_of(s) == doc)
        record("U3 ảnh→ảnh", ri == 1 or bool(rd), sec, f"ảnh gốc @{ri}, ảnh cùng bài @{rd}", g["id"])
        rt = rank(res["text"], lambda s: doc_of(s) == doc)
        record("U4 ảnh→text", bool(rt and rt <= 3), sec, f"bài chứa ảnh @{rt} (đoạn chữ)", g["id"])
        for uc, q in UC_IMAGE_QUESTIONS:
            sec, res = query(q, img)
            if "→ảnh" in uc:
                rd = rank(res["media"], lambda s: doc_of(s) == doc)
                record(uc, bool(rd), sec, f"ảnh cùng bài @{rd} ({len(res['media'])} ảnh)", g["id"])
            else:
                rt = rank(res["text"], lambda s: doc_of(s) == doc)
                record(uc, bool(rt and rt <= 3), sec, f"bài chứa ảnh @{rt}", g["id"])

    chat_q = [g for g in gold if g.get("expected_urls")][:: max(1, len(gold) // max(1, a.n_chat))][:a.n_chat]
    chat_q.append({"id": "noinfo", "question": "Giá vé máy bay Hà Nội đi Tokyo hôm nay là bao nhiêu?"})
    for g in chat_q:
        sec, answer, sources, cites = chat(a.api, g["question"], a.timeout)
        ok, detail = check_citations(answer, sources, cites)
        record("S8 trích dẫn /chat", ok, sec, f"{detail}; {len(answer)} ký tự", g["id"])

    _, _, raw = get(a.api, "/metrics")
    text = raw.decode()
    ok = "sict_http_requests_total{" in text and re.search(r"^sict_pipeline_metrics_up 1", text, re.M) is not None
    record("M /metrics", ok, 0, "có HTTP counter + pipeline up" if ok else "thiếu metric")

    n_ok = sum(r["ok"] for r in rows)
    print(f"\nĐẠT {n_ok}/{len(rows)}")
    if a.out:
        json.dump({"api": a.api, "rows": rows, "ok": n_ok, "total": len(rows)}, open(a.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return 0 if n_ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
