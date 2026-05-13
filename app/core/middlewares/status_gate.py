"""Status gate — enforces ban/mute/captcha state on every update.

Requirements:
- 2.4 — banned users get a blocked message and no profile data.
- 5.2 — banned users are silenced with at most 1 notice per 24 hours.
- 5.4 — muted users are silenced with at most 1 notice per 10 minutes.
- 5.5 — expired mutes auto-lift to ``active`` on the next update.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis

from app.core.cache.redis_client import set_nx_with_ttl
from app.core.db.models import User, UserStatus
from app.core.repositories.users import UsersRepo
from app.core.utils.clock import Clock, utc_now

log = structlog.get_logger(__name__)

BAN_NOTIFY_TTL = 24 * 60 * 60  # 24 hours (Req 5.2)
MUTE_NOTIFY_TTL = 10 * 60  # 10 minutes (Req 5.4)

MSG_BANNED = "Ваш доступ к боту ограничен."
MSG_MUTED = "Вы временно не можете писать боту."


class StatusGateMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, users: UsersRepo, *, clock: Clock = utc_now) -> None:
        self._redis = redis
        self._users = users
        self._clock = clock

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("user")
        if user is None:
            return await handler(event, data)

        if user.status == UserStatus.banned:
            await self._maybe_notify(event, user.telegram_id, "ban", MSG_BANNED, BAN_NOTIFY_TTL)
            return None

        if user.status == UserStatus.muted:
            now = self._clock()
            if user.muted_until is not None and user.muted_until <= now:
                # Auto-lift — Req 5.5.
                await self._users.set_status(user.telegram_id, UserStatus.active, now=now)
                user.status = UserStatus.active
                user.muted_until = None
            else:
                await self._maybe_notify(
                    event, user.telegram_id, "mute", MSG_MUTED, MUTE_NOTIFY_TTL
                )
                return None

        # pending_captcha branch is handled by the antispam middleware (Req 11).
        return await handler(event, data)

    async def _maybe_notify(
        self,
        event: TelegramObject,
        telegram_id: int,
        kind: str,
        text: str,
        ttl: int,
    ) -> None:
        key = f"notify:{kind}:{telegram_id}"
        fresh = await set_nx_with_ttl(self._redis, key, "1", ttl)
        if not fresh:
            return
        if isinstance(event, Message):
            try:
                await event.answer(text)
            except Exception as exc:
                log.debug("status_gate.notify_failed", error=repr(exc))
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=True)
            except Exception as exc:
                log.debug("status_gate.notify_failed", error=repr(exc))
