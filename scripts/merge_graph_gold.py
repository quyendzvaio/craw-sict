"""Gộp nhãn nháp độc lập (draft.llm) vào bộ vàng graph — giai đoạn B, D20.

Đầu vào: các file JSONL {"url", "entities", "media"} (VD gán nhãn mù bằng LLM, không xem draft.rules).
Mỗi dòng được kiểm tra JSON và gắn vào `draft.llm` của tài liệu cùng url; ảnh chỉ giữ thực thể có tên
trong alt riêng / đoạn sát ảnh / OCR (alt trùng tiêu đề không tính — eval/GRAPH_ANNOTATION.md); thực
thể có câu trích không nguyên văn vẫn giữ (chỉ là gợi ý) nhưng đánh dấu `evidence_ok: false`.
KHÔNG đụng `entities` / `media` / `status` (phần của người duyệt).

Usage: python scripts/merge_graph_gold.py --gold eval/graph_gold_v1.jsonl out0.jsonl out1.jsonl ...
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sict_crawler.service.graph import graph as G  # noqa: E402


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="eval/graph_gold_v1.jsonl")
    ap.add_argument("--source", default="llm", help="tên nhánh trong draft (mặc định llm)")
    ap.add_argument("inputs", nargs="+")
    args = ap.parse_args()
    gold_p = Path(args.gold)
    items = [json.loads(x) for x in gold_p.read_text(encoding="utf-8").splitlines() if x.strip()]
    by_url = {it["url"]: it for it in items}
    stats = {"lines": 0, "bad_json": 0, "unknown_url": 0, "entities": 0, "evidence_not_verbatim": 0,
             "media_pairs": 0, "media_pairs_dropped": 0}
    for path in args.inputs:
        for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            stats["lines"] += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                stats["bad_json"] += 1
                print(f"{path}:{n}: JSON lỗi: {e}")
                continue
            it = by_url.get(row.get("url"))
            if it is None:
                stats["unknown_url"] += 1
                continue
            src = _ws("\n".join([it["title"], it["body"], *(f"{i.get('alt', '')} {i.get('context', '')} "
                                                           f"{i.get('ocr', '')}" for i in it["images"])]))
            ents = []
            for e in row.get("entities") or []:
                ok = bool(e.get("evidence")) and _ws(e["evidence"]) in src
                stats["entities"] += 1
                stats["evidence_not_verbatim"] += not ok
                ents.append({**e, "evidence_ok": ok})
            imgs = {i["sha256"]: i for i in it["images"]}
            media = []
            for m in row.get("media") or []:
                im = imgs.get(m.get("sha256"))
                if im is None:
                    continue
                alt = "" if G.fold(im.get("alt", "")) == G.fold(it["title"]) else im.get("alt", "")
                own = G.fold(" ".join([alt, im.get("context", ""), im.get("ocr", "")]))
                keep = [x for x in m.get("entities") or [] if G.fold(x) in own]
                stats["media_pairs"] += len(keep)
                stats["media_pairs_dropped"] += len(m.get("entities") or []) - len(keep)
                if keep:
                    media.append({"sha256": m["sha256"], "entities": keep})
            it.setdefault("draft", {})[args.source] = {"entities": ents, "media": media}
    gold_p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
