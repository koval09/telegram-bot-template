"""Application services container — a simple DI record.

Instantiated once at startup (see ``build_services``). Optional features are
created only when their feature flag is on; the rest are ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.repositories.audit import AuditRepo
from app.core.repositories.broadcasts import BroadcastsRepo
from app.core.repositories.payments import PaymentsRepo
from app.core.repositories.users import UsersRepo
from app.core.services.audit import AuditLog, SuperadminNotifier
from app.core.services.registration import RegistrationService

if TYPE_CHECKING:  # pragma: no cover
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.admin.authorization import Authorization
    from app.admin.user_manager import UserManager


@dataclass(slots=True)
class AppServices:
    """All wired services available at runtime."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    bot: Bot
    dispatcher: Dispatcher

    # Repositories
    users_repo: UsersRepo
    audit_repo: AuditRepo
    payments_repo: PaymentsRepo
    broadcasts_repo: BroadcastsRepo

    # Core services
    audit: AuditLog
    registration: RegistrationService

    # Optional (filled on later stages / by feature flags)
    scheduler: AsyncIOScheduler | None = None
    authorization: Authorization | None = None
    user_manager: UserManager | None = None
    ton: Any = None
    i18n: Any = None
    antispam: Any = None
    subscriptions: Any = None
    referrals: Any = None
    broadcasts: Any = None
    stats: Any = None
    payments: Any = None

    # Resources to close on shutdown
    extra_closers: list[Any] = field(default_factory=list)


async def build_services(settings: Settings) -> AppServices:
    """Wire the Stage 1 services.

    Later stages extend this function (or a dedicated factory) to instantiate
    admin/TON/features modules behind their feature flags (Req 15.2).
    """
    from app.bot import build_bot, build_dispatcher
    from app.core.cache.redis_client import create_redis
    from app.core.db.engine import create_engine, create_sessionmaker

    engine = create_engine(settings.db_dsn)
    sessionmaker = create_sessionmaker(engine)
    redis = await create_redis(str(settings.redis_url))

    users_repo = UsersRepo(sessionmaker)
    audit_repo = AuditRepo(sessionmaker)
    payments_repo = PaymentsRepo(sessionmaker)
    broadcasts_repo = BroadcastsRepo(sessionmaker)

    bot = build_bot(settings)

    first_superadmin = settings.superadmin_ids[0] if settings.superadmin_ids else None
    audit = AuditLog(
        audit_repo,
        superadmin_notifier=SuperadminNotifier(bot, first_superadmin),
    )

    registration = RegistrationService(users_repo, audit)

    # Stage 2 — Admin panel (always enabled: admin is considered part of core).
    from app.admin.authorization import Authorization
    from app.admin.user_manager import UserManager

    authorization = Authorization(redis, users_repo)
    user_manager = UserManager(users_repo, audit, authorization, bot)

    # Stage 3 — TON Connect (feature-flagged).
    ton: Any = None
    if settings.feature_ton_connector:
        from app.ton.connector import TonConnector

        ton = TonConnector(redis, users_repo, audit, settings)

    # Stage 4 — i18n (feature-flagged). Load catalogs up front so a missing
    # or broken default locale aborts startup (Req 15.3).
    i18n: Any = None
    if settings.feature_i18n:
        from app.features.i18n import Loader

        locales_dir = Path(__file__).resolve().parent / "locales"
        loader = Loader(
            locales_dir,
            default_locale=settings.default_locale,
            supported_locales=tuple(settings.supported_locales),
            audit=audit,
        )
        loader.load()
        i18n = loader

    # Stage 4 — antispam (feature-flagged). Builds RateLimiter + CaptchaService
    # into a small bundle consumed by ``register_middlewares`` / ``register_routers``.
    antispam: Any = None
    if settings.feature_antispam:
        from app.features.antispam import (
            AntispamServices,
            CaptchaService,
            RateLimiter,
        )

        antispam = AntispamServices(
            rate_limiter=RateLimiter(redis, audit=audit),
            captcha=CaptchaService(redis, users_repo, audit),
        )

    # Stage 4 — subscriptions (feature-flagged). Config guarantees
    # ``required_channels`` is non-empty when the flag is on (see
    # ``Settings.model_post_init`` / validator).
    subscriptions: Any = None
    if settings.feature_subscriptions:
        from app.features.subscriptions import (
            SubscriptionChecker,
            SubscriptionsServices,
        )

        subscriptions = SubscriptionsServices(
            checker=SubscriptionChecker(
                bot, redis, audit, settings.required_channels
            ),
        )

    # Stage 5 — referrals (feature-flagged). Fetch ``bot.me()`` once at
    # startup and cache the username inside the bundle (Req 7.1). If the
    # Telegram API call fails we log and disable the feature for this run
    # rather than taking the whole bot down — the other features remain
    # usable.
    #
    # The bundle also wires :class:`ReferralCreditingService`, which gates
    # inviter-counter increments behind captcha + subscriptions (antifraud
    # refinement on Req 7.2). It is constructed *after* the subscriptions
    # bundle above so the optional ``SubscriptionChecker`` can be passed
    # through to ``ReferralCreditingService``.
    referrals: Any = None
    if settings.feature_referrals:
        from app.features.referrals import (
            ReferralCreditingService,
            ReferralsServices,
        )

        try:
            me = await bot.me()
        except Exception as exc:
            import structlog

            structlog.get_logger(__name__).error(
                "referrals.bot_me_failed",
                error=repr(exc),
            )
        else:
            if me.username:
                subs_checker = (
                    subscriptions.checker if subscriptions is not None else None
                )
                crediting = ReferralCreditingService(
                    users_repo,
                    audit,
                    subs_checker=subs_checker,
                    require_captcha=settings.feature_antispam,
                )
                referrals = ReferralsServices(
                    bot_username=me.username,
                    crediting=crediting,
                )
                # Late-bind the crediting service into RegistrationService
                # so the "/start" path can settle the credit immediately
                # when neither captcha nor subscription gates apply
                # (antifraud refinement on Req 7.2: "credit only after
                # the invitee is real" — with both gates off they are
                # real the moment they hit /start).
                registration.attach_crediting(crediting)
                # Also late-bind the captcha post-pass callback so a
                # successful answer settles deferred credits without the
                # captcha service knowing about referrals. The wrapper
                # discards the :class:`CreditOutcome` so the signature
                # matches ``Callable[[int], Awaitable[None]]`` and any
                # downstream failure is already audited inside the
                # crediting service.
                if antispam is not None:

                    async def _credit_on_captcha_pass(user_id: int) -> None:
                        await crediting.try_credit(user_id)

                    antispam.captcha.attach_on_passed(_credit_on_captcha_pass)
            else:
                import structlog

                structlog.get_logger(__name__).error(
                    "referrals.bot_username_missing",
                    bot_id=me.id,
                )

    # Stage 5 — broadcasts (feature-flagged). The producer router only
    # needs Redis (LPUSH / LLEN / SET). The worker is a single background
    # asyncio task instantiated here and started from ``app/__main__.py``
    # after the dispatcher is ready so shutdown can await it.
    broadcasts: Any = None
    if settings.feature_broadcasts:
        from app.features.broadcasts import BroadcastsServices, BroadcastWorker

        broadcast_worker = BroadcastWorker(
            bot=bot,
            redis=redis,
            broadcasts_repo=broadcasts_repo,
            users_repo=users_repo,
            audit=audit,
        )
        broadcasts = BroadcastsServices(redis=redis, worker=broadcast_worker)

    # Stage 5 — stats (feature-flagged). Single :class:`StatsService`
    # instance bundled so the admin-only ``/stats`` handler reaches it
    # through ``dispatcher["stats"].service`` (see ``app/bot.py``).
    stats: Any = None
    if settings.feature_stats:
        from app.features.stats import StatsService, StatsServices

        stats = StatsServices(service=StatsService(sessionmaker))

    # Stage 6 — payments (feature-flagged). Stars lands in task 25.1 and
    # TON in task 26.1. Both slot into the same :class:`PaymentsServices`
    # bundle so ``app/bot.py`` can register the Stars router and the
    # scheduler job (task 26.2) can read the TON service from one place.
    # A Stars-only or TON-only deployment leaves the opposite slot as
    # ``None``.
    payments: Any = None
    if settings.feature_payments:
        from app.features.payments import PaymentsServices

        stars_service: Any = None
        if settings.payments_provider in ("stars", "both"):
            from app.features.payments import StarsPaymentsService

            stars_service = StarsPaymentsService(bot, payments_repo, audit)

        ton_bundle: Any = None
        if settings.payments_provider in ("ton", "both"):
            # TON payments require a live TON Connect wiring — the user
            # must be able to sign the transfer from their bound wallet.
            # If ``feature_ton_connector`` is off we log and fall back
            # to Stars-only rather than crashing: this matches the "graceful
            # degradation" pattern we use for referrals/bot.me above.
            if ton is None:
                import structlog

                structlog.get_logger(__name__).error(
                    "payments.ton.requires_ton_connector",
                    detail=(
                        "FEATURE_PAYMENTS with payments_provider=ton/both "
                        "requires FEATURE_TON_CONNECTOR=true"
                    ),
                )
            else:
                import aiohttp

                from app.features.payments import (
                    TonApiClient,
                    TonPaymentsService,
                    TonPaymentsServices,
                )

                # Dedicated aiohttp session for TonCenter — kept off the
                # aiogram session so bot calls don't share a connection
                # pool with a third-party API. Closed in
                # :func:`close_services` via ``extra_closers``.
                ton_http = aiohttp.ClientSession()
                api_client = TonApiClient(
                    ton_http,
                    str(settings.ton_api_url),
                    api_key=(
                        settings.ton_api_key.get_secret_value()
                        if settings.ton_api_key is not None
                        else None
                    ),
                )
                ton_service = TonPaymentsService(
                    bot,
                    redis,
                    users_repo,
                    payments_repo,
                    audit,
                    ton,
                    api_client,
                    settings,
                )
                ton_bundle = TonPaymentsServices(
                    service=ton_service, api_client=api_client
                )

        payments = PaymentsServices(stars=stars_service, ton=ton_bundle)

    # Collect closers for any per-feature resources (aiohttp sessions,
    # etc.) so :func:`close_services` can release them in reverse order.
    extra_closers: list[Any] = []
    if payments is not None and payments.ton is not None:
        # ``ton_http`` was created above in the TON branch; close it on
        # shutdown. Reaching through the bundle keeps the wiring in one
        # spot even though the local reference is gone.
        ton_http_session = payments.ton.api_client._session
        extra_closers.append(ton_http_session.close)

    services = AppServices(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        redis=redis,
        bot=bot,
        dispatcher=build_dispatcher(settings, redis),
        users_repo=users_repo,
        audit_repo=audit_repo,
        payments_repo=payments_repo,
        broadcasts_repo=broadcasts_repo,
        audit=audit,
        registration=registration,
        authorization=authorization,
        user_manager=user_manager,
        ton=ton,
        i18n=i18n,
        antispam=antispam,
        subscriptions=subscriptions,
        referrals=referrals,
        broadcasts=broadcasts,
        stats=stats,
        payments=payments,
        extra_closers=extra_closers,
    )
    return services


async def close_services(services: AppServices) -> None:
    """Close resources on shutdown (reverse order of creation)."""
    if services.scheduler is not None:
        try:
            services.scheduler.shutdown(wait=True)
        except Exception:
            pass
    for closer in reversed(services.extra_closers):
        try:
            res = closer()
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass
    try:
        await services.bot.session.close()
    except Exception:
        pass
    try:
        await services.dispatcher.storage.close()
    except Exception:
        pass
    try:
        await services.redis.aclose()
    except Exception:
        pass
    try:
        await services.engine.dispose()
    except Exception:
        pass
