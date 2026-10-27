"""Vector thưa BM25 trên token đã bỏ dấu — nhánh keyword của hybrid search (D8, collection *_v2).

Cùng cách tách token với BM25 trong scripts/eval_retrieval.py (cấu hình đã đo trên bộ câu hỏi
vàng), nên hybrid trên Qdrant khớp hybrid lúc eval:
  - tài liệu: phần TF của BM25 (k1, b, avgdl) tính phía client lúc index;
  - câu hỏi: mỗi token 1.0; IDF do Qdrant tính (sparse vector `modifier=idf`), không phải lưu thống
    kê corpus và tự đúng khi incremental thêm/xoá point.
Thay `text_folded` + full-text index trong bản nháp D8: lọc full-text chỉ trả tập khớp theo thứ tự
scroll (không xếp hạng), còn sparse BM25 trả điểm xếp hạng được để đưa vào RRF.

Callers: indexer (point), vector_store.search_sparse (query); question_terms: agent.evidence, filters.infer.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from sict_crawler.core.text import fold

K1 = 1.2
B = 0.75
# độ dài TB (token) của chunk + caption trên artifact 2026-10-12 (23.183 văn bản: 202,6). Hằng số
# để incremental tính TF giống bulk; lệch nhẹ khi corpus đổi không ảnh hưởng xếp hạng đáng kể.
AVGDL = 200.0
VECTOR_NAME = "bm25"

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(fold(text)) if len(t) >= 2]


# hư từ / từ hỏi (đã bỏ dấu) — không phải từ nội dung của câu hỏi (agent.evidence, filters.infer)
QUESTION_STOP = frozenset("""
a ai bao bi biet cac cach cai cho chu co cua da dang de den deu do duoc gi gom hay hoac hoi khi khong la lam luc
ma minh mot muon nao nay neu nhe nhieu nhu nhung noi o oi ra roi sao se tai the thi thoi toi trong tu va ve vi voi xin
xem em anh chi ban hien duoc nay ay kia
""".split())


def question_terms(question: str) -> list[str]:
    """Từ nội dung của câu hỏi (token BM25 bỏ dấu, bỏ hư từ), giữ thứ tự, không trùng."""
    return list(dict.fromkeys(t for t in tokens(question) if t not in QUESTION_STOP))


def token_index(tok: str) -> int:
    """Token → chỉ số cố định (31 bit, blake2b) — không cần lưu từ điển."""
    return int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big") & 0x7FFFFFFF


def _vector(weights: dict[int, float]) -> dict:
    idx = sorted(weights)
    return {"indices": idx, "values": [weights[i] for i in idx]}


def doc_vector(text: str) -> dict:
    """{indices, values} cho point. Rỗng khi không có token."""
    tf = Counter(tokens(text))
    n = sum(tf.values())
    norm = K1 * (1 - B + B * n / AVGDL)
    w: dict[int, float] = {}
    for t, f in tf.items():
        i = token_index(t)
        w[i] = w.get(i, 0.0) + f * (K1 + 1) / (f + norm)
    return _vector(w)


def query_vector(text: str) -> dict:
    return _vector({token_index(t): 1.0 for t in set(tokens(text))})
