"""Per-role bot command lists shown in Telegram's blue menu.

Telegram lets us register different command sets per scope:
- :class:`BotCommandScopeDefault` — visible to everyone (the public set).
- :class:`BotCommandScopeChat(chat_id=user_id)` — overrides the default for
  the given user (used for admins / superadmins).

This module owns three responsibilities:

1. :func:`apply_startup_commands` — called once at startup. Sets the public
   default and pushes the admin/superadmin sets for every superadmin id
   from :class:`Settings.superadmin_ids` and every user whose ``role`` is
   ``admin``/``superadmin`` in the DB.
2. :func:`apply_role_commands` — called on ``/grant_admin`` and
   ``/revoke_admin`` so the menu in the affected user's chat updates
   immediately, no restart required.
3. :func:`reset_user_commands` — clears a chat-scoped command list (drops a
   user back to the default set).

All Telegram API failures are logged and swallowed — bad menu state is
strictly cosmetic and must never crash the bot or break shutdown.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings
    from app.container import AppServices
    from app.core.db.models import UserRole


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Command sets
# ---------------------------------------------------------------------------


def _public_commands(settings: Settings) -> list[BotCommand]:
    """Return the command set shown to every user in their menu.

    The list is built dynamically so disabled features do not appear.
    """
    cmds: list[BotCommand] = [
        BotCommand(command="start", description="Старт"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="cancel", description="Отменить диалог"),
    ]
    if settings.feature_referrals:
        cmds.append(BotCommand(command="ref", description="Моя реферальная ссылка"))
        cmds.append(
            BotCommand(command="referrals", description="Сколько я пригласил")
        )
    if settings.feature_ton_connector:
        cmds.append(BotCommand(command="connect_wallet", description="Привязать TON-кошелёк"))
        cmds.append(
            BotCommand(command="disconnect_wallet", description="Отвязать кошелёк")
        )
    return cmds


def _admin_commands(settings: Settings) -> list[BotCommand]:
    """Public commands + moderation commands for users with admin role."""
    cmds = list(_public_commands(settings))
    cmds.extend(
        [
            BotCommand(command="ban", description="Забанить (admin)"),
            BotCommand(command="unban", description="Разбанить (admin)"),
            BotCommand(command="mute", description="Замьютить (admin)"),
            BotCommand(command="unmute", description="Снять мут (admin)"),
            BotCommand(command="kick", description="Кикнуть из чата (admin)"),
        ]
    )
    if settings.feature_stats:
        cmds.append(BotCommand(command="stats", description="Статистика бота (admin)"))
    if settings.feature_broadcasts:
        cmds.append(BotCommand(command="broadcast", description="Запустить рассылку (admin)"))
        cmds.append(
            BotCommand(
                command="broadcast_cancel",
                description="Отменить рассылку (admin)",
            )
        )
    return cmds


def _superadmin_commands(settings: Settings) -> list[BotCommand]:
    """Admin commands + audit and role-management commands."""
    cmds = list(_admin_commands(settings))
    cmds.extend(
        [
            BotCommand(command="audit", description="Журнал событий (superadmin)"),
            BotCommand(
                command="grant_admin",
                description="Выдать роль admin (superadmin)",
            ),
            BotCommand(
                command="revoke_admin",
                description="Снять роль admin (superadmin)",
            ),
        ]
    )
    return cmds


def _commands_for_role(role: UserRole | str, settings: Settings) -> list[BotCommand]:
    role_value = getattr(role, "value", role)
    if role_value == "superadmin":
        return _superadmin_commands(settings)
    if role_value == "admin":
        return _admin_commands(settings)
    return _public_commands(settings)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def apply_startup_commands(services: AppServices) -> None:
    """Set the default + per-admin command lists at process startup.

    Idempotent — Telegram replaces the previous list each time, so calling
    this on every restart is safe.
    """
    bot = services.bot
    settings = services.settings

    # 1. Default set — shown to every user out of the box.
    public = _public_commands(settings)
    try:
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
    except TelegramAPIError as exc:
        log.warning("commands_setup.default_failed", error=repr(exc))

    # 2. Per-superadmin set — superadmins are seeded from env on every start
    # (see :class:`Authorization.seed_superadmins`), so we apply the full
    # superadmin list to every id in the env even if the DB is empty.
    super_set = _superadmin_commands(settings)
    for sa_id in settings.superadmin_ids:
        try:
            await bot.set_my_commands(super_set, scope=BotCommandScopeChat(chat_id=sa_id))
        except TelegramAPIError as exc:
            log.warning(
                "commands_setup.superadmin_failed",
                user_id=sa_id,
                error=repr(exc),
            )

    # 3. Per-admin set — anyone whose DB role is ``admin`` (excluding the
    # superadmins above, which already got the wider set).
    try:
        admin_ids = await _list_admin_ids(services)
    except Exception as exc:
        admin_ids = []
        log.warning("commands_setup.list_admins_failed", error=repr(exc))

    admin_set = _admin_commands(settings)
    for aid in admin_ids:
        if aid in settings.superadmin_ids:
            continue
        try:
            await bot.set_my_commands(admin_set, scope=BotCommandScopeChat(chat_id=aid))
        except TelegramAPIError as exc:
            log.warning(
                "commands_setup.admin_failed",
                user_id=aid,
                error=repr(exc),
            )

    log.info(
        "commands_setup.done",
        public_count=len(public),
        admin_count=len(admin_set),
        superadmin_count=len(super_set),
        promoted_admins=len(admin_ids),
    )


async def apply_role_commands(
    bot: Bot,
    settings: Settings,
    user_id: int,
    role: UserRole | str,
) -> None:
    """Set the per-chat command list to match ``role`` for ``user_id``.

    Called from the ``grant_admin`` / ``revoke_admin`` flow so the user's
    blue menu picks up the new role on their next message.
    """
    cmds = _commands_for_role(role, settings)
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
        log.warning(
            "commands_setup.role_failed",
            user_id=user_id,
            role=str(role),
            error=repr(exc),
        )
        return
    log.info("commands_setup.role_applied", user_id=user_id, role=str(role))


async def reset_user_commands(bot: Bot, user_id: int) -> None:
    """Drop chat-scoped commands for ``user_id`` (back to the default set)."""
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
        log.warning("commands_setup.reset_failed", user_id=user_id, error=repr(exc))
        return
    log.info("commands_setup.reset_done", user_id=user_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _list_admin_ids(services: AppServices) -> Sequence[int]:
    """Return the telegram_ids whose DB role is ``admin``.

    Uses a single read on the ``users`` table; intentionally bypasses the
    repo layer because :class:`UsersRepo` does not expose a "list by role"
    method and we do not want to add one just for command-menu wiring.
    """
    from sqlalchemy import select

    from app.core.db.models import User, UserRole

    sm = services.sessionmaker
    async with sm() as session:
        stmt = select(User.telegram_id).where(User.role == UserRole.admin)
        result = await session.execute(stmt)
        return [int(row[0]) for row in result.all()]


__all__ = [
    "apply_role_commands",
    "apply_startup_commands",
    "reset_user_commands",
]
