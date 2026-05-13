"""Registration middleware.

Runs BEFORE all other middleware. Upserts the user and puts the resulting
``User`` ORM object into ``data["user"]`` so every downstream component
(status gate, antispam, handlers) can rely on it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.services.registration import RegistrationService


class RegistrationMiddleware(BaseMiddleware):
    def __init__(self, registration: RegistrationService) -> None:
        self._registration = registration

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is None:
            return await handler(event, data)

        start_arg = _extract_start_arg(event)
        result = await self._registration.ensure_user(tg_user, start_arg=start_arg)
        data["user"] = result.user
        data["registration"] = result

        if result.db_error:
            # Tell the user and stop: downstream handlers cannot proceed
            # without a persisted user row (Req 1.5).
            await _reply_db_error(event)
            return None

        return await handler(event, data)


def _extract_start_arg(event: TelegramObject) -> str | None:
    if isinstance(event, Message) and event.text and event.text.startswith("/start"):
        parts = event.text.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip() or None
    return None


async def _reply_db_error(event: TelegramObject) -> None:
    text = "Произошла ошибка при регистрации. Попробуйте позже."
    if isinstance(event, Message):
        try:
            await event.answer(text)
        except Exception:
            pass
    elif isinstance(event, CallbackQuery):
        try:
            await event.answer(text, show_alert=True)
        except Exception:
            pass
