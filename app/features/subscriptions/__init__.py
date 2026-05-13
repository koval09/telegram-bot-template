"""Модуль_Подписок — required-channel membership gate.

Public surface:

* :class:`SubscriptionChecker` — cache-first per-channel probe (task 19.1,
  Req 12.1 / 12.4).
* :class:`MissingChannel` — per-channel result record consumed by the
  middleware (task 19.2).
* :class:`SubscriptionsMiddleware` — aiogram middleware that blocks commands
  for users not subscribed to every required channel (task 19.2, Req 12.1
  / 12.2).
* :data:`subscriptions_router` — callback router for the "Проверить
  подписку" button (task 19.2, Req 12.3).
* :class:`SubscriptionsServices` — DI bundle consumed by ``app.container``
  and ``app.bot`` so both the middleware and the router can be wired
  behind a single feature flag.

Container wiring (design § Модуль_Подписок): build a single
``SubscriptionChecker(bot, redis, audit, settings.required_channels)`` when
``settings.feature_subscriptions`` is on, wrap it in
:class:`SubscriptionsServices`, and register both the middleware and the
router from ``app/bot.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.features.subscriptions.checker import (
    GET_CHAT_MEMBER_TIMEOUT_SECONDS,
    SUBSCRIBED_CACHE_TTL_SECONDS,
    SUBSCRIBED_STATUSES,
    MissingChannel,
    SubscriptionChecker,
)
from app.features.subscriptions.handlers import router as subscriptions_router
from app.features.subscriptions.middleware import (
    RECHECK_CALLBACK_DATA,
    WHITELISTED_COMMANDS,
    SubscriptionsMiddleware,
    build_gate_keyboard,
)


@dataclass(slots=True)
class SubscriptionsServices:
    """Bundle of services exposed when ``feature_subscriptions=True``.

    Held on ``AppServices.subscriptions``; the bot wiring reads it to
    register :class:`SubscriptionsMiddleware` and
    :data:`subscriptions_router`.
    """

    checker: SubscriptionChecker


__all__ = [
    # Checker (task 19.1)
    "GET_CHAT_MEMBER_TIMEOUT_SECONDS",
    "MissingChannel",
    "SUBSCRIBED_CACHE_TTL_SECONDS",
    "SUBSCRIBED_STATUSES",
    "SubscriptionChecker",
    # Middleware & handler router (task 19.2)
    "RECHECK_CALLBACK_DATA",
    "SubscriptionsMiddleware",
    "SubscriptionsServices",
    "WHITELISTED_COMMANDS",
    "build_gate_keyboard",
    "subscriptions_router",
]
