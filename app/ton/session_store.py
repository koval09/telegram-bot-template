"""Redis-backed storage adapter for ``pytonconnect``.

``pytonconnect.TonConnect`` persists per-session key/value pairs through an
``IStorage`` implementation. We back it with the shared ``redis.asyncio.Redis``
client so sessions are shared across process workers and survive restarts.

Design references:
- design.md (TON Connect section): ``tc:session:<telegram_id>`` is a
  Redis-backed ``IStorage`` used by ``pytonconnect``.
- design.md (Redis key table): key ``tc:session:{id}`` stores TON Connect
  session state with a 600-second TTL.

Requirements: 3.5 — The TON_Connector SHALL mark a connection session as
expired and free associated resources if the user does not complete it within
10 minutes. Each write refreshes the 600s TTL; ``tc_session_cleanup`` (task
15.2) handles server-side expiration and user notification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.asyncio import Redis

# ``pytonconnect`` is an optional runtime dependency (feature-gated behind
# ``feature_ton_connector``). When it is unavailable we fall back to a local
# stub with the same public surface so this module stays importable for
# linting, typing and unit tests that do not exercise the real SDK.
try:  # pragma: no cover - trivial import guard
    from pytonconnect.storage import IStorage as _IStorage
except ImportError:  # pragma: no cover - fallback for envs without the SDK

    class _IStorage:  # type: ignore[no-redef]
        """Fallback base matching ``pytonconnect.storage.IStorage``.

        Kept minimal: ``pytonconnect`` only calls ``set_item``/``get_item``/
        ``remove_item`` on this object. The real SDK ships an abstract class
        with the same three coroutines; subclasses override them.
        """

        async def set_item(self, key: str, value: str) -> None:
            raise NotImplementedError

        async def get_item(
            self, key: str, default_value: str | None = None
        ) -> str | None:
            raise NotImplementedError

        async def remove_item(self, key: str) -> None:
            raise NotImplementedError


DEFAULT_TTL_SECONDS = 600
"""Bound from Requirement 3.5: 10-minute TON Connect session window."""

_KEY_PREFIX = "tc:session"


class RedisSessionStore(_IStorage):
    """``pytonconnect.storage.IStorage`` implementation over Redis.

    One instance is bound to a single ``telegram_id`` so all keys written by
    ``pytonconnect`` for that user share the ``tc:session:{telegram_id}:*``
    namespace. Every ``set_item`` refreshes the TTL — the session is alive as
    long as the user is interacting, and expires 600 s after the last write
    (Requirement 3.5).

    The ``tc_session_cleanup`` APScheduler job (task 15.2) additionally
    scans ``tc:session:*`` and closes timed-out sessions via
    ``pytonconnect.disconnect``.
    """

    __slots__ = ("_redis", "_telegram_id", "_ttl")

    def __init__(
        self,
        redis: Redis,
        telegram_id: int,
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self._redis = redis
        self._telegram_id = int(telegram_id)
        self._ttl = int(ttl)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _key(self, key: str) -> str:
        """Build the namespaced Redis key for a ``pytonconnect`` sub-key."""
        return f"{_KEY_PREFIX}:{self._telegram_id}:{key}"

    @property
    def telegram_id(self) -> int:
        """Telegram user this storage instance belongs to."""
        return self._telegram_id

    @property
    def ttl(self) -> int:
        """Per-write TTL in seconds (Requirement 3.5)."""
        return self._ttl

    # ------------------------------------------------------------------
    # IStorage interface
    # ------------------------------------------------------------------
    async def set_item(self, key: str, value: str) -> None:
        """Store ``value`` at ``key`` with a fresh 600-second TTL.

        ``pytonconnect`` hands us string values; we persist them verbatim so
        the SDK controls its own serialization (JSON).
        """
        await self._redis.set(self._key(key), value, ex=self._ttl)

    async def get_item(
        self, key: str, default_value: str | None = None
    ) -> str | None:
        """Return the stored string or ``default_value`` when the key is missing."""
        value = await self._redis.get(self._key(key))
        if value is None:
            return default_value
        return value

    async def remove_item(self, key: str) -> None:
        """Delete the key if present. Idempotent: no error when it is missing."""
        await self._redis.delete(self._key(key))
