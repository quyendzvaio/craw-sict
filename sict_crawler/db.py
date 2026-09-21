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


async def init_db():
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all does not ALTER existing tables → add new media_assets columns
        await conn.execute(text(
            "ALTER TABLE media_assets ADD COLUMN IF NOT EXISTS timestamp DOUBLE PRECISION"))
        await conn.execute(text(
            "ALTER TABLE media_assets ADD COLUMN IF NOT EXISTS persons JSONB DEFAULT '{}'::jsonb"))
