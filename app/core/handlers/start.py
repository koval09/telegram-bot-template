"""/start command handler."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.core.db.models import User
from app.core.services.registration import RegistrationResult

router = Router(name="core.start")


@router.message(CommandStart())
async def handle_start(
    message: Message, user: User, registration: RegistrationResult | None = None
) -> None:
    name = user.first_name or user.username or f"ID {user.telegram_id}"
    if registration and registration.created:
        text = (
            f"Привет, {name}!\n"
            "Вы зарегистрированы. Доступные команды:\n"
            "/profile — ваш профиль\n"
            "/cancel — отменить текущий диалог"
        )
    else:
        text = (
            f"С возвращением, {name}!\n"
            "/profile — ваш профиль\n"
            "/cancel — отменить текущий диалог"
        )
    await message.answer(text)
