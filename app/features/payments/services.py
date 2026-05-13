"""Модуль_Платежей — DI bundle consumed by ``app.container`` / ``app.bot``.

Mirrors the Stage 5 ``*Services`` pattern (see
:mod:`app.features.broadcasts.services`, :mod:`app.features.stats.services`):
kept as a thin dataclass so both the provider-specific modules (Stars in
:mod:`app.features.payments.stars`, TON in :mod:`app.features.payments.ton`)
and the wiring in :mod:`app.container` / :mod:`app.bot` can import it
without pulling in the provider implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.features.payments.stars import StarsPaymentsService
    from app.features.payments.ton import TonPaymentsService
    from app.features.payments.ton_api import TonApiClient


@dataclass(slots=True)
class TonPaymentsServices:
    """TON-specific bundle held under :attr:`PaymentsServices.ton`.

    Groups the :class:`TonPaymentsService` with the
    :class:`TonApiClient` so task 26.2's APScheduler poller can reach
    both through a single ``services.payments.ton`` handle.
    """

    service: TonPaymentsService
    api_client: TonApiClient


@dataclass(slots=True)
class PaymentsServices:
    """Bundle exposed on ``AppServices.payments`` when payments are enabled.

    Container wiring (see :mod:`app.container`)::

        if settings.feature_payments and settings.payments_provider in (
            "stars", "both"
        ):
            stars = StarsPaymentsService(bot, payments_repo, audit)
        if settings.feature_payments and settings.payments_provider in (
            "ton", "both"
        ):
            api_client = TonApiClient(http_session, settings.ton_api_url, ...)
            ton_svc = TonPaymentsService(bot, redis, users_repo, payments_repo,
                                         audit, ton_connector, api_client,
                                         settings)
            ton = TonPaymentsServices(service=ton_svc, api_client=api_client)
        services.payments = PaymentsServices(stars=stars, ton=ton)

    Attributes:
        stars: :class:`StarsPaymentsService` when the configured
            ``payments_provider`` includes Stars, otherwise ``None``.
        ton: :class:`TonPaymentsServices` bundle when the configured
            ``payments_provider`` includes TON, otherwise ``None``.
    """

    stars: StarsPaymentsService | None = None
    ton: TonPaymentsServices | None = None


__all__ = ["PaymentsServices", "TonPaymentsServices"]
