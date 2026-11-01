"""Qdrant client: text + image collections, upsert, hybrid search.

Callers: main.py, api.py, indexer.py, loader.py, query_rewriter.py.
Schemas v2 (`*_v2`, indexer.py tạo payload; point id = uuid5 artifact):
  text  — vector dense E5 (không tên) + sparse `bm25` (sparse.py, IDF phía Qdrant).
          chunk {kind text|table, document_id, doc_key, chunk_id, idx, url, title, heading, content,
          section, section_label, doc_type, published_at, year, parent_url}
          caption {kind media_caption, media_kind image|video, media_key, video_key, document_id,
          url (ảnh/video), page_url, title, caption, section, doc_type, published_at, year, timestamp}
  ocr   — (v3, `qdrant.collection_ocr`) điểm caption ở trên tách khỏi text, cùng dense + sparse; rỗng = caption
          nằm chung collection text (v1/v2). Đọc/ghi qua ocr_collection() / text_collections() / collection_for().
  image — CLIP. {kind image|video_frame, media_key (sha256), s3_key, url, document_id [int,...],
          page_urls, title, caption, section [..], doc_type [..], year [..], published_at,
          timestamp, video_key} — 1 point / 1 file media, document_id là danh sách bài chứa nó.
Schema v1 (sict_text/sict_image, chờ xoá): document_id int, media_id → media_assets.
"""
from __future__ import annotations

import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PointStruct,
    Range,
    SearchParams,
    VectorParams,
)

from sict_crawler.core.config import get_settings

_client = None


# Lượt truy vấn độc lập chạy song song (2026-10-25: 1 câu hỏi = 9–20 lượt tuần tự, 0,1–3,7 s). Hai pool theo tầng:
# nhánh (dense / BM25 / graph / câu viết lại, `parallel`) → collection (text / ocr, `_per_collection`). Việc ở một tầng
# chỉ chờ việc của tầng dưới, nên pool có đầy cũng không kẹt (retriever có thêm tầng giai đoạn ở trên).
_LEG_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qdrant-leg")
_COL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qdrant-col")


def parallel(calls: list) -> list:
    """Chạy các hàm không đối số song song, trả kết quả đúng thứ tự; lỗi của một lượt nổi lên như khi gọi tuần tự."""
    if len(calls) <= 1:
        return [c() for c in calls]
    return [f.result() for f in [_LEG_POOL.submit(c) for c in calls]]


def _per_collection(fn, names: list[str]) -> list:
    return [fn(n) for n in names] if len(names) <= 1 else list(_COL_POOL.map(fn, names))


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        cfg = get_settings().qdrant
        # D12: API key gửi cả REST lẫn gRPC. Kết nối VM2 → VM1 đi trong mạng private của VPC (HTTP thường)
        # → bỏ cảnh báo "Api key is used with an insecure connection" mà client in mỗi lần khởi động.
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
        _client = QdrantClient(host=cfg.host, port=cfg.port, grpc_port=cfg.grpc_port,
                               prefer_grpc=cfg.prefer_grpc, timeout=cfg.timeout,
                               api_key=cfg.api_key or None, https=False)
    return _client


def _with_retry(fn, *, tries: int = 3, base_delay: float = 2.0):
    """Retry transient Qdrant failures (gRPC DEADLINE/UNAVAILABLE under load).

    Write/delete path only — reads stay single-shot để không làm chậm API.
    """
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — qdrant/grpc error types vary
            last = e
            if attempt < tries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last  # type: ignore[misc]


def ocr_collection() -> str:
    """Collection của điểm caption (tiêu đề + mục + OCR ảnh/video, phụ đề video)."""
    q = get_settings().qdrant
    return q.collection_ocr or q.collection_text


def text_collections() -> list[str]:
    """Các collection chữ (dense cùng không gian + sparse BM25): [text] hoặc [text, ocr] khi tách."""
    return list(dict.fromkeys((get_settings().qdrant.collection_text, ocr_collection())))


def collection_for(payload: dict) -> str:
    """Collection đích của 1 điểm chữ theo payload: caption → ocr, chunk → text."""
    return ocr_collection() if payload.get("kind") == "media_caption" else get_settings().qdrant.collection_text


def ensure_collections():
    """Tạo collection còn thiếu theo schema v2: E5 dense + sparse BM25 (text), CLIP (image);
    scalar quantization int8 (bản int8 trong RAM, vector gốc on-disk để rescore). Collection đã
    có giữ nguyên (v1 không bị đụng)."""
    from qdrant_client.models import (
        Modifier,
        ScalarQuantization,
        ScalarQuantizationConfig,
        ScalarType,
        SparseVectorParams,
    )

    from sict_crawler.core import sparse
    cfg = get_settings().qdrant
    client = get_client()
    quant = ScalarQuantization(scalar=ScalarQuantizationConfig(
        type=ScalarType.INT8, quantile=0.99, always_ram=True))
    for name, dim, with_sparse in [*((c, cfg.text_dim, True) for c in text_collections()),
                                   (cfg.collection_image, cfg.image_dim, False)]:
        if not client.collection_exists(name):
            client.create_collection(
                name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE, on_disk=True),
                sparse_vectors_config=({sparse.VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF)}
                                       if with_sparse else None),
                quantization_config=quant)
            print(f"[qdrant] tạo collection {name} (dim={dim}, sparse={with_sparse})")
    _caps.clear()
    ensure_payload_indexes()


# document_id: delete/filter theo doc. section/doc_type/year: metadata filtering (filters.py).
# kind: tách chunk / caption. media_key, video_key: gắn/gỡ bài khỏi point media (indexer).
# entities / entities_strong: lọc theo thực thể graph ("person:<key>", "event:<key>", "org:<key>").
_PAYLOAD_INDEXES = (("document_id", "integer"), ("section", "keyword"),
                    ("doc_type", "keyword"), ("year", "integer"), ("kind", "keyword"),
                    ("media_key", "keyword"), ("video_key", "keyword"),
                    ("entities", "keyword"), ("entities_strong", "keyword"),  # graph D20
                    ("page", "integer"))  # D24: lọc theo trang PDF/PPTX


def ensure_payload_indexes():
    """Payload index cho mọi collection (tránh full-scan 20k-75k points). Idempotent."""
    cfg = get_settings().qdrant
    client = get_client()
    for name in (*text_collections(), cfg.collection_image):
        try:
            schema = client.get_collection(name).payload_schema or {}
        except Exception:
            continue  # collection vừa tạo ở ensure_collections, bỏ qua
        for fld, kind in _PAYLOAD_INDEXES:
            if fld not in schema:
                client.create_payload_index(name, field_name=fld, field_schema=kind)
                print(f"[qdrant] payload index {fld} → {name}")


_caps: dict[str, bool] = {}


def has_sparse(collection: str | None = None) -> bool:
    """Collection có sparse `bm25` (schema v2)? Cache theo tên; lỗi kết nối → False (không cache)."""
    from sict_crawler.core import sparse
    name = collection or get_settings().qdrant.collection_text
    if name not in _caps:
        try:
            info = get_client().get_collection(name)
        except Exception:
            return False
        _caps[name] = sparse.VECTOR_NAME in (info.config.params.sparse_vectors or {})
    return _caps[name]


def require_v2() -> None:
    """Đường ghi v2 (indexer) chỉ ghi vào collection schema v2 — tránh trộn payload v2 vào
    collection v1 khi config/env còn trỏ tên cũ."""
    name = get_settings().qdrant.collection_text
    if not has_sparse(name):
        raise RuntimeError(
            f"collection {name!r} không phải schema v2 (thiếu sparse bm25). Trỏ "
            "QDRANT_COLLECTION_TEXT/IMAGE sang *_v2 và chạy `python -m sict_crawler.pipeline.loader` trước.")


def upsert_points(collection: str, points: list[PointStruct], batch: int = 256) -> None:
    client = get_client()
    for i in range(0, len(points), batch):
        part = points[i:i + batch]
        _with_retry(lambda part=part: client.upsert(collection, part, wait=True))


def text_point(pid: str, dense: list[float], text: str, payload: dict) -> PointStruct:
    """Point collection text v2: dense E5 + sparse BM25 của `text` (chuỗi đã embed)."""
    from qdrant_client.models import SparseVector

    from sict_crawler.core import sparse
    sv = sparse.doc_vector(text)
    vec: dict = {"": list(dense)}
    if sv["indices"]:
        vec[sparse.VECTOR_NAME] = SparseVector(**sv)
    return PointStruct(id=pid, vector=vec, payload=payload)


def _and(*filters):
    """Gộp nhiều qdrant Filter (must) — None bị bỏ qua."""
    from qdrant_client.models import Filter
    must, must_not = [], []
    for f in filters:
        if f is None:
            continue
        must += list(f.must or [])
        must_not += list(f.must_not or [])
    if not must and not must_not:
        return None
    return Filter(must=must or None, must_not=must_not or None)


def _pid() -> uuid.UUID:
    return uuid.uuid4()


def upsert_text_chunks(rows: list[dict]) -> list[str]:
    """rows: {vector, document_id, chunk_id, url, title, heading, content}. Returns point ids."""
    cfg = get_settings().qdrant
    client = get_client()
    points, ids = [], []
    for r in rows:
        pid = str(_pid())
        ids.append(pid)
        points.append(PointStruct(id=pid, vector=r["vector"], payload={
            k: r[k] for k in ("document_id", "chunk_id", "url", "title", "heading", "content")
        }))
    _with_retry(lambda: client.upsert(cfg.collection_text, points))
    return ids


def upsert_images(rows: list[dict]) -> list[str]:
    """rows: {vector, media_id, document_id, url, kind, s3_key[, caption][, timestamp]}.
    Returns point ids."""
    cfg = get_settings().qdrant
    client = get_client()
    points, ids = [], []
    for r in rows:
        pid = str(_pid())
        ids.append(pid)
        points.append(PointStruct(id=pid, vector=r["vector"], payload=r))
    _with_retry(lambda: client.upsert(cfg.collection_image, points))
    return ids


def upsert_media_captions(rows: list[dict]) -> list[str]:
    """rows: {vector, media_id, document_id, url, kind='media_caption', media_kind,
    title, caption, timestamp}. E5 vectors → collection caption (ocr_collection) so text queries hit media."""
    client = get_client()
    points, ids = [], []
    for r in rows:
        pid = str(_pid())
        ids.append(pid)
        points.append(PointStruct(id=pid, vector=r["vector"], payload=r))
    _with_retry(lambda: client.upsert(ocr_collection(), points))
    return ids


def set_image_captions(pairs: list[tuple[str, str]]) -> None:
    """Gán caption vào payload sict_image đã có (theo point id, không embed lại).
    Dùng sau annotate (PG đã có caption nhưng point upsert trước đó chưa có)."""
    cfg = get_settings().qdrant
    client = get_client()
    for pid, cap in pairs:
        if not pid or not cap:
            continue
        _with_retry(lambda pid=pid, cap=cap: client.set_payload(
            cfg.collection_image, {"caption": cap}, [pid]))


def delete_by_document(document_id: int) -> None:
    """Gỡ 1 tài liệu: xoá point chữ (chunk + caption) của nó; point media (v2: document_id là
    danh sách bài chứa) chỉ gỡ id bài, xoá point khi không còn bài nào dùng (AC-B5)."""
    client = get_client()
    sel = FilterSelector(filter=Filter(must=[
        FieldCondition(key="document_id", match=MatchValue(value=document_id))]))
    for name in text_collections():
        _with_retry(lambda name=name: client.delete(name, points_selector=sel))
    detach_document(document_id)


def _doc_ids(v) -> list[int]:
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def detach_document(document_id: int, point_ids: list[str] | None = None) -> int:
    """Gỡ document_id khỏi point media; point không còn bài → xoá. Trả số point bị xoá."""
    cfg = get_settings().qdrant
    client = get_client()
    flt = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))])
    if point_ids:
        pts = client.retrieve(cfg.collection_image, point_ids, with_payload=["document_id"])
    else:
        pts, offset = [], None
        while True:
            page, offset = client.scroll(cfg.collection_image, scroll_filter=flt, limit=256,
                                         offset=offset, with_payload=["document_id"])
            pts += page
            if offset is None:
                break
    gone = []
    for p in pts:
        rest = [d for d in _doc_ids(p.payload.get("document_id")) if d != document_id]
        if rest:
            _with_retry(lambda p=p, rest=rest: client.set_payload(
                cfg.collection_image, {"document_id": rest}, [p.id]))
        else:
            gone.append(p.id)
    if gone:
        _with_retry(lambda: client.delete(cfg.collection_image, points_selector=gone))
    return len(gone)


def attach_document(point_ids: list[str], document_id: int, page_url: str) -> list[str]:
    """Thêm bài vào point media đã có (ảnh/frame dùng chung nhiều bài). Trả các id tồn tại."""
    if not point_ids:
        return []
    cfg = get_settings().qdrant
    client = get_client()
    found = []
    for p in client.retrieve(cfg.collection_image, point_ids,
                             with_payload=["document_id", "page_urls"]):
        ids = _doc_ids(p.payload.get("document_id"))
        urls = list(p.payload.get("page_urls") or [])
        if document_id not in ids:
            ids.append(document_id)
        if page_url not in urls:
            urls.append(page_url)
        _with_retry(lambda p=p, ids=ids, urls=urls: client.set_payload(
            cfg.collection_image, {"document_id": ids, "page_urls": urls}, [p.id]))
        found.append(str(p.id))
    return found  # id dạng str (uuid) để so với media.point_id


def delete_text_chunks(document_id: int) -> None:
    """Chỉ text chunks của một doc (points sict_text không có key 'kind' phân biệt
    chunk thường vs media_caption → must_not 'media_caption' giữ caption)."""
    cfg = get_settings().qdrant
    client = get_client()
    sel = FilterSelector(filter=Filter(
        must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))],
        must_not=[FieldCondition(key="kind", match=MatchValue(value="media_caption"))]))
    _with_retry(lambda: client.delete(cfg.collection_text, points_selector=sel))


def search_text(query_vector: list[float], limit: int = 8,
                filter_document_id: int | None = None, query_filter=None,
                collection: str | None = None) -> list[dict]:
    """query_filter: qdrant Filter từ filters.to_qdrant (lọc cứng metadata) hoặc None.
    collection None = mọi collection chữ (text_collections): mỗi collection tối đa `limit`, gộp theo cosine
    (cùng model, cùng không gian → so được) — caption không chen mất chỗ của chunk."""
    client = get_client()
    doc_flt = None
    if filter_document_id is not None:
        doc_flt = Filter(must=[FieldCondition(key="document_id",
                                              match=MatchValue(value=filter_document_id))])
    def one(name: str) -> list[dict]:
        res = client.query_points(name, query=query_vector, limit=limit,
                                  search_params=SearchParams(exact=False),
                                  query_filter=_and(doc_flt, query_filter))
        return [{"score": p.score, "_pid": str(p.id), "_col": name, **p.payload} for p in res.points]
    hits = [h for part in _per_collection(one, [collection] if collection else text_collections()) for h in part]
    return sorted(hits, key=lambda h: -h["score"]) if not collection else hits


def search_images(query_vector: list[float], limit: int = 5, query_filter=None) -> list[dict]:
    cfg = get_settings().qdrant
    client = get_client()
    res = client.query_points(cfg.collection_image, query=query_vector, limit=limit,
                              query_filter=query_filter)
    return [{"score": p.score, **p.payload} for p in res.points]


def scroll_text(query_filter, limit: int = 200, collection: str | None = None) -> list[dict]:
    """Điểm collection chữ theo filter payload (không vector, không xếp hạng) — VD mọi chunk của 1 bài
    (`document_id` có payload index) hay caption của 1 ảnh trong 1 bài. collection None = mọi collection chữ."""
    out: list[dict] = []
    for name in ([collection] if collection else text_collections()):
        points, _ = get_client().scroll(name, scroll_filter=query_filter, limit=limit, with_payload=True)
        out += [{"score": 0.0, **p.payload} for p in points]
    return out


def latest_text(sections: list[str], limit: int = 10, since_year: int | None = None,
                max_points: int = 2000) -> list[dict]:
    """Bài mới nhất của các mục (`published_at` giảm dần), mỗi bài 1 hit = đoạn đầu (`idx` nhỏ nhất); bỏ file đính kèm
    (là con của bài). Payload không có index ngày → lọc mục + năm (có index) rồi sắp phía client: mục "thong-bao" từ
    năm trước tới nay ~350 điểm, 0,02 s. Dùng cho câu hỏi chỉ gồm "mục + mới nhất" (filters.QueryFilters.latest)."""
    must = [FieldCondition(key="section", match=MatchAny(any=sections))]
    if since_year:
        must.append(FieldCondition(key="year", range=Range(gte=since_year)))
    flt = Filter(must=must, must_not=[FieldCondition(key="doc_type", match=MatchValue(value="attachment")),
                                      FieldCondition(key="kind", match=MatchValue(value="media_caption"))])
    first: dict = {}
    for name in text_collections():
        offset, seen = None, 0
        while seen < max_points:
            points, offset = get_client().scroll(name, scroll_filter=flt, limit=256, offset=offset, with_payload=True)
            seen += len(points)
            for p in points:
                pl = p.payload
                d, idx = pl.get("document_id"), pl.get("idx") or 0
                if pl.get("published_at") and (d not in first or idx < (first[d].get("idx") or 0)):
                    first[d] = {"score": 0.0, "_pid": str(p.id), "_col": name, **pl}
            if offset is None:
                break
    return sorted(first.values(), key=lambda h: h["published_at"], reverse=True)[:limit]


def scroll_text_by_content(term: str, limit: int = 8, query_filter=None) -> list[dict]:
    """Keyword leg for hybrid search: payload MatchText on content + caption.
    No vector query — order is scroll order; caller fuses by rank (RRF)."""
    from qdrant_client.models import MatchText
    cfg = get_settings().qdrant
    client = get_client()
    hits: list[dict] = []
    for key in ("content", "caption"):
        flt = _and(Filter(must=[FieldCondition(key=key, match=MatchText(text=term))]),
                   query_filter)
        points, _ = client.scroll(cfg.collection_text, scroll_filter=flt,
                                  limit=limit, with_payload=True)
        for p in points:
            hits.append({"score": 0.0, **p.payload})
    return hits


def _interleave(lists: list[list[dict]]) -> list[dict]:
    """Gộp danh sách đã xếp hạng của nhiều collection theo hạng (1 của A, 1 của B, 2 của A…)."""
    out: list[dict] = []
    for i in range(max((len(x) for x in lists), default=0)):
        out += [x[i] for x in lists if i < len(x)]
    return out


def search_sparse(text: str, limit: int = 8, query_filter=None, collection: str | None = None) -> list[dict]:
    """Nhánh keyword v2: BM25 trên token bỏ dấu (sparse `bm25`, IDF phía Qdrant). score = BM25.
    collection None = mọi collection chữ; IDF tính riêng từng collection nên điểm BM25 không so chéo được →
    gộp theo hạng (_interleave), mỗi collection tối đa `limit`."""
    from qdrant_client.models import SparseVector

    from sict_crawler.core import sparse
    qv = sparse.query_vector(text)
    if not qv["indices"]:
        return []
    def one(name: str) -> list[dict]:
        res = get_client().query_points(name, query=SparseVector(**qv), using=sparse.VECTOR_NAME,
                                        limit=limit, query_filter=query_filter)
        return [{"score": p.score, "_pid": str(p.id), "_col": name, **p.payload} for p in res.points]
    return _interleave(_per_collection(one, [collection] if collection else text_collections()))


def point_vectors(refs: list[tuple[str, str]]) -> dict[str, list[float]]:
    """[(collection, point id)] → {point id: vector dense đã lưu} (1 lượt retrieve / collection, không nhúng lại)."""
    out: dict[str, list[float]] = {}
    by_col: dict[str, list[str]] = {}
    for col, pid in refs:
        by_col.setdefault(col, []).append(pid)
    for col, ids in by_col.items():
        for p in get_client().retrieve(col, ids, with_vectors=True, with_payload=False):
            v = p.vector.get("") if isinstance(p.vector, dict) else p.vector
            if v is not None:
                out[str(p.id)] = list(v)
    return out


def keyword_search(text: str, terms: list[str], limit: int = 8, query_filter=None,
                   collection: str | None = None) -> list[dict]:
    """Nhánh keyword có xếp hạng: v2 → sparse BM25 (1 lượt gọi / collection); v1 → MatchText từng từ (cũ)."""
    if has_sparse():
        return search_sparse(text, limit=limit, query_filter=query_filter, collection=collection)
    hits: list[dict] = []
    seen: set[tuple] = set()
    for term in terms[:6]:
        try:
            part = scroll_text_by_content(term, limit=limit, query_filter=query_filter)
        except Exception:
            continue
        for h in part:
            k = (h.get("url", ""), h.get("chunk_id", h.get("media_id", "")))
            if k not in seen:
                seen.add(k)
                hits.append(h)
    return hits
