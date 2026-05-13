"""Менеджер_Пользователей — ban / mute / kick / role management.

Requirements 4.5, 4.6, 5.1–5.9. Each public method:
- Looks up the target by Telegram ID; returns a typed error if absent (5.9).
- Refuses to moderate another admin / superadmin (5.8).
- Persists the state change in a single DB transaction.
- Writes an audit record (6.1).
- Invalidates the Authorization cache for ``ban``/``grant``/``revoke`` (4.7).

Time-throttled user-facing notifications (Reqs 5.2 / 5.4) are enforced in
the ``StatusGate`` middleware, not here.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog

from app.admin.authorization import Authorization
from app.core.db.models import UserRole, UserStatus
from app.core.repositories.users import UsersRepo
from app.core.services.audit import AuditLog, ModerationAction
from app.core.utils.clock import Clock, utc_now

log = structlog.get_logger(__name__)

REASON_MIN_LEN = 1
REASON_MAX_LEN = 500
MUTE_MIN = timedelta(minutes=1)
MUTE_MAX = timedelta(days=30)
KICK_TIMEOUT_SEC = 10.0

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 86400 * 7,
}


# --------------------------------------------------------------------------
# Return type — a small sum type so handlers can render localized replies
# without importing Telegram error classes.
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ModerationResult:
    ok: bool
    kind: str  # 'ok' | 'target_not_found' | 'forbidden' | 'invalid' | 'api_error'
    message: str = ""
    extra: dict[str, Any] | None = None


def _ok(msg: str = "") -> ModerationResult:
    return ModerationResult(ok=True, kind="ok", message=msg)


def _err(kind: str, msg: str) -> ModerationResult:
    return ModerationResult(ok=False, kind=kind, message=msg)


def parse_duration(value: str) -> timedelta | None:
    """Parse ``10m`` / ``2h`` / ``1d`` / ``3w`` / ``45s`` into timedelta.

    Returns ``None`` for malformed input; the handler then replies with an
    "invalid duration" message.
    """
    match = _DURATION_RE.match(value)
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2).lower()
    seconds = count * _DURATION_UNITS[unit]
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# UserManager
# --------------------------------------------------------------------------

class UserManager:
    def __init__(
        self,
        users: UsersRepo,
        audit: AuditLog,
        authorization: Authorization,
        bot: Any,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._users = users
        self._audit = audit
        self._auth = authorization
        self._bot = bot
        self._clock = clock

    # ------------------------------------------------------------------
    # Ban / unban
    # ------------------------------------------------------------------
    async def ban(self, actor_id: int, target_id: int, reason: str) -> ModerationResult:
        if not REASON_MIN_LEN <= len(reason) <= REASON_MAX_LEN:
            return _err("invalid", f"Причина должна быть 1..{REASON_MAX_LEN} символов.")

        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")
        if target.role in (UserRole.admin, UserRole.superadmin):
            return _err("forbidden", "Недостаточно прав.")

        now = self._clock()
        await self._users.set_status(
            target_id,
            UserStatus.banned,
            banned_by=actor_id,
            ban_reason=reason,
            now=now,
        )
        await self._auth.invalidate(target_id)
        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.ban,
            reason=reason,
            now=now,
        )
        return _ok(f"Пользователь {target_id} заблокирован.")

    async def unban(self, actor_id: int, target_id: int) -> ModerationResult:
        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")

        now = self._clock()
        await self._users.set_status(target_id, UserStatus.active, now=now)
        await self._auth.invalidate(target_id)
        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.unban,
            reason="",
            now=now,
        )
        return _ok(f"Пользователь {target_id} разблокирован.")

    # ------------------------------------------------------------------
    # Mute / unmute
    # ------------------------------------------------------------------
    async def mute(
        self, actor_id: int, target_id: int, duration: timedelta, reason: str = ""
    ) -> ModerationResult:
        if not MUTE_MIN <= duration <= MUTE_MAX:
            return _err(
                "invalid",
                "Длительность мута должна быть от 1 минуты до 30 дней.",
            )
        if len(reason) > REASON_MAX_LEN:
            return _err("invalid", f"Причина не должна превышать {REASON_MAX_LEN} символов.")

        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")
        if target.role in (UserRole.admin, UserRole.superadmin):
            return _err("forbidden", "Недостаточно прав.")

        now = self._clock()
        muted_until = now + duration
        await self._users.set_status(
            target_id,
            UserStatus.muted,
            muted_until=muted_until,
            muted_by=actor_id,
            now=now,
        )
        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.mute,
            reason=reason or f"duration={duration}",
            now=now,
        )
        return _ok(f"Пользователь {target_id} замьючен до {muted_until:%Y-%m-%d %H:%M UTC}.")

    async def unmute(self, actor_id: int, target_id: int) -> ModerationResult:
        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")

        now = self._clock()
        await self._users.set_status(target_id, UserStatus.active, now=now)
        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.unmute,
            reason="",
            now=now,
        )
        return _ok(f"Мут пользователя {target_id} снят.")

    # ------------------------------------------------------------------
    # Kick (group chats only)
    # ------------------------------------------------------------------
    async def kick(
        self, actor_id: int, target_id: int, chat_id: int
    ) -> ModerationResult:
        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")
        if target.role in (UserRole.admin, UserRole.superadmin):
            return _err("forbidden", "Недостаточно прав.")

        # Late import to keep the module importable without aiogram installed
        # (useful for unit tests of the manager over fakes).
        try:
            from aiogram.exceptions import (
                TelegramBadRequest,
                TelegramForbiddenError,
                TelegramNetworkError,
            )
        except Exception:  # pragma: no cover
            TelegramBadRequest = TelegramForbiddenError = TelegramNetworkError = Exception  # type: ignore[assignment]

        try:
            # ban → immediate unban performs a "kick without ban" (user can rejoin).
            await asyncio.wait_for(
                self._bot.ban_chat_member(chat_id, target_id),
                timeout=KICK_TIMEOUT_SEC,
            )
            await asyncio.wait_for(
                self._bot.unban_chat_member(chat_id, target_id, only_if_banned=False),
                timeout=KICK_TIMEOUT_SEC,
            )
        except TelegramForbiddenError as exc:
            await self._audit.record_error(
                source="Telegram API",
                message=f"kick denied: {exc!r}",
                actor_id=actor_id,
                target_id=target_id,
            )
            return _err("api_error", "Боту нужны права администратора в чате.")
        except TelegramBadRequest as exc:
            await self._audit.record_error(
                source="Telegram API",
                message=f"kick bad_request: {exc!r}",
                actor_id=actor_id,
                target_id=target_id,
            )
            return _err("api_error", "Не удалось исключить пользователя из чата.")
        except (TimeoutError, TelegramNetworkError) as exc:  # type: ignore[misc]
            await self._audit.record_error(
                source="Telegram API",
                message=f"kick timeout/network: {exc!r}",
                actor_id=actor_id,
                target_id=target_id,
            )
            return _err("api_error", "Telegram API не ответил вовремя.")

        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.kick,
            reason=f"chat_id={chat_id}",
        )
        return _ok(f"Пользователь {target_id} исключён из чата.")

    # ------------------------------------------------------------------
    # Role management
    # ------------------------------------------------------------------
    async def grant_admin(self, actor_id: int, target_id: int) -> ModerationResult:
        # Only superadmins may grant (Req 4.5 / 4.6) — filter enforces this,
        # but we double-check here for programmatic callers.
        if not await self._auth.is_superadmin(actor_id):
            await self._audit.record_warning(
                event="grant_admin_denied",
                actor_id=actor_id,
                details={"target": target_id},
            )
            return _err("forbidden", "Недостаточно прав.")

        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")

        changed = await self._auth.grant_admin(target_id)
        if not changed:
            return _err("invalid", "Пользователь уже админ.")

        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.grant_admin,
            reason="",
        )
        return _ok(f"Пользователь {target_id} получил роль admin.")

    async def revoke_admin(self, actor_id: int, target_id: int) -> ModerationResult:
        if not await self._auth.is_superadmin(actor_id):
            await self._audit.record_warning(
                event="revoke_admin_denied",
                actor_id=actor_id,
                details={"target": target_id},
            )
            return _err("forbidden", "Недостаточно прав.")

        target = await self._users.get_by_tg_id(target_id)
        if target is None:
            return _err("target_not_found", "Пользователь не найден.")
        if target.role is UserRole.superadmin:
            return _err("forbidden", "Роль superadmin снимается только через переменные окружения.")

        changed = await self._auth.revoke_admin(target_id)
        if not changed:
            return _err("invalid", "У пользователя нет роли admin.")

        await self._audit.record_moderation(
            actor_id=actor_id,
            target_id=target_id,
            action=ModerationAction.revoke_admin,
            reason="",
        )
        return _ok(f"Роль admin у пользователя {target_id} снята.")
