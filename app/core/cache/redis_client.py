"""Redis client factory and small helpers.

Requirements:
- 11.4 — Модуль_Антиспам SHALL хранить счётчики в Кэше (sliding-window).
- 14.1 — FSM хранится в Кэше.
- 17.3/17.4 — health-check c таймаутом 1с на компонент.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis


async def create_redis(redis_url: str) -> Redis:
    """Create an async Redis client ready for use.

    ``socket_timeout`` is intentionally left at the redis-py default
    (``None``, i.e. no timeout) so that long-running blocking commands
    such as ``BRPOP`` (used by the broadcast worker with a 5-second
    poll budget) are not interrupted by a shorter socket timeout.
    Per-call deadlines are enforced explicitly via ``asyncio.wait_for``
    in the health-check (see :func:`redis_ping`).
    ``socket_connect_timeout`` is kept short so a misconfigured host
    fails fast at startup.
    """
    client: Redis = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
    )
    # Fail fast: a single ping to verify connectivity on startup.
    await client.ping()
    return client


async def redis_ping(client: Redis, *, timeout: float = 1.0) -> bool:
    """Return True iff Redis responds within ``timeout`` seconds (Req 17.3/17.4)."""
    try:
        await asyncio.wait_for(client.ping(), timeout=timeout)
        return True
    except Exception:
        return False


async def set_nx_with_ttl(client: Redis, key: str, value: str, ttl_seconds: int) -> bool:
    """SET ``key`` only if absent with TTL; returns True on success."""
    result = await client.set(key, value, nx=True, ex=ttl_seconds)
    return bool(result)


async def sliding_window_incr(
    client: Redis,
    key: str,
    *,
    now_ms: int,
    window_ms: int,
) -> int:
    """ZSET-based sliding window counter.

    Drops entries older than ``now_ms - window_ms``, adds the new one and
    returns the total count in the current window. Used by the anti-spam
    middleware (Requirement 11.3).
    """
    pipe = client.pipeline()
    pipe.zremrangebyscore(key, 0, now_ms - window_ms)
    pipe.zadd(key, {f"{now_ms}-{id(pipe)}": now_ms})
    pipe.zcard(key)
    pipe.pexpire(key, window_ms)
    results = await pipe.execute()
    return int(results[2])
