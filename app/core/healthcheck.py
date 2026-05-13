"""Health-check endpoint handler.

Requirements 17.3 / 17.4:
- Responds within 2 seconds.
- Per-component probe timeout: 1 second.
- Reports ``available`` / ``unavailable`` separately for DB and Cache.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.cache.redis_client import redis_ping
from app.core.db.engine import db_ping

KEY_DB = "db"
KEY_CACHE = "cache"
STATUS_OK = "available"
STATUS_BAD = "unavailable"


def healthz_factory(engine: AsyncEngine, redis: Redis) -> Any:
    async def healthz(_request: web.Request) -> web.Response:
        db_task = asyncio.create_task(db_ping(engine, timeout=1.0))
        cache_task = asyncio.create_task(redis_ping(redis, timeout=1.0))
        results = await asyncio.gather(db_task, cache_task, return_exceptions=True)

        body = {
            KEY_DB: STATUS_OK if results[0] is True else STATUS_BAD,
            KEY_CACHE: STATUS_OK if results[1] is True else STATUS_BAD,
        }
        status = 200 if all(v == STATUS_OK for v in body.values()) else 503
        return web.json_response(body, status=status)

    return healthz
