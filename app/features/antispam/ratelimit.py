"""Sliding-window rate limiter for ``Модуль_Антиспам``.

Implements the primitive described in design § антиспам and task 18.1:

- **Req 11.3** — while a user sends more than 5 messages in 3 seconds, the
  bot must temporarily ignore their messages for 30 seconds and log the
  event to the audit journal.
- **Req 11.4** — counters live in the cache (Redis), not the database.

Keys in Redis (one entry per user):

- ``rl:{user_id}``       — ZSET of timestamps of recent events (sliding
  window). Expires after the window; each event's score is the Unix time
  in milliseconds.
- ``rl:block:{user_id}`` — the "blocked" marker. ``SET key 1 NX EX 30``
  establishes a fresh 30-second block; ``NX`` guarantees that only the
  first event that crosses the threshold receives ``block_triggered=True``.

All writes are executed atomically via a single Lua script so that
concurrent updates from the same user cannot lose an event or double-fire
the block-triggered notification.

The class exposes a plain API the middleware (task 18.3) can call:

.. code-block:: python

    rl = RateLimiter(redis, audit=audit)
    result = await rl.check_and_record(user_id)
    if not result.allowed:
        if result.block_triggered:
            await message.answer("Слишком много сообщений")
        return  # swallow the update

``audit`` is optional so unit tests (and the middleware) can pass a custom
callback. When provided, the rate limiter writes a single
``audit.record_warning(event="ratelimit_blocked", ...)`` entry the first
time a user enters a new block window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from redis.asyncio import Redis

from app.core.utils.clock import Clock, utc_now

if TYPE_CHECKING:  # pragma: no cover
    from app.core.services.audit import AuditLog

log = structlog.get_logger(__name__)

# Defaults from design § антиспам and requirement 11.3.
DEFAULT_WINDOW_SECONDS = 3.0
DEFAULT_MAX_EVENTS = 5
DEFAULT_BLOCK_SECONDS = 30
DEFAULT_KEY_PREFIX = "rl"


# --------------------------------------------------------------------------
# Atomic Lua script: check block, insert event, maybe block.
# --------------------------------------------------------------------------
#
# KEYS[1] — rl:{user_id}        (ZSET of recent events)
# KEYS[2] — rl:block:{user_id}  (block marker)
# ARGV[1] — now_ms              (int)
# ARGV[2] — window_ms           (int)
# ARGV[3] — max_events          (int; block is triggered on count > max_events)
# ARGV[4] — block_seconds       (int)
# ARGV[5] — member              (unique ZSET member for this event)
#
# Returns a 4-element array:
#   [1] allowed          — 1 if the handler may proceed, 0 if blocked
#   [2] block_triggered  — 1 iff this call freshly started a new block
#   [3] retry_after_ms   — remaining block TTL in ms (0 when allowed)
#   [4] count            — current event count in the window (debug/telemetry)
_CHECK_AND_RECORD_LUA = """
local rl_key = KEYS[1]
local block_key = KEYS[2]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local max_events = tonumber(ARGV[3])
local block_seconds = tonumber(ARGV[4])
local member = ARGV[5]

-- Fast path: the user is already in a block window.
local ttl = redis.call('PTTL', block_key)
if ttl and ttl > 0 then
  return {0, 0, ttl, 0}
end

-- Sliding-window insert: add → evict → count.
redis.call('ZADD', rl_key, now_ms, member)
redis.call('ZREMRANGEBYSCORE', rl_key, 0, now_ms - window_ms)
local count = redis.call('ZCARD', rl_key)
redis.call('PEXPIRE', rl_key, window_ms)

if count > max_events then
  -- SET NX: only the first crossing of the threshold trips the block.
  local set_reply = redis.call('SET', block_key, '1', 'NX', 'EX', block_seconds)
  local triggered = 0
  if set_reply then triggered = 1 end
  return {0, triggered, block_seconds * 1000, count}
end

return {1, 0, 0, count}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a single ``check_and_record`` call.

    Attributes:
        allowed: ``True`` when the handler may proceed; ``False`` when the
            update should be swallowed by the middleware.
        block_triggered: ``True`` *only* when this call caused a fresh block
            to start. The middleware uses this to send exactly one
            "Слишком много сообщений" reply per block window and to write a
            single ``audit`` warning (Req 11.3).
        retry_after_seconds: Seconds remaining on the block; ``0`` when
            ``allowed`` is ``True``.
        count: Current number of events in the sliding window. Exposed for
            telemetry/tests; middleware can ignore it.
    """

    allowed: bool
    block_triggered: bool
    retry_after_seconds: int
    count: int


class RateLimiter:
    """Redis sliding-window rate limiter with sticky block window.

    This object owns the Lua script and is safe to share across tasks/
    middleware. The class does no I/O in ``__init__``; the first call to
    ``check_and_record`` (or ``_script``) registers the script with Redis.

    Args:
        redis: Async Redis client (``redis.asyncio.Redis``).
        window_seconds: Width of the sliding window (default ``3.0`` s,
            per design § антиспам).
        max_events: Block is triggered when the in-window count exceeds
            this value (default ``5``, per Req 11.3).
        block_seconds: Duration of the block window (default ``30`` s, per
            Req 11.3).
        key_prefix: Redis key prefix. ``rl`` by default; override for tests
            or to namespace multiple bots on one Redis.
        audit: Optional ``AuditLog``; when provided, the limiter records one
            ``record_warning(event="ratelimit_blocked", ...)`` entry per
            block window. If ``None``, the middleware is expected to do so
            itself using ``RateLimitResult.block_triggered``.
        clock: Injectable clock (for tests); defaults to ``utc_now``.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_events: int = DEFAULT_MAX_EVENTS,
        block_seconds: int = DEFAULT_BLOCK_SECONDS,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        audit: AuditLog | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if block_seconds < 1:
            raise ValueError("block_seconds must be >= 1")
        self._redis = redis
        self._window_ms = int(window_seconds * 1000)
        self._max_events = max_events
        self._block_seconds = block_seconds
        self._prefix = key_prefix.rstrip(":")
        self._audit = audit
        self._clock = clock
        # redis.asyncio exposes register_script which returns a callable that
        # uses EVALSHA with an EVAL fallback — exactly what we want here.
        self._script = self._redis.register_script(_CHECK_AND_RECORD_LUA)

    # ------------------------------------------------------------------
    # Configuration accessors (read-only; useful for the middleware log)
    # ------------------------------------------------------------------
    @property
    def window_seconds(self) -> float:
        return self._window_ms / 1000.0

    @property
    def max_events(self) -> int:
        return self._max_events

    @property
    def block_seconds(self) -> int:
        return self._block_seconds

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------
    def _events_key(self, user_id: int) -> str:
        return f"{self._prefix}:{user_id}"

    def _block_key(self, user_id: int) -> str:
        return f"{self._prefix}:block:{user_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def is_blocked(self, user_id: int) -> int:
        """Return the remaining block TTL in seconds, or ``0`` when active.

        Used by the middleware as a fast pre-check when it only needs to
        know whether the user is currently silenced. Does NOT record an
        event; use ``check_and_record`` for the normal update path.
        """
        ttl_ms = await self._redis.pttl(self._block_key(user_id))
        if ttl_ms is None or ttl_ms <= 0:
            return 0
        # PTTL returns -2 for missing and -1 for no-expire; treat both as 0.
        return max(0, (int(ttl_ms) + 999) // 1000)

    async def reset(self, user_id: int) -> None:
        """Clear both the sliding window and any active block for a user.

        Intended for admin / test helpers; not used by the middleware.
        """
        await self._redis.delete(self._events_key(user_id), self._block_key(user_id))

    async def check_and_record(self, user_id: int) -> RateLimitResult:
        """Register a new event and return whether the update may proceed.

        Behaviour matches task 18.1 / Req 11.3:

        1. If ``rl:block:{user_id}`` is set → return
           ``RateLimitResult(allowed=False, block_triggered=False, ...)``.
        2. Otherwise add ``now`` to the ``rl:{user_id}`` ZSET, evict
           entries older than the window, and count.
        3. If the count exceeds ``max_events``, ``SET NX`` the block marker
           with a 30-second TTL. The first call that flips the block
           reports ``block_triggered=True``.

        The operation is atomic (single Lua round-trip), so concurrent
        updates from the same user cannot lose events or double-fire the
        block notification.
        """
        now_ms = int(self._clock().timestamp() * 1000)
        # Unique ZSET member — two events in the same millisecond must not
        # collide (ZADD would otherwise overwrite the prior member).
        member = f"{now_ms}-{uuid.uuid4().hex}"

        raw = await self._script(
            keys=[self._events_key(user_id), self._block_key(user_id)],
            args=[
                now_ms,
                self._window_ms,
                self._max_events,
                self._block_seconds,
                member,
            ],
        )
        allowed_flag, triggered_flag, retry_after_ms, count = (
            int(raw[0]),
            int(raw[1]),
            int(raw[2]),
            int(raw[3]),
        )
        result = RateLimitResult(
            allowed=bool(allowed_flag),
            block_triggered=bool(triggered_flag),
            retry_after_seconds=(retry_after_ms + 999) // 1000
            if retry_after_ms > 0
            else 0,
            count=count,
        )

        if result.block_triggered:
            log.warning(
                "antispam.ratelimit.blocked",
                user_id=user_id,
                count=count,
                window_seconds=self.window_seconds,
                block_seconds=self._block_seconds,
            )
            if self._audit is not None:
                # The audit write must never bubble up — a broken journal
                # cannot be allowed to keep a blocked user out.
                try:
                    await self._audit.record_warning(
                        event="ratelimit_blocked",
                        actor_id=user_id,
                        details={
                            "count": count,
                            "window_seconds": self.window_seconds,
                            "block_seconds": self._block_seconds,
                            "max_events": self._max_events,
                        },
                    )
                except Exception as exc:
                    log.error(
                        "antispam.ratelimit.audit_failed",
                        user_id=user_id,
                        error=repr(exc),
                    )

        return result
