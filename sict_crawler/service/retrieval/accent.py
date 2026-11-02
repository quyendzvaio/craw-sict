"""Khôi phục dấu tiếng Việt cho câu hỏi gõ không dấu (thử nghiệm 2026-11-02).

Vì sao: mô hình nhúng (jina-clip-v2) gần như không hiểu tiếng Việt không dấu. Cùng câu golden_v1 bỏ dấu, recall@5
1,0 → 0,881, MRR 0,928 → 0,775; "co van hoc tap cua truong" không tìm thấy trang, "Cố vấn học tập…" ra hạng 2.
BM25 đã bỏ dấu cả hai phía nhưng âm tiết bỏ dấu quá chung (co, de, tai) nên không bù được.

Cách làm: mô hình bigram âm tiết đếm từ chính kho chữ của site (không LLM, không mô hình ngoài), giải Viterbi trên
các âm tiết đã bỏ dấu, chọn trong ≤ 6 dạng có dấu hay gặp nhất của mỗi âm tiết. Số, ký hiệu, chữ viết tắt không có
dạng có dấu thì giữ nguyên. Một ít cụm từ để hỏi (bao nhiêu, mấy, ở đâu…) được cộng thêm vì kho chữ là văn bản
tường thuật, ít câu hỏi. Đo trên 198 câu có dấu của golden_v1/v2 + list_v2 (bỏ dấu rồi khôi phục): đúng 95,6 % âm
tiết; sai chủ yếu ở từ để hỏi, ít ảnh hưởng tìm kiếm.

Dữ liệu: `accent_vi.json.gz` cạnh file này, dựng bằng `python -m sict_crawler.service.retrieval.accent build` từ
collection chữ đang phục vụ. retriever chỉ dùng câu khôi phục như MỘT truy vấn phụ (D27: câu gốc luôn được tìm).
"""
from __future__ import annotations

import gzip
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from sict_crawler.core.text import fold

MODEL_PATH = Path(__file__).with_name("accent_vi.json.gz")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_SPLIT = re.compile(r"[\n|.;:!?()\[\]]")
MAX_CANDIDATES = 6
MIN_BIGRAM = 2            # bigram gặp 1 lần bỏ khỏi file (nhiễu, nặng file)
MAX_UNIGRAMS = 60000
# cụm hỏi thường gặp: kho chữ là văn bản tường thuật nên "mấy", "bao nhiêu"… hiếm, dễ thành "máy", "báo nhiều"
QUESTION_PHRASES = ["bao nhiêu", "là bao nhiêu", "mấy", "số mấy", "tầng mấy", "học mấy năm", "là gì", "ở đâu",
                    "khi nào", "như thế nào", "những gì", "những ai", "gồm những", "có những", "nào", "là ai",
                    "tải về", "tải", "xin", "cho em hỏi", "cho mình hỏi", "ạ"]
PHRASE_WEIGHT = 200


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(unicodedata.normalize("NFC", text))]


def has_diacritics(text: str) -> bool:
    t = unicodedata.normalize("NFC", text).lower()
    return fold(t) != t


def build(texts) -> dict:
    """Đếm unigram / bigram âm tiết (chữ thường, NFC) trong các đoạn văn; bigram không vượt qua dấu câu."""
    uni: Counter = Counter()
    bi: Counter = Counter()
    for t in texts:
        for part in _SPLIT.split(t or ""):
            w = words(part)
            uni.update(w)
            bi.update(zip(w, w[1:]))
    return {"uni": dict(uni.most_common(MAX_UNIGRAMS)),
            "bi": {f"{a} {b}": c for (a, b), c in bi.items() if c >= MIN_BIGRAM and a in uni and b in uni}}


class Restorer:
    def __init__(self, model: dict):
        self.uni: Counter = Counter(model["uni"])
        self.bi: Counter = Counter({tuple(k.split(" ", 1)): v for k, v in model["bi"].items()})
        for ph in QUESTION_PHRASES:
            w = words(ph)
            for x in w:
                self.uni[x] += PHRASE_WEIGHT
            for a, b in zip(w, w[1:]):
                self.bi[(a, b)] += PHRASE_WEIGHT
        self.total = sum(self.uni.values())
        cand: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for w, c in self.uni.items():
            cand[fold(w)].append((w, c))
        self.cand = {k: [w for w, _ in sorted(v, key=lambda x: -x[1])[:MAX_CANDIDATES]] for k, v in cand.items()}

    def _lp_uni(self, w: str) -> float:
        return math.log((self.uni[w] + 0.1) / (self.total + 0.1 * len(self.uni)))

    def _lp_bi(self, a: str, b: str) -> float:
        # Witten-Bell đơn giản: bigram hiếm → lùi về tần suất của b
        return math.log((self.bi[(a, b)] + 50 * (self.uni[b] + 1) / self.total) / (self.uni[a] + 50))

    def restore_words(self, ws: list[str]) -> list[str]:
        if not ws:
            return []
        lat = [self.cand.get(w, [w]) for w in ws]
        best = {c: (self._lp_uni(c), [c]) for c in lat[0]}
        for col in lat[1:]:
            best = {c: max(((s + self._lp_bi(p, c), path + [c]) for p, (s, path) in best.items()),
                           key=lambda x: x[0]) for c in col}
        return max(best.values(), key=lambda x: x[0])[1]

    def restore(self, text: str) -> str:
        """Khôi phục dấu các âm tiết, giữ nguyên số/ký hiệu/khoảng trắng; chữ hoa đầu âm tiết được giữ."""
        text = unicodedata.normalize("NFC", text)
        spans = [(m.start(), m.end()) for m in _WORD.finditer(text)]
        fixed = self.restore_words([text[a:b].lower() for a, b in spans])
        out, last = [], 0
        for (a, b), w in zip(spans, fixed):
            orig = text[a:b]
            if orig.isupper() and len(orig) > 1:
                w = orig                       # CNTT, HIT, SICT: viết tắt giữ nguyên
            elif orig[:1].isupper():
                w = w[:1].upper() + w[1:]
            out += [text[last:a], w]
            last = b
        return "".join(out + [text[last:]])


@lru_cache(maxsize=1)
def _restorer() -> Restorer | None:
    if not MODEL_PATH.exists():
        return None
    with gzip.open(MODEL_PATH, "rt", encoding="utf-8") as f:
        return Restorer(json.load(f))


def maybe_restore(question: str) -> str | None:
    """Câu không có dấu nào và có ≥ 2 âm tiết tiếng Việt khôi phục được → câu có dấu; còn lại None."""
    r = _restorer()
    if r is None or has_diacritics(question):
        return None
    ws = words(question)
    if sum(1 for w in ws if any(has_diacritics(c) for c in r.cand.get(w, []))) < 2:
        return None
    out = r.restore(question)
    return out if out != question else None


def _build_cli(out: Path) -> None:
    from sict_crawler.core.config import get_settings
    from sict_crawler.repository import vector_store
    texts: list[str] = []
    client = vector_store.get_client()
    for name in [get_settings().qdrant.collection_text]:   # không lấy caption OCR (nhiễu ký tự)
        off = None
        while True:
            pts, off = client.scroll(name, limit=2000, offset=off, with_payload=["content"], with_vectors=False)
            texts += [p.payload.get("content") or "" for p in pts]
            if off is None:
                break
    model = build(texts)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False)
    print(f"{len(texts)} đoạn, {len(model['uni'])} âm tiết, {len(model['bi'])} bigram → {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Dựng mô hình khôi phục dấu từ collection chữ đang phục vụ")
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--out", type=Path, default=MODEL_PATH)
    a = ap.parse_args()
    _build_cli(a.out)
