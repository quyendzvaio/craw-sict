"""SQLAlchemy models: documents, chunks, media_assets.

Callers: db.py, main.py, api.py.
Schemas:
  documents(id, url, title, raw_s3_key, content_hash, crawled_at)
  chunks(id, document_id, content, heading, chunk_index, token_count)
  media_assets(id, document_id, kind[image|video_frame], s3_key, base64_data, source_url,
               caption, timestamp, persons JSONB {faces, person, via})
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
