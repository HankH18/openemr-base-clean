"""SQLAlchemy 2 async engine + session factory + declarative Base."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator, TypeEngine

from copilot.config import get_settings


class JSONType(TypeDecorator[dict]):
    """JSONB on Postgres, JSON on SQLite — same interface for tests + prod."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: object) -> TypeEngine[dict]:  # type: ignore[override]
        if getattr(dialect, "name", "") == "postgresql":
            return dialect.type_descriptor(JSONB())  # type: ignore[attr-defined]
        return dialect.type_descriptor(JSON())  # type: ignore[attr-defined]


class Base(DeclarativeBase):
    """Common declarative base — every model inherits from this."""


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Async engine keyed off the cached Settings.

    Cached because engine construction opens a connection pool; a single
    process holds one engine for its lifetime.
    """
    settings = get_settings()
    return create_async_engine(settings.database_url, future=True, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the engine."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession; commit on success, rollback on exception."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
