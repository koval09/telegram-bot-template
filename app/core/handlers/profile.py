"""/profile command — shows the user their own data only (Req 2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.core.db.models import User, UserStatus
from app.core.repositories.users import UsersRepo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.ton.connector import TonConnector

router = Router(name="core.profile")

CB_LANG_PREFIX = "profile:lang:"
CB_WALLET_DISCONNECT = "profile:wallet:disconnect"


def _display_name(user: User) -> str:
    if user.first_name or user.last_name:
        return " ".join(filter(None, [user.first_name, user.last_name]))
    if user.username:
        return f"@{user.username}"
    return f"ID {user.telegram_id}"


def _status_label(status: UserStatus) -> str:
    return {
        UserStatus.active: "активен",
        UserStatus.banned: "заблокирован",
        UserStatus.muted: "ограничен",
        UserStatus.pending_captcha: "ожидание капчи",
    }.get(status, status.value)


def _render(user: User, settings: Settings) -> str:
    lines = [
        "<b>Профиль</b>",
        f"ID: <code>{user.telegram_id}</code>",
        f"Имя: {_display_name(user)}",
        f"Язык: {user.language_code or '—'}",
        f"Регистрация: {user.created_at.strftime('%Y-%m-%d %H:%M UTC') if user.created_at else '—'}",
        f"Статус: {_status_label(user.status)}",
    ]
    if settings.feature_ton_connector:
        lines.append(f"TON-кошелёк: {user.ton_address or '—'}")
    if settings.feature_referrals:
        lines.append(f"Приглашено: {user.referrals_count}")
    return "\n".join(lines)


def _build_keyboard(user: User, settings: Settings) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if settings.feature_i18n:
        row = [
            InlineKeyboardButton(text=code.upper(), callback_data=f"{CB_LANG_PREFIX}{code}")
            for code in settings.supported_locales
        ]
        rows.append(row)
    if settings.feature_ton_connector and user.ton_address:
        rows.append([
            InlineKeyboardButton(
                text="Отвязать кошелёк", callback_data=CB_WALLET_DISCONNECT
            )
        ])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("profile"))
async def handle_profile(
    message: Message, user: User, settings: Settings
) -> None:
    if user.status == UserStatus.banned:
        # Req 2.4 — return a blocked message, no profile data.
        await message.answer("Ваш доступ к боту ограничен.")
        return
    await message.answer(_render(user, settings), reply_markup=_build_keyboard(user, settings))


@router.callback_query(F.data.startswith(CB_LANG_PREFIX))
async def handle_change_language(
    query: CallbackQuery,
    user: User,
    settings: Settings,
    users_repo: UsersRepo,
) -> None:
    assert query.data is not None
    code = query.data[len(CB_LANG_PREFIX):]
    if code not in settings.supported_locales:
        await query.answer("Язык не поддерживается", show_alert=True)
        return
    await users_repo.set_language(user.telegram_id, code)
    user.language_code = code
    await query.answer(f"Язык: {code.upper()}")
    if query.message:
        await query.message.edit_text(
            _render(user, settings), reply_markup=_build_keyboard(user, settings)
        )


@router.callback_query(F.data == CB_WALLET_DISCONNECT)
async def handle_wallet_disconnect(
    query: CallbackQuery,
    user: User,
    settings: Settings,
    users_repo: UsersRepo,
    ton: TonConnector | None = None,
) -> None:
    if not settings.feature_ton_connector or not user.ton_address:
        await query.answer("Кошелёк не привязан", show_alert=True)
        return
    # Req 2.3 / 3.4 — delegate the teardown to :class:`TonConnector` when
    # available (Stage 3+). It closes the TON Connect session, wipes Redis
    # state and clears the DB fields. When the feature is off (no ``ton`` in
    # dispatcher data) we fall back to the DB-only path so the handler stays
    # idempotent even though the button is not rendered in that case.
    if ton is not None:
        await ton.disconnect(user.telegram_id)
    else:
        await users_repo.clear_wallet(user.telegram_id)
    user.ton_address = None
    user.ton_wallet_name = None
    user.ton_connected_at = None
    await query.answer("Кошелёк отвязан")
    if query.message:
        await query.message.edit_text(
            _render(user, settings), reply_markup=_build_keyboard(user, settings)
        )
