"""Реферальная_Система — ``/ref`` and ``/referrals`` feature bundle.

Public surface:

* :class:`ReferralsServices` — DI bundle holding the cached ``bot_username``
  (fetched once at startup via ``bot.me()``, Req 7.1) and the
  :class:`ReferralCreditingService` that gates inviter-counter
  increments behind captcha + subscriptions (antifraud refinement on
  Req 7.2).
* :data:`referrals_router` — aiogram ``Router`` with the ``/ref`` and
  ``/referrals`` handlers (task 21.1, Req 7.1 / 7.4).

Container wiring (design § Реферальная_Система):

    if settings.feature_referrals:
        me = await bot.me()
        crediting = ReferralCreditingService(
            users_repo, audit,
            subs_checker=services.subscriptions.checker if services.subscriptions else None,
            require_captcha=settings.feature_antispam,
        )
        services.referrals = ReferralsServices(
            bot_username=me.username, crediting=crediting,
        )

``app/bot.py`` includes :data:`referrals_router` and exposes the bundle
as ``dispatcher["referrals"]`` so the handlers and the captcha /
subscription trigger sites can resolve the service via DI.

Req 7.3 (reject self-referrals) is enforced in the registration
middleware (task 6.2) which records the referrer; the bundle here
controls *when* the inviter actually gets the credit.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.features.referrals.crediting import (
    CreditOutcome,
    ReferralCreditingService,
    maybe_credit,
)
from app.features.referrals.handlers import router as referrals_router


@dataclass(slots=True)
class ReferralsServices:
    """Bundle of services exposed when ``feature_referrals=True``.

    Attributes:
        bot_username: The bot's username (without the leading ``@``), fetched
            once via ``bot.me()`` at startup and cached here. Used to build
            the personal deeplink ``https://t.me/<bot_username>?start=ref_<id>``
            (Req 7.1).
        crediting: The eligibility-aware crediting service. Handlers and
            middlewares call ``crediting.try_credit(invitee_id)`` whenever
            the invitee may have just become "real" (post-captcha,
            post-subscription).
    """

    bot_username: str
    crediting: ReferralCreditingService


__all__ = [
    "CreditOutcome",
    "ReferralCreditingService",
    "ReferralsServices",
    "maybe_credit",
    "referrals_router",
]
