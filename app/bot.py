"""Bot and Dispatcher factories.

Middleware order (Req 1/2.4/5.2/5.4 + 11/12):

    Registration → StatusGate → (Antispam) → (I18n) → (Subscriptions) → handler

Routers are registered conditionally depending on feature flags (Req 15.2).
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from redis.asyncio import Redis

from app.config import Settings
from app.core.services.fsm import create_fsm_storage


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(settings: Settings, redis: Redis) -> Dispatcher:
    storage = create_fsm_storage(redis, ttl_seconds=settings.fsm_timeout_seconds)
    dispatcher = Dispatcher(storage=storage)
    # Expose settings to every handler via aiogram's data injection.
    dispatcher["settings"] = settings
    return dispatcher


def register_middlewares(dispatcher: Dispatcher, services: AppServices) -> None:  # type: ignore[name-defined]
    from app.core.middlewares.registration import RegistrationMiddleware
    from app.core.middlewares.status_gate import StatusGateMiddleware

    reg_mw = RegistrationMiddleware(services.registration)
    status_mw = StatusGateMiddleware(services.redis, services.users_repo)

    # ``update`` middlewares run for every incoming update type.
    dispatcher.update.outer_middleware(reg_mw)
    dispatcher.message.outer_middleware(status_mw)
    dispatcher.callback_query.outer_middleware(status_mw)

    # Stage 4 — antispam / i18n / subscriptions — wired here once they land.
    # Order (design.md): Registration → StatusGate → (Antispam) → (I18n) → (Subscriptions) → handler.
    if services.antispam is not None:
        from app.features.antispam import AntispamMiddleware

        antispam_mw = AntispamMiddleware(
            services.antispam.rate_limiter,
            services.antispam.captcha,
            services.users_repo,
            services.redis,
        )
        dispatcher.message.outer_middleware(antispam_mw)
        dispatcher.callback_query.outer_middleware(antispam_mw)
        dispatcher["antispam"] = services.antispam
        dispatcher["captcha"] = services.antispam.captcha

    if services.i18n is not None:
        from app.features.i18n.middleware import I18nMiddleware

        i18n_mw = I18nMiddleware(services.i18n)
        # Keep I18n AFTER Registration/StatusGate/Antispam so the design
        # order (Registration → StatusGate → Antispam → I18n → handler)
        # holds. Registered on the same observers as StatusGate/Antispam
        # so the per-observer middleware queue preserves that order.
        dispatcher.message.outer_middleware(i18n_mw)
        dispatcher.callback_query.outer_middleware(i18n_mw)
        dispatcher["i18n"] = services.i18n

    if services.subscriptions is not None:
        from app.features.subscriptions import SubscriptionsMiddleware

        subs_mw = SubscriptionsMiddleware(services.subscriptions.checker)
        # Registered AFTER I18n so the design order
        # (Registration → StatusGate → Antispam → I18n → Subscriptions → handler)
        # holds. Only bound to ``dispatcher.message`` because the gate
        # applies to command invocations; callback queries (including the
        # re-check button itself) must reach the router regardless.
        dispatcher.message.outer_middleware(subs_mw)
        dispatcher["subscriptions"] = services.subscriptions

    # Stage 2 — admin-related middlewares (none needed: filters handle access).


def register_routers(dispatcher: Dispatcher, services: AppServices) -> None:  # type: ignore[name-defined]
    """Register routers behind feature flags."""
    from app.admin import handlers as h_admin
    from app.core.handlers import cancel as h_cancel
    from app.core.handlers import profile as h_profile
    from app.core.handlers import start as h_start

    # Core commands are always on (Stage 1 = MVP).
    dispatcher.include_router(h_start.router)
    dispatcher.include_router(h_profile.router)
    dispatcher.include_router(h_cancel.router)

    # Stage 2 — admin panel is part of core; filters gate access.
    dispatcher.include_router(h_admin.router)

    # Stage 3 — TON Connect router is wired in only when the connector was
    # constructed (feature_ton_connector is true). See app/container.py.
    if services.ton is not None:
        from app.ton import handlers as h_ton

        dispatcher.include_router(h_ton.router)
        dispatcher["ton"] = services.ton

    # Stage 4 — antispam captcha router (feature_antispam).
    if services.antispam is not None:
        from app.features.antispam import captcha_router

        dispatcher.include_router(captcha_router)

    # Stage 4 — subscriptions router (feature_subscriptions). The
    # dispatcher data slot is already populated in ``register_middlewares``
    # above so the handler can resolve the checker via ``**data``.
    if services.subscriptions is not None:
        from app.features.subscriptions import subscriptions_router

        dispatcher.include_router(subscriptions_router)

    # Stage 5 — referrals router (feature_referrals). The bundle holds the
    # cached ``bot_username`` from ``bot.me()`` (Req 7.1); handlers receive
    # it via aiogram DI keyed on ``referrals``.
    if services.referrals is not None:
        from app.features.referrals import referrals_router

        dispatcher.include_router(referrals_router)
        dispatcher["referrals"] = services.referrals

    # Stage 5 — broadcasts producer router (feature_broadcasts). The worker
    # (task 22.2) is started separately from ``app/__main__.py``; here we
    # only need to include the admin-facing producer router and expose the
    # Redis client by name so FSM handlers get it injected via DI.
    if services.broadcasts is not None:
        from app.features.broadcasts import producer_router

        dispatcher.include_router(producer_router)
        dispatcher["broadcasts"] = services.broadcasts
        # ``redis`` is already reachable through ``services.redis`` but the
        # producer handlers expect a plain ``redis`` kwarg (matches the
        # pattern used for ``audit`` / ``users_repo``).
        dispatcher["redis"] = services.redis

    # Stage 5 — stats router (feature_stats). The admin ``/stats`` handler
    # is gated by ``IsAdminFilter``; we expose the bundle via DI so the
    # handler can call ``stats.service.get_overview(now)`` directly.
    if services.stats is not None:
        from app.features.stats import stats_router

        dispatcher.include_router(stats_router)
        dispatcher["stats"] = services.stats

    # Stage 6 — payments router(s). Only Stars is wired here; TON lands
    # in task 26.1. Handlers resolve the service via aiogram DI keyed on
    # the provider name (``dispatcher["stars"]``), matching the pattern
    # used for referrals/broadcasts/stats above.
    if services.payments is not None and services.payments.stars is not None:
        from app.features.payments import stars_router

        dispatcher.include_router(stars_router)
        dispatcher["payments"] = services.payments
        dispatcher["stars"] = services.payments.stars

    # Make services discoverable by handlers via dispatcher data.
    dispatcher["users_repo"] = services.users_repo
    dispatcher["authorization"] = services.authorization
    dispatcher["user_manager"] = services.user_manager
    dispatcher["audit"] = services.audit

    # Stages 4..6 register their own routers here.


# ----------------------------------------------------------------------------
# For type-checker only; avoids a circular import at module import time.
# ----------------------------------------------------------------------------
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from app.container import AppServices
