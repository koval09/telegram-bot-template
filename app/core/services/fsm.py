"""Менеджер_Состояний — thin wrapper around aiogram's RedisStorage.

The wrapper pings Redis before each state mutation (Requirement 14.4). On a
Redis outage we answer the user with a localized “dialogs unavailable”
message and log an error — we do NOT crash the process.
"""

from __future__ import annotations

import asyncio

import structlog
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

log = structlog.get_logger(__name__)


def create_fsm_storage(redis: Redis, *, ttl_seconds: int) -> BaseStorage:
    """Return a ``RedisStorage`` with per-key TTL equal to ``ttl_seconds``.

    aiogram's ``RedisStorage`` accepts ``state_ttl``/``data_ttl`` as
    ``int`` seconds (or ``None``). We set both so idle dialogs are purged.
    """
    return RedisStorage(
        redis=redis,
        state_ttl=ttl_seconds,
        data_ttl=ttl_seconds,
    )


async def redis_is_healthy(redis: Redis, *, timeout: float = 0.3) -> bool:
    """Pre-write probe used by handlers to fail fast on Redis outage."""
    try:
        await asyncio.wait_for(redis.ping(), timeout=timeout)
        return True
    except Exception as exc:
        log.warning("fsm.redis_unavailable", error=repr(exc))
        return False
