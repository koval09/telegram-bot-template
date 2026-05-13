"""APScheduler jobs registered at startup.

Stage 2: daily cleanup of ``action_log`` entries older than 90 days
(Requirement 6.4). Stage 3 adds per-minute TON Connect session cleanup
(Requirement 3.5). Stage 6 adds per-minute TON payments polling
(Requirements 13.2 / 13.3 / 13.4 / 13.5).

The actual task callables are imported from :mod:`app.scheduler.tasks` so
they stay importable in environments where APScheduler is not installed
(e.g. minimal smoke tests).
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.container import AppServices
from app.scheduler.tasks import (
    audit_cleanup,
    ton_payments_poll,
    ton_session_cleanup,
)


def build_scheduler(services: AppServices) -> AsyncIOScheduler:
    """Build the scheduler and register Stage 2+ jobs based on feature flags."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        audit_cleanup,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        kwargs={"services": services},
        id="audit_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Stage 3 — TON Connect session cleanup (Requirement 3.5). Registered
    # only when the connector is wired in, so the job list matches the
    # active feature flags.
    if services.ton is not None:
        scheduler.add_job(
            ton_session_cleanup,
            trigger=IntervalTrigger(seconds=60),
            kwargs={"services": services},
            id="ton_session_cleanup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Stage 6 — TON payments polling (Req 13.2 / 13.3 / 13.4 / 13.5).
    # Registered only when the TON payments bundle is wired in
    # (``payments_provider in {"ton", "both"}`` AND TON Connector is on).
    # ``max_instances=1 + coalesce=True`` guarantees a slow poll does not
    # stack; the next tick waits instead of running concurrently.
    if (
        services.payments is not None
        and getattr(services.payments, "ton", None) is not None
    ):
        scheduler.add_job(
            ton_payments_poll,
            trigger=IntervalTrigger(minutes=1),
            kwargs={"services": services},
            id="ton_payments_poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    return scheduler
