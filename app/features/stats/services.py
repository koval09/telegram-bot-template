"""Модуль_Статистики — DI bundle consumed by ``app.container`` / ``app.bot``.

Kept as a thin dataclass so both the handler module and the container can
import it without pulling in the service implementation itself (mirrors the
pattern from :mod:`app.features.broadcasts.services`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.features.stats.service import StatsService


@dataclass(slots=True)
class StatsServices:
    """Bundle exposed on ``AppServices.stats`` when ``feature_stats=True``.

    Attributes:
        service: The :class:`StatsService` instance that runs aggregation
            queries. Handlers retrieve it via aiogram DI from
            ``dispatcher["stats"]`` (see ``app/bot.py``).
    """

    service: StatsService


__all__ = ["StatsServices"]
