"""Bot entry point.

Responsibilities:
- Load and validate settings (fail-fast on missing/invalid config).
- Configure structured logging.
- Wire services (``AppServices``).
- Log component versions to ``Журнал_Действий`` BEFORE accepting updates
  (Requirement 17.5).
- Start the web app (health-check + optional webhook).
- Start the Dispatcher (polling or webhook).
- On SIGTERM/SIGINT — graceful shutdown (close sessions, dispose pools).
"""

from __future__ import annotations

import asyncio
import importlib.metadata as meta_
import signal
from typing import Any

import structlog
from aiohttp import web

from app.bot import register_middlewares, register_routers
from app.config import Settings, load_settings
from app.container import AppServices, build_services, close_services
from app.core.utils.logging import setup_logging

log = structlog.get_logger(__name__)


def _safe_version(pkg: str) -> str:
    try:
        return meta_.version(pkg)
    except Exception:
        return "unknown"


async def _log_startup_versions(services: AppServices) -> None:
    versions = {
        "aiogram": _safe_version("aiogram"),
        "sqlalchemy": _safe_version("sqlalchemy"),
        "redis": _safe_version("redis"),
    }
    if services.settings.feature_ton_connector:
        versions["pytonconnect"] = _safe_version("pytonconnect")
    log.info("startup.versions", **versions)
    await services.audit.record_info(event="startup", details=versions)


async def _serve_http(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("http.started", host=host, port=port)
    return runner


async def _install_shutdown_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _signal_handler() -> None:
        if not stop_event.is_set():
            log.info("shutdown.signal_received")
            stop_event.set()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)
    except NotImplementedError:  # pragma: no cover — Windows
        # Signals via add_signal_handler aren't supported on Windows event loops.
        # KeyboardInterrupt still triggers cleanup via the outer run() wrapper.
        pass


async def _amain() -> int:
    settings: Settings = load_settings()
    setup_logging(settings.log_level)

    log.info("startup.config_loaded", tg_mode=settings.tg_mode, http_port=settings.http_port)

    services = await build_services(settings)

    # Stage 2 — seed superadmins from env and start scheduler.
    from app.scheduler.jobs import build_scheduler

    if services.authorization is not None:
        await services.authorization.seed_superadmins(settings.superadmin_ids)
    services.scheduler = build_scheduler(services)
    services.scheduler.start()

    register_middlewares(services.dispatcher, services)
    register_routers(services.dispatcher, services)

    await _log_startup_versions(services)

    # Stage 5 — spin up the BroadcastWorker consumer when the feature is on.
    # Container only constructs it; starting the asyncio task here keeps
    # startup order predictable and ensures ``stop()`` runs before we close
    # Redis in the graceful-shutdown path below.
    if services.broadcasts is not None and services.broadcasts.worker is not None:
        await services.broadcasts.worker.start()

    web_app = _build_web_app_lazy(services)
    runner = await _serve_http(web_app, settings.http_host, settings.http_port)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    await _install_shutdown_handlers(loop, stop_event)

    if settings.tg_mode == "polling":
        poll_task = asyncio.create_task(
            services.dispatcher.start_polling(
                services.bot,
                handle_as_tasks=True,
                allowed_updates=services.dispatcher.resolve_used_update_types(),
            ),
            name="aiogram.polling",
        )
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown.wait")
        done, pending = await asyncio.wait(
            {poll_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if poll_task in done and poll_task.exception() is not None:
            log.error("polling.crashed", error=repr(poll_task.exception()))
        # Stop polling if still running.
        if not poll_task.done():
            await services.dispatcher.stop_polling()
            try:
                await asyncio.wait_for(poll_task, timeout=10)
            except Exception as exc:
                log.warning("polling.shutdown_timeout", error=repr(exc))
        for task in pending:
            task.cancel()
    else:
        # Webhook mode: just wait for the signal.
        await stop_event.wait()

    log.info("shutdown.begin")
    # Stop the broadcasts worker before closing Redis so its last BRPOP /
    # counter flush can unwind cleanly.
    if services.broadcasts is not None and services.broadcasts.worker is not None:
        try:
            await services.broadcasts.worker.stop(timeout=10)
        except Exception as exc:
            log.warning("shutdown.broadcasts_worker_stop_failed", error=repr(exc))
    try:
        await runner.cleanup()
    except Exception as exc:
        log.warning("shutdown.http_cleanup_failed", error=repr(exc))
    await close_services(services)
    log.info("shutdown.complete")
    return 0


def _build_web_app_lazy(services: AppServices) -> web.Application:
    # Late import to avoid a circular import at module load time.
    from app.web import build_web_app

    return build_web_app(services)


def main() -> Any:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
