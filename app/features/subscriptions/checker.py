"""Модуль_Подписок — per-user subscription checker (task 19.1).

For every channel listed in :attr:`~app.config.Settings.required_channels`,
:class:`SubscriptionChecker` answers the question *"is ``user_id`` a member?"*
with a minimum of Telegram API round-trips:

1. Positive answers are cached in Redis under ``subs:ok:{user_id}:{channel}``
   with a 5-minute TTL (design § Модуль_Подписок).
2. On cache miss we call ``bot.get_chat_member(channel, user_id)`` behind a
   5-second ``asyncio.wait_for`` budget; Telegram-side statuses
   ``member`` / ``administrator`` / ``creator`` count as subscribed and are
   written to the cache.
3. When Telegram denies or misbehaves
   (``TelegramForbiddenError``/``TelegramBadRequest``/timeout) we skip the
   channel entirely and record an audit error with ``source="Telegram API"``
   (Requirement 12.4). A skipped channel is *not* reported as missing: the
   user has no way to self-resolve it, and surfacing it would block every
   command indefinitely.

The middleware that consumes :meth:`check` (task 19.2) decides how to render
the ``MissingChannel`` list. This module stays Bot/Redis-only and has no
knowledge of FSM or inline keyboards.

Requirements: 12.1, 12.4.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiogram import Bot
    from redis.asyncio import Redis

    from app.core.services.audit import AuditLog


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables (mirrored in design § Модуль_Подписок and Req 12.1)
# ---------------------------------------------------------------------------
GET_CHAT_MEMBER_TIMEOUT_SECONDS = 5.0
"""Per-channel budget for ``bot.get_chat_member`` (Req 12.1)."""

SUBSCRIBED_CACHE_TTL_SECONDS = 300
"""TTL of the ``subs:ok:{user}:{channel}`` positive cache entry (design)."""

SUBSCRIBED_STATUSES: frozenset[str] = frozenset(
    {"member", "administrator", "creator"}
)
"""Telegram ``ChatMember.status`` values that count as subscribed.

``restricted`` is intentionally excluded — a restricted member may be unable
to see channel content, so design treats them as not subscribed.
"""

_CACHE_KEY_PREFIX = "subs:ok"


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissingChannel:
    """One channel the user still needs to join.

    ``chat_id`` is the identifier from
    :attr:`~app.config.Settings.required_channels` verbatim — either an
    ``@username`` or a ``-100…`` supergroup id — so the middleware can pass
    it back into ``bot.get_chat_member`` on the next probe without any
    re-formatting. ``title`` and ``invite_url`` are best-effort metadata for
    rendering the reply keyboard; either may be ``None`` when the checker
    cannot derive them cheaply.
    """

    chat_id: str
    title: str | None = None
    invite_url: str | None = None


class _Outcome(Enum):
    """Internal per-channel outcome used by :meth:`SubscriptionChecker._probe`."""

    subscribed = "subscribed"
    missing = "missing"
    skip = "skip"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invite_url_for(channel: str) -> str | None:
    """Best-effort ``t.me`` link for rendering the "subscribe" button.

    * ``@channel``          → ``https://t.me/channel``
    * ``https://t.me/…``    → returned as-is
    * numeric ``-100…`` ids → ``None`` (the middleware should fetch
      ``bot.get_chat(...).invite_link`` if it wants a clickable URL).
    """
    if not channel:
        return None
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    if channel.startswith("https://t.me/") or channel.startswith("http://t.me/"):
        return channel
    return None


# ---------------------------------------------------------------------------
# SubscriptionChecker
# ---------------------------------------------------------------------------


class SubscriptionChecker:
    """Cache-first subscription probe for ``required_channels``.

    The class is a simple collaborator: the caller (middleware, task 19.2)
    instantiates one per bot and calls :meth:`check` for every update that
    should be gated on subscription. Failures from the Telegram side are
    audited and swallowed so a bot that lost admin rights on a single
    channel never blocks the entire command set (Req 12.4).

    Notes on wiring (task 19.2+):
    * Build once in ``app/container.py`` under ``settings.feature_subscriptions``
      with ``SubscriptionChecker(bot, redis, audit, settings.required_channels)``.
    * Keep ``required_channels`` ordering — the returned list follows it so
      the middleware can render a deterministic keyboard.
    """

    def __init__(
        self,
        bot: Bot,
        redis: Redis,
        audit: AuditLog,
        required_channels: Sequence[str],
        *,
        cache_ttl_seconds: int = SUBSCRIBED_CACHE_TTL_SECONDS,
        get_chat_member_timeout_seconds: float = GET_CHAT_MEMBER_TIMEOUT_SECONDS,
    ) -> None:
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        if get_chat_member_timeout_seconds <= 0:
            raise ValueError("get_chat_member_timeout_seconds must be positive")
        self._bot = bot
        self._redis = redis
        self._audit = audit
        # Preserve caller-supplied ordering; duplicates are deliberately kept
        # out so we never audit the same API denial twice per update.
        self._required_channels: tuple[str, ...] = tuple(
            dict.fromkeys(c for c in required_channels if c)
        )
        self._cache_ttl_seconds = int(cache_ttl_seconds)
        self._timeout_seconds = float(get_chat_member_timeout_seconds)

    # ------------------------------------------------------------------
    # Read-only accessors (handy for tests / diagnostics)
    # ------------------------------------------------------------------
    @property
    def required_channels(self) -> tuple[str, ...]:
        return self._required_channels

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def check(self, user_id: int) -> list[MissingChannel]:
        """Return the list of channels ``user_id`` is *not* subscribed to.

        Ordering matches :attr:`required_channels`. Channels the bot cannot
        probe (no admin rights, bad chat id, or API timeout) are silently
        skipped after an audit ``record_error`` entry — they appear neither
        in the result nor in the cache.
        """
        if not self._required_channels:
            return []

        missing: list[MissingChannel] = []
        for channel in self._required_channels:
            outcome = await self._probe(user_id, channel)
            if outcome is _Outcome.subscribed:
                continue
            if outcome is _Outcome.skip:
                # Req 12.4 — do not block the user on a channel the bot
                # cannot verify; the audit entry is already written.
                continue
            missing.append(
                MissingChannel(
                    chat_id=channel,
                    title=None,
                    invite_url=_invite_url_for(channel),
                )
            )
        return missing

    async def invalidate(self, user_id: int, channel: str | None = None) -> None:
        """Drop the positive cache entry for a user (single channel or all).

        Exposed for callers that just learned the user left a channel
        (e.g. through an explicit "Re-check" button) so the next
        :meth:`check` forces a real ``get_chat_member`` call.
        """
        if channel is not None:
            await self._redis.delete(self._cache_key(user_id, channel))
            return
        await self._redis.delete(
            *[self._cache_key(user_id, c) for c in self._required_channels]
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_key(user_id: int, channel: str) -> str:
        return f"{_CACHE_KEY_PREFIX}:{user_id}:{channel}"

    async def _probe(self, user_id: int, channel: str) -> _Outcome:
        cache_key = self._cache_key(user_id, channel)
        cached = await self._redis.get(cache_key)
        if cached:
            return _Outcome.subscribed

        # Late import of the aiogram exception classes. Keeping this local
        # lets unit tests import ``SubscriptionChecker`` without aiogram
        # installed and mirrors the style already used by ``user_manager``.
        try:
            from aiogram.exceptions import (
                TelegramBadRequest,
                TelegramForbiddenError,
                TelegramNetworkError,
            )
        except Exception:  # pragma: no cover - exercised only without aiogram
            TelegramBadRequest = TelegramForbiddenError = TelegramNetworkError = (  # type: ignore[assignment]
                Exception
            )

        try:
            member = await asyncio.wait_for(
                self._bot.get_chat_member(channel, user_id),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            await self._audit.record_error(
                source="Telegram API",
                message=(
                    f"subscription check timeout after {self._timeout_seconds}s "
                    f"for channel={channel}"
                ),
                actor_id=user_id,
            )
            return _Outcome.skip
        except TelegramForbiddenError as exc:
            await self._audit.record_error(
                source="Telegram API",
                message=f"no rights in {channel}: {exc!r}",
                actor_id=user_id,
            )
            return _Outcome.skip
        except TelegramBadRequest as exc:
            await self._audit.record_error(
                source="Telegram API",
                message=f"bad_request for {channel}: {exc!r}",
                actor_id=user_id,
            )
            return _Outcome.skip
        except TelegramNetworkError as exc:  # type: ignore[misc]
            await self._audit.record_error(
                source="Telegram API",
                message=f"network error for {channel}: {exc!r}",
                actor_id=user_id,
            )
            return _Outcome.skip
        except Exception as exc:
            # Anything else is unexpected. Log, audit, and skip — the design
            # guarantees "пропускаем канал" on any Telegram-side failure.
            log.exception(
                "subscriptions.check.unexpected_error",
                user_id=user_id,
                channel=channel,
            )
            await self._audit.record_error(
                source="Telegram API",
                message=f"unexpected error for {channel}: {exc!r}",
                actor_id=user_id,
            )
            return _Outcome.skip

        status = getattr(member, "status", None)
        status_value = getattr(status, "value", status)  # enum-or-str tolerant
        if status_value in SUBSCRIBED_STATUSES:
            await self._redis.set(
                cache_key, "1", ex=self._cache_ttl_seconds
            )
            return _Outcome.subscribed
        return _Outcome.missing


__all__ = [
    "GET_CHAT_MEMBER_TIMEOUT_SECONDS",
    "SUBSCRIBED_CACHE_TTL_SECONDS",
    "SUBSCRIBED_STATUSES",
    "MissingChannel",
    "SubscriptionChecker",
]
