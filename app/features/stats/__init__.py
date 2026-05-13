"""Модуль_Статистики — admin ``/stats`` feature bundle.

Public surface:

* :class:`StatsService` — read-only aggregation service over ``users``
  (:mod:`app.features.stats.service`).
* :class:`StatsOverview`, :class:`DayCount` — value objects returned by
  the service.
* :class:`StatsServices` — DI bundle exposed on ``AppServices.stats`` when
  ``settings.feature_stats`` is true
  (:mod:`app.features.stats.services`).
* :data:`stats_router` — aiogram ``Router`` with the ``/stats`` handler,
  gated by :class:`~app.admin.filters.IsAdminFilter` (task 23.1,
  Req 9.1 / 9.3).

Container wiring (see :mod:`app.container`)::

    if settings.feature_stats:
        services.stats = StatsServices(service=StatsService(sessionmaker))

``app/bot.py`` then includes :data:`stats_router` and exposes the bundle
as ``dispatcher["stats"]`` so the handler resolves it via DI.

Requirements: 9.1, 9.2, 9.3.
"""

from __future__ import annotations

from app.features.stats.handlers import router as stats_router
from app.features.stats.service import DayCount, StatsOverview, StatsService
from app.features.stats.services import StatsServices

__all__ = [
    "DayCount",
    "StatsOverview",
    "StatsService",
    "StatsServices",
    "stats_router",
]
