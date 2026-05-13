"""aiohttp web application — health-check, webhook, TON Connect manifest."""

from __future__ import annotations

from aiohttp import web

from app.container import AppServices
from app.core.healthcheck import healthz_factory


def build_web_app(services: AppServices) -> web.Application:
    app = web.Application()
    app["services"] = services

    app.router.add_get("/healthz", healthz_factory(services.engine, services.redis))

    # ------------------------------------------------------------------
    # Webhook (Stage 1 optional path; enabled via TG_MODE=webhook).
    # ------------------------------------------------------------------
    settings = services.settings
    if settings.tg_mode == "webhook":
        from aiogram.webhook.aiohttp_server import (
            SimpleRequestHandler,
            setup_application,
        )

        assert settings.webhook_secret is not None
        webhook_path = f"/tg/webhook/{settings.webhook_secret.get_secret_value()}"
        handler = SimpleRequestHandler(
            dispatcher=services.dispatcher,
            bot=services.bot,
            secret_token=settings.webhook_secret.get_secret_value(),
        )
        handler.register(app, path=webhook_path)
        setup_application(app, services.dispatcher, bot=services.bot)

    # Stage 3 — TON Connect manifest endpoint (only when the feature is on).
    if settings.feature_ton_connector:
        from app.ton.manifest import register_manifest_route

        register_manifest_route(app, settings)

    return app
