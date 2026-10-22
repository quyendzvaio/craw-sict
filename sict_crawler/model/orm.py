"""SQLAlchemy models.

Index spec v2 (collection Qdrant *_v2, D2/D8): Postgres chỉ giữ metadata + quan hệ, nội dung chunk
nằm trong payload Qdrant. Point id = uuid5 cố định (doc_rules.stable_id) nên không cần lưu id từng chunk.
  documents(id, url, title, raw_s3_key, content_hash, meta, crawled_at,
            doc_key, section, doc_type, published_at, year, parent_url, hash_v2)
  media(id, sha256 unique, kind[image|video_frame], s3_key, source_url, ocr_text, ocr_status,
        ocr_attempts, video_id, timestamp, point_id, frame_sets)  — 1 dòng / 1 file media
  media_occurrence(document_id, media_id, alt, pos, caption_point_id)  — 1 dòng / 1 lần xuất hiện
  videos(id, video_key unique [YouTube id], url, title, subs, frames, status)
  video_occurrence(document_id, video_id, caption_point_id)
  entities(id, kind[person|event|org], key, name, aliases, doc_count)   — graph D20 (graph.py)
  entity_links(entity_id, document_id, media_id?, video_id?, rel[about|mentions|depicts], weight,
               source, evidence)
  entity_aliases(kind, alias_key unique, entity_key|NULL=chặn, source[staff|review|manual])
  entity_review(kind, key, surface, reason, candidate_key, document_id, context, status) — hàng chờ duyệt
  entity_relations(src_id < dst_id, rel "<kind>-<kind>", weight, doc_count, media_count, doc_ids)
               — thực thể ↔ thực thể suy từ entity_links (graph.relations_from_links)
Bảng v1 (giữ tới khi xoá collection cũ — rollback): chunks, media_assets.

Callers: db.py, main.py, api.py, indexer.py, loader.py.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("url", name="uq_documents_url"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(512), default="")
    raw_s3_key: Mapped[str] = mapped_column(String(512), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    crawled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc),
        index=True)  # recheck ORDER BY crawled_at
    # index spec v2 (indexing.DocMeta). NULL = tài liệu chỉ có ở index v1 (listing, trang trùng...)
    doc_key: Mapped[str | None] = mapped_column(String(16), default=None, index=True)
    section: Mapped[str | None] = mapped_column(String(80), default=None)
    doc_type: Mapped[str | None] = mapped_column(String(16), default=None)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    year: Mapped[int | None] = mapped_column(Integer, default=None)
    parent_url: Mapped[str | None] = mapped_column(String(2048), default=None, index=True)
    # sha1 markdown sạch (trang) | sha256 file (đính kèm). Không dùng hash HTML thô: bộ đếm lượt
    # xem trong HTML đổi mỗi lần tải → recheck tưởng bài nào cũng sửa.
    hash_v2: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    # D23: cấu trúc trong trang. structure = outline block (indexing.outline, không lặp toàn văn);
    # hash_struct = sha1 outline — đổi khi thêm/bớt/đổi chỗ heading, bảng, ảnh, video, file (hash_v2 không thấy)
    structure: Mapped[list | None] = mapped_column(JSONB, default=None)
    hash_struct: Mapped[str | None] = mapped_column(String(64), default=None)

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    media: Mapped[list["MediaAsset"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(String(512), default="")
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    vector_id: Mapped[str] = mapped_column(String(64), default="")  # qdrant point id

    document: Mapped[Document] = relationship(back_populates="chunks")


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # image | video_frame
    s3_key: Mapped[str] = mapped_column(String(512), default="", index=True)
    base64_data: Mapped[str] = mapped_column(Text, default="")  # legacy, S3-only từ 2026-09: luôn ""
    source_url: Mapped[str] = mapped_column(String(2048), default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[float | None] = mapped_column(default=None)  # video_frame pts seconds
    persons: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"faces": n, "person": str|None, "via": "arcface|vlm|page"}
    vector_id: Mapped[str] = mapped_column(String(64), default="")

    document: Mapped[Document] = relationship(back_populates="media")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    video_key: Mapped[str] = mapped_column(String(64), unique=True)  # YouTube id | sha1(url)[:16]
    url: Mapped[str] = mapped_column(String(2048), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    subs: Mapped[str] = mapped_column(Text, default="")
    frames: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | blocked (không tải được)


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # image | video_frame
    s3_key: Mapped[str] = mapped_column(String(512), default="")
    source_url: Mapped[str] = mapped_column(String(2048), default="", index=True)
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|done|failed
    ocr_attempts: Mapped[int] = mapped_column(Integer, default=0)
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), default=None, index=True)
    timestamp: Mapped[float | None] = mapped_column(Float, default=None)
    point_id: Mapped[str] = mapped_column(String(36), default="")  # point sict_image_v2
    # khung video: collection ảnh mà khung thuộc bộ khung của nó (sict_image_v2 / sict_image_v3). PG dùng chung
    # cho blue/green: mỗi bản compute trích khung ở thời điểm khác → 1 video mang nhiều bộ; index lại chỉ lấy
    # bộ của collection đang phục vụ (indexer.pick_frame_set), bộ cũ dọn khi bỏ hẳn collection đó (reconcile)
    frame_sets: Mapped[list] = mapped_column(JSONB, default=list)


class MediaOccurrence(Base):
    __tablename__ = "media_occurrence"
    __table_args__ = (UniqueConstraint("document_id", "media_id", name="uq_media_occ"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), index=True)
    alt: Mapped[str] = mapped_column(Text, default="")
    pos: Mapped[int] = mapped_column(Integer, default=0)
    context: Mapped[str] = mapped_column(Text, default="")  # đoạn văn sát ảnh (graph D20 dựng lại từ PG)
    block_idx: Mapped[int | None] = mapped_column(Integer, default=None)  # D23: vị trí block trong documents.structure
    heading_path: Mapped[list] = mapped_column(JSONB, default=list)       # D23: mục chứa ảnh
    caption_point_id: Mapped[str] = mapped_column(String(36), default="")  # point sict_text_v2


class VideoOccurrence(Base):
    __tablename__ = "video_occurrence"
    __table_args__ = (UniqueConstraint("document_id", "video_id", name="uq_video_occ"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    block_idx: Mapped[int | None] = mapped_column(Integer, default=None)  # D23
    caption_point_id: Mapped[str] = mapped_column(String(36), default="")


class GraphEntity(Base):
    """Thực thể graph (D20). Khoá = chữ bỏ dấu (graph.py); payload Qdrant dùng "<kind>:<key>"."""
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_entities_kind_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(300), default="")
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    attrs: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"staff", "honorific", "profile_url"}


class EntityLink(Base):
    """Cạnh graph. Luôn gắn 1 tài liệu (bằng chứng nằm trong tài liệu đó) → index lại / xoá bài thì
    xoá theo document_id. media_id/video_id có khi rel = depicts."""
    __tablename__ = "entity_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[int | None] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), default=None, index=True)
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), default=None)
    chunk_id: Mapped[str | None] = mapped_column(String(36), default=None)  # mentions: chunk nhắc tên
    rel: Mapped[str] = mapped_column(String(16))      # about | mentions | depicts
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="")  # doc | alt | context | ocr
    roles: Mapped[list] = mapped_column(JSONB, default=list)  # vai trò trong bài (LLM, giai đoạn C)
    evidence: Mapped[str] = mapped_column(String(300), default="")  # đoạn trích chứa tên (AC-C6)


class EntityRelation(Base):
    """Cạnh thực thể ↔ thực thể (vô hướng, lưu 1 lần với src_id < dst_id), suy từ việc cùng xuất
    hiện trên 1 ảnh / cùng ở tiêu đề / tiêu đề + thân bài (graph.relations_from_links). Tính lại toàn
    bộ mỗi lượt incremental (vài giây) nên không cần cập nhật từng phần."""
    __tablename__ = "entity_relations"
    __table_args__ = (UniqueConstraint("src_id", "dst_id", name="uq_entity_rel"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    src_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    dst_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    rel: Mapped[str] = mapped_column(String(32))          # "person-event", "person-person", ...
    weight: Mapped[float] = mapped_column(Float, default=0.0)   # noisy-OR các bằng chứng, ≤ 1
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    media_count: Mapped[int] = mapped_column(Integer, default=0)  # số ảnh/video nêu cả hai
    doc_ids: Mapped[list] = mapped_column(JSONB, default=list)    # ≤ 10 bài làm chứng mạnh nhất
    roles: Mapped[dict] = mapped_column(JSONB, default=dict)     # {"src": [vai trò], "dst": [...]} trong các bài chung


class EntityAlias(Base):
    """Alias / chặn ĐÃ DUYỆT (nguồn sự thật cho gộp tên): (kind, alias_key) → entity_key đích;
    entity_key NULL = chặn (không phải thực thể). Lưu theo khoá chữ, không theo id, để dựng lại
    graph (id đổi) vẫn áp được. Alias do luật tự sinh nằm ở entities.aliases (tính lại mỗi lần)."""
    __tablename__ = "entity_aliases"
    __table_args__ = (UniqueConstraint("kind", "alias_key", name="uq_entity_alias"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    alias_key: Mapped[str] = mapped_column(String(300))
    entity_key: Mapped[str | None] = mapped_column(String(300), default=None)
    source: Mapped[str] = mapped_column(String(16), default="review")  # review | manual
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


class EntityReview(Base):
    """Hàng chờ duyệt: tên người gần giống người đã có (không tự gộp), thực thể nghi nhiễu, và (giai
    đoạn C) trích xuất LLM độ tin thấp. Duyệt xong → entity_aliases (graphsync review-import)."""
    __tablename__ = "entity_review"
    __table_args__ = (UniqueConstraint("kind", "key", "reason", "candidate_key", name="uq_entity_review"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(300))
    surface: Mapped[str] = mapped_column(String(300), default="")
    reason: Mapped[str] = mapped_column(String(32))          # near_duplicate | llm_low_conf | ...
    candidate_key: Mapped[str] = mapped_column(String(300), default="")
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), default=None)
    context: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | merged | distinct | blocked
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
