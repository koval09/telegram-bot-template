"""Модуль_Статистики — admin-only ``/stats`` command handler.

Implements Req 9.1: returns a text block with total users, active over
24 h / 7 d / 30 d, banned count and number of connected TON wallets.

The handler is gated by :class:`~app.admin.filters.IsAdminFilter` so
non-admins see the generic "command not found" reply (Req 4.3). Response
time ≤ 3 s is met by the existing indexes on ``last_seen_at``, ``status``
and the partial index on ``ton_address`` (see migration ``0001_initial``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.admin.filters import IsAdminFilter
from app.core.utils.clock import utc_now

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.features.stats.services import StatsServices

log = structlog.get_logger(__name__)

router = Router(name="stats")


# ---------------------------------------------------------------------------
# Russian fallbacks — mirrored in ``app/locales/{ru,en}.yml`` under
# ``stats.*``. Same translator/fallback convention as the referrals,
# subscriptions and broadcasts handlers.
# ---------------------------------------------------------------------------

_MSG_FEATURE_OFF = "Статистика недоступна."
_MSG_ERROR = "Не удалось получить статистику. Попробуйте позже."
_MSG_OVERVIEW = (
    "<b>Статистика</b>\n"
    "Всего пользователей: {total}\n"
    "Активны за 24ч: {active_24h}\n"
    "Активны за 7д: {active_7d}\n"
    "Активны за 30д: {active_30d}\n"
    "Заблокированы: {banned}\n"
    "Привязанные кошельки: {wallets}\n"
    "На момент: {at}"
)

_DATE_FORMAT = "%Y-%m-%d %H:%M UTC"


# ---------------------------------------------------------------------------
# /stats (Req 9.1)
# ---------------------------------------------------------------------------


@router.message(Command("stats"), IsAdminFilter())
async def handle_stats(
    message: Message,
    stats: StatsServices | None = None,
    **data: Any,
) -> None:
    """Render the overview snapshot for the calling admin.

    ``stats`` is populated by aiogram DI from ``dispatcher["stats"]``
    which ``app.bot.register_routers`` sets when the feature flag is on.
    The defensive ``None`` branch keeps the handler safe if the router
    gets registered without the bundle (e.g. misconfigured wiring).
    """
    translator = data.get("_")

    if stats is None:
        text = _translate(
            translator, "stats.feature_off", _MSG_FEATURE_OFF
        )
        await message.answer(text)
        log.warning(
            "stats.handler.feature_off",
            actor_id=message.from_user.id if message.from_user else None,
        )
        return

    now = utc_now()
    try:
        overview = await stats.service.get_overview(now)
    except Exception as exc:
        log.error(
            "stats.handler.overview_failed",
            error=repr(exc),
            actor_id=message.from_user.id if message.from_user else None,
        )
        await message.answer(
            _translate(translator, "stats.error", _MSG_ERROR)
        )
        return

    text = _translate(
        translator,
        "stats.overview",
        _MSG_OVERVIEW,
        total=overview.total,
        active_24h=overview.active_24h,
        active_7d=overview.active_7d,
        active_30d=overview.active_30d,
        banned=overview.banned,
        wallets=overview.wallets,
        at=overview.at.strftime(_DATE_FORMAT),
    )
    await message.answer(text)
    log.info(
        "stats.handler.sent",
        actor_id=message.from_user.id if message.from_user else None,
        total=overview.total,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _translate(
    translator: Any | None,
    key: str,
    fallback: str,
    **kwargs: Any,
) -> str:
    """Translate ``key`` via ``translator`` or return the Russian fallback.

    Mirrors the helper used by the other feature handlers: when the
    translator returns the key unchanged (i.e. the string is missing) we
    substitute ``fallback`` with the same ``{placeholders}``.
    """
    if translator is not None:
        try:
            value = translator(key, **kwargs)
        except Exception as exc:
            log.debug(
                "stats.handler.translate_failed",
                key=key,
                error=repr(exc),
            )
            value = key
        if value != key:
            return value
    if kwargs:
        try:
            return fallback.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return fallback
    return fallback


__all__ = ["router"]
