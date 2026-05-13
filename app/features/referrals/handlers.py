"""Реферальная_Система — ``/ref`` and ``/referrals`` command handlers.

Requirements implemented here:

- **Req 7.1** — ``/ref`` returns the personal referral link
  ``https://t.me/<bot_username>?start=ref_<telegram_id>``.
  ``bot_username`` is fetched exactly once at startup via ``bot.me()`` and
  cached inside :class:`~app.features.referrals.ReferralsServices`
  (see ``app/container.py``).
- **Req 7.4** — ``/referrals`` returns the number of invited users
  (``referrals_count``) and the date of the most recent invite
  (``last_referral_at``) in a human-readable form.
- **Req 7.2 / 7.3** are satisfied elsewhere:
    * 7.2 — recording the inviter on ``/start ref_<id>`` happens in
      :class:`~app.core.services.registration.RegistrationService`
      (task 6.2). The actual ``referrals_count`` increment lives in
      :class:`~app.features.referrals.crediting.ReferralCreditingService`
      and runs only after the invitee passes captcha + the required-
      channel subscription gate (antifraud refinement). When neither
      gate is enabled the credit settles on ``/start`` itself; when
      either gate is on it is triggered from the matching event
      (captcha pass / subscription recheck).
    * 7.3 — the registration middleware rejects self-referrals and
      unknown referrers (Req 1.8), so this module only *reads* the
      stats.

I18n contract
-------------
When the i18n feature is enabled the middleware puts a translator callable
at ``data["_"]``. Handlers here prefer it when available and fall back to
Russian literals otherwise. The translator returns the key unchanged when a
translation is missing, so we compare ``value != key`` to decide whether to
use the translation or the hard-coded Russian fallback (the pattern used by
``app/features/subscriptions``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.db.models import User

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.features.referrals import ReferralsServices


log = structlog.get_logger(__name__)


router = Router(name="referrals")


# ---------------------------------------------------------------------------
# Russian fallbacks — used when i18n is disabled or the key is missing.
# Keep wording aligned with ``app/locales/ru.yml`` under the ``referrals.*``
# namespace so enabling i18n is a silent no-op behaviourally.
# ---------------------------------------------------------------------------

_MSG_FEATURE_OFF = "Реферальная система недоступна."
_MSG_LINK = "Ваша реферальная ссылка:\n{link}"
_MSG_STATS_HEADER = "Ваши рефералы"
_MSG_STATS_COUNT = "Приглашено: {count}"
_MSG_STATS_LAST_WITH_DATE = "Последнее приглашение: {date}"
_MSG_STATS_LAST_NONE = "Последнее приглашение: ещё нет"

_DATE_FORMAT = "%Y-%m-%d %H:%M UTC"


# ---------------------------------------------------------------------------
# /ref (Req 7.1)
# ---------------------------------------------------------------------------


@router.message(Command("ref"))
async def handle_ref(
    message: Message,
    user: User,
    referrals: ReferralsServices | None = None,
    **data: Any,
) -> None:
    """Return the user's personal referral deeplink.

    ``referrals`` is populated by aiogram DI from ``dispatcher["referrals"]``
    which ``app.bot.register_routers`` sets when the feature flag is on.
    The defensive ``None`` branch keeps the handler safe if the router gets
    registered without the bundle (e.g. a misconfigured wiring).
    """
    translator = data.get("_")

    if referrals is None or not referrals.bot_username:
        text = _translate(
            translator, "referrals.feature_off", _MSG_FEATURE_OFF
        )
        await message.answer(text)
        log.warning(
            "referrals.ref.feature_off",
            telegram_id=user.telegram_id,
            has_bundle=referrals is not None,
        )
        return

    link = (
        f"https://t.me/{referrals.bot_username}"
        f"?start=ref_{user.telegram_id}"
    )
    text = _translate(
        translator,
        "referrals.link",
        _MSG_LINK,
        link=link,
    )
    await message.answer(text, disable_web_page_preview=True)
    log.info(
        "referrals.ref.sent",
        telegram_id=user.telegram_id,
        bot_username=referrals.bot_username,
    )


# ---------------------------------------------------------------------------
# /referrals (Req 7.4)
# ---------------------------------------------------------------------------


@router.message(Command("referrals"))
async def handle_referrals(
    message: Message,
    user: User,
    **data: Any,
) -> None:
    """Show ``referrals_count`` and ``last_referral_at``.

    Reads straight from ``data["user"]`` — the registration middleware
    (task 6.2) has already refreshed the row for this update, so we do
    not need another DB round-trip.
    """
    translator = data.get("_")

    header = _translate(
        translator, "referrals.stats.header", _MSG_STATS_HEADER
    )
    count_line = _translate(
        translator,
        "referrals.stats.count",
        _MSG_STATS_COUNT,
        count=user.referrals_count,
    )
    last_line = _render_last_referral_line(user.last_referral_at, translator)

    await message.answer(f"{header}\n{count_line}\n{last_line}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_last_referral_line(
    last_referral_at: datetime | None, translator: Any | None
) -> str:
    """Render the "last invite" line, picking the right i18n key."""
    if last_referral_at is None:
        return _translate(
            translator,
            "referrals.stats.last_none",
            _MSG_STATS_LAST_NONE,
        )
    date_str = last_referral_at.strftime(_DATE_FORMAT)
    return _translate(
        translator,
        "referrals.stats.last",
        _MSG_STATS_LAST_WITH_DATE,
        date=date_str,
    )


def _translate(
    translator: Any | None,
    key: str,
    fallback: str,
    **kwargs: Any,
) -> str:
    """Translate ``key`` via ``translator`` or return ``fallback``.

    Matches the helper in ``app/features/subscriptions/middleware.py`` so the
    translator behaviour is consistent across features: if the translator
    returns ``key`` unchanged (i.e. the string is missing) we substitute the
    Russian fallback with the same ``{placeholders}``.
    """
    if translator is not None:
        try:
            value = translator(key, **kwargs)
        except Exception as exc:
            log.debug(
                "referrals.translate_failed",
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
