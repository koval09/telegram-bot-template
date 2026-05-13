"""Модуль_Рассылки — DI bundle consumed by ``app.container`` / ``app.bot``.

Split out into its own module so both :mod:`app.features.broadcasts.producer`
(task 22.1) and :mod:`app.features.broadcasts.worker` (task 22.2) can import
:class:`BroadcastsServices` without introducing an import cycle via
``app.features.broadcasts.__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis


@dataclass(slots=True)
class BroadcastsServices:
    """Bundle of services exposed when ``feature_broadcasts=True``.

    Held on ``AppServices.broadcasts``; the bot wiring reads it to register
    the producer router and (once task 22.2 lands) the consumer worker.

    Attributes:
        redis: The shared async Redis client. The producer uses it to push
            jobs onto ``bcast:queue`` (``LPUSH``), check queue length
            (``LLEN``, Req 8.5) and persist the ``bcast:cancel:<id>`` flag
            (``SET ... EX 3600``, Req 8.6). The worker (task 22.2) consumes
            the same keys.
        worker: Filled by task 22.2. Kept as ``Any`` for now so this bundle
            stays importable before the worker class exists. ``None`` means
            the consumer is not running yet.
    """

    redis: Redis
    worker: Any = None


__all__ = ["BroadcastsServices"]
