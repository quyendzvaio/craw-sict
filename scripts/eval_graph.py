"""Chấm điểm trích xuất graph (D20) trên bộ vàng tự chứa — precision / recall / F1 theo loại.

  --pred rules   luật hiện tại (graph.py) chạy trên văn bản lưu trong bộ vàng; danh bạ: PG nếu kết
                 nối được (giống production), không thì dựng từ chính các tài liệu trong bộ vàng
  --pred llm     nhãn `draft.llm` trong bộ vàng (gán độc lập) / kết quả giai đoạn C
  --pred FILE    JSONL {"url", "entities": [...], "media": [...]}
Tham chiếu: dòng `status == "reviewed"` (`entities`, `media`). `--ref llm` dùng tạm draft.llm làm
tham chiếu (CHỈ để ước lượng trước khi có nhãn người — không phải bộ vàng).

So khớp: người theo khoá giữ dấu (graph.person_key); sự kiện/CLB theo chuỗi bỏ dấu + bỏ năm, chứa trọn
nhau (≥ 2 từ) cũng tính; ảnh theo cặp (sha256, thực thể); vai trò trên thực thể đã khớp.

Usage: python scripts/eval_graph.py --gold eval/graph_gold_v1.jsonl --pred rules [--errors 20]
Callers: CLI, tests/test_eval_graph.py (CI, bộ nhỏ gán tay).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sict_crawler.service.graph import graph as G  # noqa: E402

KINDS = ("person", "event", "org")


def norm(kind: str, name: str) -> str:
    if kind == "person":
        return G.person_key(name)
    k = G._clean_key(name)
    return k[4:] if kind == "org" and k.startswith("clb ") else k


def same(kind: str, a: str, b: str) -> bool:
    if a == b:
        return True
    if kind == "person" or not a or not b:
        return False
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < 2 and kind != "org":
        return False
    n = len(short)
    return any(long_[i:i + n] == short for i in range(len(long_) - n + 1))


def match(gold: list[dict], pred: list[dict], kind: str):
    """Ghép 1–1 tham lam → (tp, fp, fn, cặp đã ghép, fp list, fn list)."""
    g = [(x, norm(kind, x["name"])) for x in gold if x["type"] == kind]
    p = [(x, norm(kind, x["name"])) for x in pred if x["type"] == kind]
    used, pairs = set(), []
    for gx, gk in g:
        for j, (px, pk) in enumerate(p):
            if j not in used and same(kind, gk, pk):
                used.add(j)
                pairs.append((gx, px))
                break
    fn = [gx for gx, gk in g if all(gx is not a for a, _ in pairs)]
    fp = [px for j, (px, _) in enumerate(p) if j not in used]
    return len(pairs), len(fp), len(fn), pairs, fp, fn


def media_pairs(item_media: list[dict], kind_of: dict[str, str]) -> set[tuple[str, str]]:
    out = set()
    for m in item_media or []:
        for name in m.get("entities", []):
            k = kind_of.get(G.fold(name), "person")
            out.add((m["sha256"], norm(k, name)))
    return out


def media_match(gm: set[tuple[str, str]], pm: set[tuple[str, str]], kind_of: dict[str, str]) -> int:
    """Cặp (ảnh, thực thể) khớp 1–1 theo cùng luật `same` của thực thể (tên chuẩn "Đội Hỗ trợ kỹ thuật IT
    Supporter" khớp nhãn "IT Supporter"), không đòi chuỗi y hệt."""
    kinds = {norm(k, n): k for n, k in kind_of.items()}
    used, tp = set(), 0
    for sha, g in sorted(gm):
        k = kinds.get(g, "person")
        hit = next((p for p in sorted(pm) if p not in used and p[0] == sha
                    and (p[1] == g or same(k, g, p[1]))), None)
        if hit is not None:
            used.add(hit)
            tp += 1
    return tp


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3),
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0, "tp": tp, "fp": fp, "fn": fn}


def rules_gazetteer(items: list[dict], source: str = "auto") -> G.Gazetteer:
    if source == "items":
        return _items_gazetteer(items)
    try:
        import asyncio

        from sict_crawler.repository import db
        from sict_crawler.service.graph import graphsync

        async def load():
            async with db.get_session() as s:
                return await asyncio.wait_for(graphsync.load_gazetteer(s), timeout=10)
        gaz = asyncio.run(load())
        if len(gaz):
            return gaz
    except Exception:
        pass
    return _items_gazetteer(items)


def _items_gazetteer(items: list[dict]) -> G.Gazetteer:
    ms = [G.doc_mentions({"title": it["title"], "section": it["section"], "doc_type": it["doc_type"]},
                         it["body"], [i.get("alt", "") for i in it.get("images", [])],
                         [i.get("context", "") for i in it.get("images", [])],
                         [i.get("ocr", "") for i in it.get("images", [])]) for it in items]
    return G.build_gazetteer(ms)


def predictions(items: list[dict], mode: str, gaz_source: str = "auto") -> dict[str, dict]:
    if mode == "rules":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from build_graph_gold import predict_rules
        gaz = rules_gazetteer(items, gaz_source)
        return {it["url"]: predict_rules(it, gaz) for it in items}
    if mode == "llm":
        return {it["url"]: (it.get("draft") or {}).get("llm") or {"entities": [], "media": []} for it in items}
    rows = [json.loads(x) for x in Path(mode).read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["url"]: r for r in rows}


def evaluate(items: list[dict], preds: dict[str, dict], ref: str = "reviewed", errors: int = 0) -> dict:
    tot = defaultdict(lambda: [0, 0, 0])
    roles = [0, 0]
    errs = []
    used = 0
    for it in items:
        if ref == "reviewed":
            if it.get("status") != "reviewed":
                continue
            gold_e, gold_m = it.get("entities", []), it.get("media", [])
        else:
            d = (it.get("draft") or {}).get(ref) or {}
            gold_e, gold_m = d.get("entities", []), d.get("media", [])
        used += 1
        pr = preds.get(it["url"], {"entities": [], "media": []})
        for kind in KINDS:
            tp, fp, fn, pairs, fps, fns = match(gold_e, pr.get("entities", []), kind)
            for i, v in enumerate((tp, fp, fn)):
                tot[kind][i] += v
            for gx, px in pairs:
                if gx.get("roles") and px.get("roles") is not None:
                    roles[1] += 1
                    roles[0] += bool(set(gx["roles"]) & set(px["roles"]))
            if errors and (fps or fns):
                errs.append({"url": it["url"], "kind": kind, "missed": [x["name"] for x in fns],
                             "spurious": [x["name"] for x in fps]})
        kind_of = {G.fold(x["name"]): x["type"] for x in [*gold_e, *pr.get("entities", [])]}
        gm, pm = media_pairs(gold_m, kind_of), media_pairs(pr.get("media", []), kind_of)
        tp = media_match(gm, pm, kind_of)
        tot["media"][0] += tp
        tot["media"][1] += len(pm) - tp
        tot["media"][2] += len(gm) - tp
    out = {"docs": used, **{k: prf(*v) for k, v in tot.items()}}
    all_ = [sum(tot[k][i] for k in KINDS) for i in range(3)]
    out["all_entities"] = prf(*all_)
    if roles[1]:
        out["roles"] = {"accuracy": round(roles[0] / roles[1], 3), "n": roles[1]}
    if errors:
        out["errors"] = errs[:errors]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="eval/graph_gold_v1.jsonl")
    ap.add_argument("--pred", default="rules", help="rules | llm | <file.jsonl>")
    ap.add_argument("--ref", default="reviewed", help="reviewed (mặc định) | llm (ước lượng tạm)")
    ap.add_argument("--errors", type=int, default=0)
    ap.add_argument("--gaz", default="auto", help="auto (PG nếu có) | items (dựng từ bộ vàng)")
    args = ap.parse_args()
    items = [json.loads(x) for x in Path(args.gold).read_text(encoding="utf-8").splitlines() if x.strip()]
    print(json.dumps(evaluate(items, predictions(items, args.pred, args.gaz), args.ref, args.errors),
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
