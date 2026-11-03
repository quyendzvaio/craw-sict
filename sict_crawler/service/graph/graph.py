"""Graph khái niệm D33 (2026-11-03): danh bạ + khớp đoạn chữ / câu hỏi. Không luật theo loại, không LLM.

Thực thể = khái niệm (kind "concept"): cụm danh từ ≥ 2 từ của chính corpus, chọn theo số bài (graph/concepts.py,
dựng bởi graphsync). Khoá = chữ đã bỏ dấu, bỏ năm và ký tự lạ (`_clean_key`). Payload Qdrant `entities`
("concept:<khoá>") trên từng đoạn chữ / caption → retriever lọc thẳng theo khái niệm nêu trong câu hỏi.
Graph luật cũ (người / sự kiện / CLB, D20) đã gỡ theo quyết định của chủ dự án ngày 2026-11-03.

Callers: graphsync (dựng), api.routes (nạp danh bạ từ PG), retriever (match_question, ekeys).
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from sict_crawler.core.text import fold

KIND = "concept"

_YEARISH = re.compile(r"\b(?:19|20)\d{2}\b|\blan thu \d+\b|\bnam hoc\b|\bkhoa \d+\b")
_WORD = re.compile(r"[^\W_]+")


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def _clean_key(s: str) -> str:
    """Khoá chuẩn: bỏ dấu, bỏ năm / "lần thứ N" / "năm học" / "khóa N", chỉ giữ a-z0-9."""
    k = _YEARISH.sub(" ", fold(s))
    k = re.sub(r"[^a-z0-9 ]+", " ", k)
    return re.sub(r"\s+", " ", k).strip()


@dataclass
class Entity:
    kind: str
    key: str
    name: str
    aliases: list[str] = field(default_factory=list)
    doc_count: int = 0
    id: int | None = None
    attrs: dict = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.kind, self.key))


class Gazetteer:
    """Khớp danh bạ trên 1 đoạn chữ / câu hỏi: n-gram token đã bỏ dấu, tại mỗi vị trí lấy cụm DÀI NHẤT,
    các vị trí sau vẫn xét (cho phép chồng lấn: "thị giác máy tính" và "máy tính")."""

    def __init__(self, entities: list[Entity]):
        self.entities = {(e.kind, e.key): e for e in entities}
        index: dict[tuple[str, ...], Entity] = {}
        for e in entities:
            toks = tuple(e.key.split())
            if len(toks) >= 2 or len(e.key) >= 5:
                index.setdefault(toks, e)
            for a in e.aliases:
                if len(a) >= 3 and len(a.split()) >= 2:
                    index.setdefault(tuple(a.split()), e)
        self.index = index
        by: dict[str, set] = defaultdict(set)
        for toks in index:
            by[toks[0]].add(len(toks))
        self._by_first = {k: sorted(v, reverse=True) for k, v in by.items()}

    def __len__(self) -> int:
        return len(self.entities)

    def match(self, text: str) -> list[Entity]:
        ftoks = [fold(w.lower()) for w in _WORD.findall(_nfc(text))]
        out: list[Entity] = []
        for i, t in enumerate(ftoks):
            for n in self._by_first.get(t, ()):
                if i + n > len(ftoks):
                    continue
                e = self.index.get(tuple(ftoks[i:i + n]))
                if e is None:
                    continue
                if e not in out:
                    out.append(e)
                break   # cụm dài nhất tại vị trí này
        return out


def ekey(e: Entity) -> str:
    """Khoá payload Qdrant: "<kind>:<key>"."""
    return f"{e.kind}:{e.key}"


def ekeys(es) -> list[str]:
    return list(dict.fromkeys(ekey(e) for e in es))


_current: Gazetteer | None = None


def current() -> Gazetteer | None:
    """Danh bạ API đang dùng (nạp từ Postgres lúc khởi động + làm mới định kỳ). None → tắt graph."""
    return _current


def set_current(g: Gazetteer | None) -> None:
    global _current
    _current = g


def match_question(*texts: str) -> list[Entity]:
    g = _current
    if g is None:
        return []
    out: list[Entity] = []
    for t in texts:
        for e in g.match(t or ""):
            if e not in out:
                out.append(e)
    return out
