"""Модуль_Рассылки — admin-driven broadcast feature.

Public surface:

* :class:`BroadcastsServices` — DI bundle held on ``AppServices.broadcasts``
  when ``feature_broadcasts=True``. Exposes the shared ``Redis`` client and
  (once task 22.2 lands) the :class:`BroadcastWorker` instance.
* :data:`producer_router` — aiogram ``Router`` for the admin producer side:
  the ``/broadcast`` FSM dialog and the ``/broadcast_cancel <id>`` command
  (task 22.1, Req 8.1 / 8.5 / 8.6).
* :class:`BroadcastStates` — FSM states used by the producer dialog.

Container wiring (see :mod:`app.container`)::

    if settings.feature_broadcasts:
        services.broadcasts = BroadcastsServices(redis=redis, worker=None)

``app/bot.py`` then includes :data:`producer_router` and exposes the bundle
as ``dispatcher["broadcasts"]`` so handlers can resolve Redis directly.

Requirements: 8.1, 8.5, 8.6.
"""

from __future__ import annotations

from app.features.broadcasts.producer import BroadcastStates
from app.features.broadcasts.producer import router as producer_router
from app.features.broadcasts.services import BroadcastsServices
from app.features.broadcasts.worker import BroadcastWorker

__all__ = [
    "BroadcastStates",
    "BroadcastWorker",
    "BroadcastsServices",
    "producer_router",
]
