"""Graph khái niệm (kiểu LazyGraphRAG, 2026-11-03): danh bạ = cụm danh từ của chính corpus, không luật
theo loại, không LLM.

Vì sao: graph luật (person/event/org) phải viết luật cho từng dạng tên, nên luôn có trường hợp nằm ngoài
dự tính. Ở đây mọi cụm danh từ ≥ 2 từ do underthesea tách (CRF có sẵn trong gói, chạy CPU, không tải model)
đều là ứng viên. Bộ lọc tổng quát duy nhất là số bài chứa cụm (document frequency):
  - < 2 bài: cụm riêng 1 bài không nối được bài nào khác;
  - > concept_df_max_ratio × số bài: cụm quá phổ biến ("sinh viên", "nhà trường") không phân biệt được bài.
Thêm một lớp lọc ngữ pháp: cụm toàn hư từ / từ hỏi ("bao nhiêu", "như thế nào") không làm khái niệm
(is_function_phrase, 2026-11-04).
Liên kết: khớp danh bạ (graph.Gazetteer.match, cụm dài nhất tại mỗi vị trí) trên MỌI đoạn (chữ, bảng,
caption = tiêu đề + caption) → payload `entities` từng đoạn = "concept:<khoá>". Truy xuất dùng lại nguyên
nhánh lọc graph sẵn có (retriever._graph_filter). Không có cạnh thực thể ↔ thực thể (không dòng [graph]).

Đo 2026-11-03 trên index v3, so với graph luật trên cùng trạng thái index: golden_v1 MRR 0,933 → 0,954,
golden_v2 MRR 0,690 → 0,701 (url_hit 70/80 cả hai), list_v2 0,871 → 0,916; mất 1 câu từ chối đúng (g080). Dựng toàn bộ: ~10 phút / 1 lõi, < 1 GB RAM; kết quả
tách cụm cache theo từng đoạn trong bảng chunk_phrases nên lần dựng sau chỉ tách đoạn mới/đổi.

underthesea chỉ cần khi dựng (worker, group `graph`); API chỉ khớp danh bạ đã lưu trong PG.
"""
from __future__ import annotations

import hashlib
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from sict_crawler.service.graph import graph as G

KIND = G.KIND
SENT_MAX_CHARS = 1500      # câu dài hơn (bảng dán liền, danh sách không dấu chấm) bị cắt trước khi tách
# Phiên bản luật chọn khái niệm: tăng khi đổi luật để incremental dựng lại graph dù corpus không đổi
# (nằm trong chữ ký corpus, pipeline.incremental.corpus_signature). 2: bỏ cụm toàn hư từ (2026-11-04).
RULES_VERSION = 2

# Hư từ / từ hỏi / đại từ tiếng Việt CÓ DẤU (lớp từ đóng; bản có dấu của core.sparse.QUESTION_STOP và vài từ còn
# thiếu). underthesea hay gắn nhầm các cụm toàn hư từ là cụm danh từ ("bao nhiêu", "như thế nào", "những ai",
# "có thể"); trong câu hỏi chúng xuất hiện khắp nơi, nên danh sách lọc AND của graph co lại còn vài đoạn ngẫu
# nhiên có cụm đó và đẩy chúng lên đầu (golden_v2 g018, 2026-11-04). Phải xét trên chữ CÓ DẤU: bỏ dấu thì
# "bảo vệ", "cơ thể", "đề thi", "báo chí" cũng chỉ còn hư từ.
FUNCTION_WORDS = frozenset(unicodedata.normalize("NFC", """
a à ai bao bị biết các cách cái cho chứ có của đã đang để đến đều đó được gì gồm hay hoặc hỏi khi không là làm
lúc mà mình một muốn nào này nếu nhé nhiều như những nơi ở ơi ra rồi sao sẽ tại thế thì thôi tôi trong từ và về vì
với xin xem em anh chị bạn hiện ấy kia nhiêu thể vậy đâu bấy giờ chỉ cũng rất đây
""").split())


def is_function_phrase(phrase: str) -> bool:
    """Cụm chỉ gồm hư từ / từ hỏi (xét chữ có dấu, không phân biệt hoa thường) → không làm khái niệm."""
    words = unicodedata.normalize("NFC", phrase).lower().split()
    return bool(words) and all(w in FUNCTION_WORDS for w in words)


@dataclass
class ChunkText:
    chunk_id: str            # payload chunk_id hoặc id point (khớp với sync_payloads)
    document_id: int
    kind: str                # text | table | media_caption
    text: str                # caption: "tiêu đề. caption"

    @property
    def phrase_source(self) -> bool:
        """Chỉ đoạn chữ thường được tách cụm danh từ (bảng / OCR nhiễu, tách sai nhiều); mọi đoạn đều được
        khớp danh bạ."""
        return self.kind == "text"


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def noun_phrases(text: str) -> list[str]:
    """Cụm danh từ (B-NP/I-NP) của underthesea.chunk, theo từng câu."""
    from underthesea import chunk, sent_tokenize   # chỉ worker có (group `graph`)
    out: list[str] = []
    for s in sent_tokenize(text):
        try:
            toks = chunk(s[:SENT_MAX_CHARS])
        except Exception:
            continue
        cur: list[str] = []
        for w, _pos, tag in toks:
            if tag == "B-NP" or (tag == "I-NP" and not cur):
                if cur:
                    out.append(" ".join(cur))
                cur = [w]
            elif tag == "I-NP":
                cur.append(w)
            elif cur:
                out.append(" ".join(cur))
                cur = []
        if cur:
            out.append(" ".join(cur))
    return out


def concept_key(phrase: str) -> str | None:
    """Khoá chuẩn (bỏ dấu, bỏ năm, bỏ ký tự lạ) — None khi không đủ làm khái niệm (1 từ, toàn số)."""
    k = G._clean_key(phrase)
    if len(k.split()) < 2 or k.replace(" ", "").isdigit() or len(k) > 300:   # 300 = cột entities.key
        return None
    return k


def select_concepts(phrases: list[tuple[int, list[str]]], n_docs: int, df_max_ratio: float) -> list[G.Entity]:
    """phrases: [(document_id, cụm của 1 đoạn)] theo thứ tự ổn định → thực thể kind=concept có
    2 ≤ số bài ≤ df_max_ratio × n_docs, trừ khoá mà cách đọc hư từ chiếm nhiều bài hơn cách đọc khái niệm.
    Tên hiển thị = cách viết khái niệm gặp đầu tiên."""
    df_max = max(2, int(df_max_ratio * n_docs))
    docs: dict[str, set[int]] = defaultdict(set)
    fdocs: dict[str, set[int]] = defaultdict(set)    # bài có cách viết toàn hư từ của khoá
    cdocs: dict[str, set[int]] = defaultdict(set)    # bài có cách viết là khái niệm thật
    name: dict[str, str] = {}
    for doc, nps in phrases:
        for p in nps:
            k = concept_key(p)
            if k is None:
                continue
            docs[k].add(doc)
            if is_function_phrase(p):
                fdocs[k].add(doc)
            else:
                cdocs[k].add(doc)
                name.setdefault(k, p)
    # Quyết định theo KHOÁ (bỏ dấu), vì câu hỏi được khớp trên chữ bỏ dấu: "hien tai" gặp 112 bài "hiện tại" và
    # 2 bài "HIỀN TÀI" → bỏ cả khoá, kẻo câu "hiện tại …" vẫn khớp; "co the" có cả "có thể" lẫn "cơ thể" ngang nhau → giữ.
    return [G.Entity(kind=KIND, key=k, name=name[k], doc_count=len(d))
            for k, d in docs.items()
            if 2 <= len(d) <= df_max and len(fdocs[k]) <= len(cdocs[k])]


def link_rows(gaz: G.Gazetteer, chunks: list[ChunkText], eids: dict[tuple[str, str], int]) -> list[dict]:
    """Cạnh `mentions` (đoạn ↔ khái niệm) theo danh bạ; khớp như văn bản bài viết (query=False)."""
    rows = []
    for c in chunks:
        for e in gaz.match(c.text):
            i = eids.get((e.kind, e.key))
            if i is None:
                continue
            rows.append({"entity_id": i, "document_id": c.document_id, "media_id": None, "video_id": None,
                         "chunk_id": c.chunk_id[:36], "rel": "mentions", "weight": 1.0, "source": c.kind[:16],
                         "roles": [], "evidence": ""})
    return rows
