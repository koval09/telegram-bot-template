"""Модуль_Платежей — payments feature bundle (Stars + TON).

Public surface:

* :class:`StarsPaymentsService` — Telegram Stars (XTR) invoice generation
  and ``pre_checkout_query`` / ``successful_payment`` handling
  (task 25.1, Req 13.1 / 13.3 / 13.5).
* :class:`TonPaymentsService` — TON Connect payments: creates pending
  invoices and registers ``on_paid`` / ``on_expired`` / ``on_mismatch``
  hooks consumed by the polling scheduler job (task 26.1, Req 13.2 / 13.3).
* :class:`TonApiClient` — thin TonCenter v3 wrapper used by the poller
  to find confirming on-chain transactions (task 26.1, Req 13.2).
* :class:`PaymentsServices` — DI bundle held on ``AppServices.payments``
  when ``settings.feature_payments`` is true. Exposes per-provider
  services (``stars``, ``ton``).
* :class:`TonPaymentsServices` — inner bundle grouping
  :class:`TonPaymentsService` + :class:`TonApiClient`, held under
  :attr:`PaymentsServices.ton`.
* :data:`stars_router` — module-level aiogram ``Router`` for the Stars
  handlers. Included by :func:`app.bot.register_routers` when the Stars
  service is present; its handlers resolve the service instance via DI
  (``dispatcher["stars"]``).

Container wiring (see :mod:`app.container`) — Stage 6 sketch::

    if settings.feature_payments and settings.payments_provider in (
        "stars", "both"
    ):
        stars = StarsPaymentsService(bot, payments_repo, audit)
    if settings.feature_payments and settings.payments_provider in (
        "ton", "both"
    ):
        api_client = TonApiClient(http_session, settings.ton_api_url, ...)
        ton_svc = TonPaymentsService(
            bot, redis, users_repo, payments_repo, audit,
            ton_connector, api_client, settings,
        )
        ton = TonPaymentsServices(service=ton_svc, api_client=api_client)
    services.payments = PaymentsServices(stars=stars, ton=ton)

Requirements: 13.1, 13.2, 13.3, 13.5.
"""

from __future__ import annotations

from app.features.payments.services import (
    PaymentsServices,
    TonPaymentsServices,
)
from app.features.payments.stars import StarsPaymentsService
from app.features.payments.stars import router as stars_router
from app.features.payments.ton import (
    PaymentIntent,
    TonPaymentsService,
    WalletNotConnectedError,
)
from app.features.payments.ton_api import TonApiClient, TonTx

__all__ = [
    "PaymentIntent",
    "PaymentsServices",
    "StarsPaymentsService",
    "TonApiClient",
    "TonPaymentsService",
    "TonPaymentsServices",
    "TonTx",
    "WalletNotConnectedError",
    "stars_router",
]
