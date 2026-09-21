"""Qdrant client: text + image collections, upsert, hybrid search.

Callers: main.py, api.py.
Schemas: text collection payload {document_id, chunk_id, url, title, heading, content};
         image collection payload {media_id, document_id, url, kind[image|video_frame],
         s3_key, caption[, timestamp]}.
"""
from __future__ import annotations

import time
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, FilterSelector, MatchValue, PointStruct,
    SearchParams, VectorParams,
)

from .config import get_settings

_client = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        cfg = get_settings().qdrant
        _client = QdrantClient(host=cfg.host, port=cfg.port, grpc_port=cfg.grpc_port,
                               prefer_grpc=cfg.prefer_grpc, timeout=cfg.timeout)
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


def ensure_collections():
    cfg = get_settings().qdrant
    client = get_client()
    for name, dim in [(cfg.collection_text, cfg.text_dim), (cfg.collection_image, cfg.image_dim)]:
        if not client.collection_exists(name):
            client.create_collection(
                name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    ensure_payload_indexes()


def ensure_payload_indexes():
    """Integer index on document_id (both collections) so delete/filter by
    document no longer full-scans 20k-75k points. Idempotent."""
    cfg = get_settings().qdrant
    client = get_client()
    for name in (cfg.collection_text, cfg.collection_image):
        try:
            schema = client.get_collection(name).payload_schema or {}
        except Exception:
            continue  # collection vừa tạo ở ensure_collections, bỏ qua
        if "document_id" not in schema:
            client.create_payload_index(name, field_name="document_id",
                                        field_schema="integer")
            print(f"[qdrant] payload index document_id → {name}")


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
    title, caption, timestamp}. E5 vectors → sict_text so text queries hit media."""
    cfg = get_settings().qdrant
    client = get_client()
    points, ids = [], []
    for r in rows:
        pid = str(_pid())
        ids.append(pid)
        points.append(PointStruct(id=pid, vector=r["vector"], payload=r))
    _with_retry(lambda: client.upsert(cfg.collection_text, points))
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
    """Remove every point belonging to a document (chunks, media captions, images)."""
    cfg = get_settings().qdrant
    client = get_client()
    sel = FilterSelector(filter=Filter(must=[
        FieldCondition(key="document_id", match=MatchValue(value=document_id))]))
    _with_retry(lambda: client.delete(cfg.collection_text, points_selector=sel))
    _with_retry(lambda: client.delete(cfg.collection_image, points_selector=sel))

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
                filter_document_id: int | None = None) -> list[dict]:
    cfg = get_settings().qdrant
    client = get_client()
    flt = None
    if filter_document_id is not None:
        flt = Filter(must=[FieldCondition(key="document_id", value=MatchValue(value=filter_document_id))])
    res = client.query_points(cfg.collection_text, query=query_vector, limit=limit,
                              search_params=SearchParams(exact=False), query_filter=flt)
    return [{"score": p.score, **p.payload} for p in res.points]


def search_images(query_vector: list[float], limit: int = 5) -> list[dict]:
    cfg = get_settings().qdrant
    client = get_client()
    res = client.query_points(cfg.collection_image, query=query_vector, limit=limit)
    return [{"score": p.score, **p.payload} for p in res.points]


def scroll_text_by_content(term: str, limit: int = 8) -> list[dict]:
    """Keyword leg for hybrid search: payload MatchText on content + caption.
    No vector query — order is scroll order; caller fuses by rank (RRF)."""
    from qdrant_client.models import MatchText
    cfg = get_settings().qdrant
    client = get_client()
    hits: list[dict] = []
    for key in ("content", "caption"):
        flt = Filter(must=[FieldCondition(key=key, match=MatchText(text=term))])
        points, _ = client.scroll(cfg.collection_text, scroll_filter=flt,
                                  limit=limit, with_payload=True)
        for p in points:
            hits.append({"score": 0.0, **p.payload})
    return hits
