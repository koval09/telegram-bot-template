"""Admin command handlers.

Commands (registered on the ``admin_router``):

- ``/ban <id> <reason>``
- ``/unban <id>``
- ``/mute <id> <duration> [reason]`` — duration: ``10m`` / ``2h`` / ``1d``
- ``/unmute <id>``
- ``/kick <id>`` — inside a group chat the bot is admin of
- ``/grant_admin <id>`` — superadmin only
- ``/revoke_admin <id>`` — superadmin only
- ``/audit [page]`` — superadmin only (Req 6.3 / 6.5)
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.admin.filters import IsAdminFilter, IsSuperadminFilter
from app.admin.user_manager import ModerationResult, UserManager, parse_duration
from app.core.services.audit import AuditLog

router = Router(name="admin")


# --------------------------------------------------------------------------
# Arg parsing helpers
# --------------------------------------------------------------------------

def _split_args(command: CommandObject, want: int) -> list[str] | None:
    if not command.args:
        return None
    parts = command.args.strip().split(maxsplit=want - 1)
    if len(parts) < want:
        return None
    return parts


def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


async def _reply(message: Message, result: ModerationResult) -> None:
    await message.answer(result.message or ("OK" if result.ok else "Ошибка."))


# --------------------------------------------------------------------------
# /ban, /unban
# --------------------------------------------------------------------------

@router.message(Command("ban"), IsAdminFilter())
async def handle_ban(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    args = _split_args(command, 2)
    if args is None:
        await message.answer("Использование: /ban &lt;telegram_id&gt; &lt;причина&gt;")
        return
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    assert message.from_user is not None
    await _reply(message, await user_manager.ban(message.from_user.id, target, args[1]))


@router.message(Command("unban"), IsAdminFilter())
async def handle_unban(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    args = _split_args(command, 1)
    if args is None:
        await message.answer("Использование: /unban &lt;telegram_id&gt;")
        return
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    assert message.from_user is not None
    await _reply(message, await user_manager.unban(message.from_user.id, target))


# --------------------------------------------------------------------------
# /mute, /unmute
# --------------------------------------------------------------------------

@router.message(Command("mute"), IsAdminFilter())
async def handle_mute(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    # /mute <id> <duration> [reason...]
    args = _split_args(command, 3)
    if args is None:
        # Allow 2-arg form (without reason).
        two = _split_args(command, 2)
        if two is None:
            await message.answer(
                "Использование: /mute &lt;telegram_id&gt; &lt;длительность&gt; [причина]\n"
                "Длительность: 10m, 2h, 1d, 3w, 45s"
            )
            return
        args = [two[0], two[1], ""]
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    duration = parse_duration(args[1])
    if duration is None:
        await message.answer("Некорректная длительность. Примеры: 10m, 2h, 1d.")
        return
    assert message.from_user is not None
    await _reply(message, await user_manager.mute(message.from_user.id, target, duration, args[2]))


@router.message(Command("unmute"), IsAdminFilter())
async def handle_unmute(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    args = _split_args(command, 1)
    if args is None:
        await message.answer("Использование: /unmute &lt;telegram_id&gt;")
        return
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    assert message.from_user is not None
    await _reply(message, await user_manager.unmute(message.from_user.id, target))


# --------------------------------------------------------------------------
# /kick — works only inside a group/supergroup
# --------------------------------------------------------------------------

@router.message(Command("kick"), IsAdminFilter())
async def handle_kick(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Команда /kick доступна только в групповом чате.")
        return
    args = _split_args(command, 1)
    if args is None:
        await message.answer("Использование: /kick &lt;telegram_id&gt;")
        return
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    assert message.from_user is not None
    await _reply(
        message,
        await user_manager.kick(message.from_user.id, target, message.chat.id),
    )


# --------------------------------------------------------------------------
# /grant_admin, /revoke_admin — superadmin only
# --------------------------------------------------------------------------

@router.message(Command("grant_admin"), IsSuperadminFilter())
async def handle_grant_admin(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    args = _split_args(command, 1)
    if args is None:
        await message.answer("Использование: /grant_admin &lt;telegram_id&gt;")
        return
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    assert message.from_user is not None
    await _reply(message, await user_manager.grant_admin(message.from_user.id, target))


@router.message(Command("revoke_admin"), IsSuperadminFilter())
async def handle_revoke_admin(
    message: Message, command: CommandObject, user_manager: UserManager
) -> None:
    args = _split_args(command, 1)
    if args is None:
        await message.answer("Использование: /revoke_admin &lt;telegram_id&gt;")
        return
    target = _parse_int(args[0])
    if target is None:
        await message.answer("Некорректный telegram_id.")
        return
    assert message.from_user is not None
    await _reply(message, await user_manager.revoke_admin(message.from_user.id, target))


# --------------------------------------------------------------------------
# /audit — paginated journal viewer (superadmin only)
# --------------------------------------------------------------------------

AUDIT_CB_PREFIX = "audit:"


def _format_page(items, page: int, total_pages: int, total: int) -> str:
    if not items:
        return "Журнал пуст."
    lines = [f"<b>Журнал</b> — страница {page}/{total_pages} (всего: {total})"]
    for rec in items:
        when = rec.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lvl = rec.level.value.upper()
        actor = rec.actor_id if rec.actor_id is not None else "-"
        target = rec.target_id if rec.target_id is not None else "-"
        action = rec.action or rec.source or "-"
        detail = rec.reason or rec.message or ""
        lines.append(
            f"<code>{when}</code> [{lvl}] {action} actor={actor} target={target}"
            + (f" — {detail}" if detail else "")
        )
    return "\n".join(lines)


def _audit_kb(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="← Пред", callback_data=f"{AUDIT_CB_PREFIX}{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="audit:noop"))
    if page < total_pages:
        row.append(InlineKeyboardButton(text="След →", callback_data=f"{AUDIT_CB_PREFIX}{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


@router.message(Command("audit"), IsSuperadminFilter())
async def handle_audit(
    message: Message, command: CommandObject, audit: AuditLog
) -> None:
    page = 1
    if command.args:
        parsed = _parse_int(command.args.strip())
        if parsed is not None and parsed >= 1:
            page = parsed
    result = await audit.list_page(page=page, page_size=50)
    await message.answer(
        _format_page(result.items, result.page, result.total_pages, result.total),
        reply_markup=_audit_kb(result.page, result.total_pages),
    )


@router.callback_query(lambda cq: cq.data and cq.data.startswith(AUDIT_CB_PREFIX))
async def handle_audit_nav(
    query: CallbackQuery, audit: AuditLog, authorization
) -> None:
    # The callback filter is separate from the message filter, so we
    # double-check access here.
    if query.from_user is None or not await authorization.is_superadmin(query.from_user.id):
        await query.answer()
        return
    assert query.data is not None
    if query.data.endswith(":noop"):
        await query.answer()
        return
    try:
        page = int(query.data.removeprefix(AUDIT_CB_PREFIX))
    except ValueError:
        await query.answer()
        return
    result = await audit.list_page(page=page, page_size=50)
    if query.message:
        await query.message.edit_text(
            _format_page(result.items, result.page, result.total_pages, result.total),
            reply_markup=_audit_kb(result.page, result.total_pages),
        )
    await query.answer()
