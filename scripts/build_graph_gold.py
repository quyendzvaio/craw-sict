"""Xuất mẫu phân tầng tài liệu → bộ vàng graph (giai đoạn B, D20) để gán nhãn tay.

Mỗi dòng tự chứa văn bản cần đọc (tiêu đề, thân bài cắt ≤ MAX_BODY ký tự, ảnh: alt + đoạn sát ảnh
+ OCR) để chấm điểm offline / trong CI mà không cần PG. Nhãn nháp:
  draft.rules  thực thể luật hiện tại tìm ra trên CHÍNH văn bản đã lưu (danh bạ production từ PG)
  draft.llm    (tuỳ chọn) gán nhãn độc lập, không xem draft.rules — scripts/merge_graph_gold.py
`entities` / `media` để trống: người duyệt điền theo eval/GRAPH_ANNOTATION.md rồi đặt status=reviewed.

Usage: python scripts/build_graph_gold.py --out eval/graph_gold_v1.jsonl [--n 150] [--seed 7]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from sict_crawler import db, graphsync  # noqa: E402
from sict_crawler import graph as G  # noqa: E402
from sict_crawler.models import Document  # noqa: E402

MAX_BODY = 6000
# số bài theo nhóm mục (tổng ~150); nhóm "khac" lấy phần còn lại
QUOTA = {"tin-tuc": 45, "thong-bao": 20, "guong-sang": 15, "can-bo-giang-vien": 10, "tuyen-dung": 12,
         "ke-hoach": 8, "cau-lac-bo": 5, "thu-vien-anh": 4, "sinh-vien-nckh": 4, "tin-khcn": 4,
         "attachment": 10, "khac": 13}


def strip_header(text: str) -> str:
    """Bỏ header ngữ cảnh "[tiêu đề | mục | ngày]" index spec v2 thêm vào đầu chunk."""
    return text.split("] ", 1)[1] if text.startswith("[") and "] " in text[:400] else text


def predict_rules(item: dict, gaz: G.Gazetteer) -> dict:
    """Thực thể + ảnh↔thực thể theo luật hiện tại trên văn bản của item (dùng chung với eval_graph)."""
    doc = {"title": item["title"], "section": item["section"], "doc_type": item["doc_type"],
           "url": item["url"]}
    media = [G.MediaRef(im["sha256"], "image", im.get("alt", ""), im.get("context", ""), im.get("ocr", ""))
             for im in item.get("images", [])]
    g = G.link_document(doc, [("body", item["body"])], media, gaz)
    ents = [{"type": e.kind, "name": e.name, "in_title": e in g.about} for e in [*g.about, *g.mentions]]
    med = [{"sha256": k, "entities": [e.name for e, w, _ in v if w >= G.STRONG_MIN]}
           for k, v in g.depicts.items()]
    return {"entities": ents, "media": [m for m in med if m["entities"]]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="eval/graph_gold_v1.jsonl")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--refresh-rules", action="store_true",
                    help="chỉ tính lại draft.rules trong file có sẵn (giữ draft.llm + phần người duyệt)")
    args = ap.parse_args()
    if args.refresh_rules:
        path = Path(args.out)
        items = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        async with db.get_session() as s:
            gaz = await graphsync.load_gazetteer(s)
        for it in items:
            it.setdefault("draft", {})["rules"] = predict_rules(it, gaz)
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items), encoding="utf-8")
        print(json.dumps({"refreshed": len(items)}))
        return
    async with db.get_session() as s:
        rows = (await s.execute(select(Document.id, Document.section, Document.doc_type).where(
            Document.hash_v2.is_not(None)).order_by(Document.id))).all()
        by: dict[str, list[int]] = defaultdict(list)
        for pk, sec, dt_ in rows:
            by["attachment" if dt_ == "attachment" else (sec if sec in QUOTA else "khac")].append(pk)
        rnd = random.Random(args.seed)
        scale = args.n / sum(QUOTA.values())
        picked = []
        for grp, q in QUOTA.items():
            pool = by.get(grp, [])
            picked += rnd.sample(pool, min(len(pool), max(1, round(q * scale))))
        ins = {di.pk: di for di in await graphsync.load_inputs(s, picked)}
        gaz = await graphsync.load_gazetteer(s)
    out = []
    for pk in picked:
        di = ins.get(pk)
        if di is None:
            continue
        body = "\n".join(strip_header(t) for _, t in di.chunks)[:MAX_BODY]
        item = {"doc_pk": pk, "url": di.doc["url"], "title": di.doc["title"],
                "section": di.doc["section"], "doc_type": di.doc["doc_type"], "body": body,
                "images": [{"sha256": m.key, "alt": m.alt, "context": m.context, "ocr": m.ocr[:300]}
                           for m in di.media if m.kind == "image"][:15],
                "status": "draft", "entities": [], "media": []}
        item["draft"] = {"rules": predict_rules(item, gaz)}
        out.append(item)
    Path(args.out).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out),
                              encoding="utf-8")
    print(json.dumps({"docs": len(out), "groups": {g: sum(1 for x in out if (
        "attachment" if x["doc_type"] == "attachment" else (x["section"] if x["section"] in QUOTA else "khac")) == g)
        for g in QUOTA}}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
