"""PostgreSQL async engine + session.

Callers: main.py, api.py.
Schemas: creates tables from models.Base on init_db().
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().postgres_dsn, pool_size=10, echo=False)
    return _engine


def get_session() -> AsyncSession:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory()


# cột thêm sau create_all (bảng đã tồn tại). TODO Giai đoạn 3: chuyển sang Alembic migration.
_ADD_COLUMNS = (
    ("media_assets", "timestamp", "DOUBLE PRECISION"),
    ("media_assets", "persons", "JSONB DEFAULT '{}'::jsonb"),
    # index spec v2 trên documents (bảng media/media_occurrence/videos mới do create_all tạo)
    ("documents", "doc_key", "VARCHAR(16)"), ("documents", "section", "VARCHAR(80)"),
    ("documents", "doc_type", "VARCHAR(16)"), ("documents", "published_at", "TIMESTAMPTZ"),
    ("documents", "year", "INTEGER"), ("documents", "parent_url", "VARCHAR(2048)"),
    ("documents", "hash_v2", "VARCHAR(64)"),
    # graph D20
    ("entity_links", "evidence", "VARCHAR(300) DEFAULT ''"), ("entity_links", "chunk_id", "VARCHAR(36)"),
    ("entities", "attrs", "JSONB DEFAULT '{}'::jsonb"), ("media_occurrence", "context", "TEXT DEFAULT ''"),
    ("entity_links", "roles", "JSONB DEFAULT '[]'::jsonb"), ("entity_relations", "roles", "JSONB DEFAULT '{}'::jsonb"),
    # D23: cấu trúc trong trang (outline block) + hash cấu trúc; vị trí ảnh/video trong bài
    ("documents", "structure", "JSONB"), ("documents", "hash_struct", "VARCHAR(64)"),
    ("media_occurrence", "block_idx", "INTEGER"), ("media_occurrence", "heading_path", "JSONB DEFAULT '[]'::jsonb"),
    ("video_occurrence", "block_idx", "INTEGER"),
)


async def init_db():
    """create_all + thêm cột còn THIẾU. Chỉ ALTER khi cột chưa có (ALTER cần khoá độc quyền: chạy khi
    tiến trình khác — incremental, loader, graphsync — đang giữ giao dịch sẽ xếp hàng và chặn mọi truy
    vấn phía sau). lock_timeout để không bao giờ treo vô hạn."""
    async with get_engine().begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        await conn.run_sync(Base.metadata.create_all)
        have = {(t, c) for t, c in (await conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
        ))).all()}
        for tbl, col, typ in _ADD_COLUMNS:
            if (tbl, col) not in have:
                await conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {typ}"))
        for col in ("doc_key", "parent_url", "hash_v2"):
            await conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_documents_{col} ON documents ({col})"))
