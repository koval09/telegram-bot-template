"""Async SQLAlchemy engine factory and a small DB ping helper."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(dsn: str) -> AsyncEngine:
    """Create a pooled async engine.

    We enable ``pool_pre_ping`` so stale connections (e.g. after a DB
    restart) are recycled transparently.
    """
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    # SQLite has no connection pool in the traditional sense
    if not dsn.startswith("sqlite"):
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 5
    return create_async_engine(dsn, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def db_ping(engine: AsyncEngine, *, timeout: float = 1.0) -> bool:
    """Return True iff the DB answers ``SELECT 1`` within ``timeout`` seconds.

    Requirements 17.3 / 17.4.
    """
    async def _do() -> bool:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    try:
        return await asyncio.wait_for(_do(), timeout=timeout)
    except Exception:
        return False
