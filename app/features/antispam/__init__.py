"""Модуль_Антиспам — rate-limiting, captcha, and the bundling middleware.

Public surface:

* :class:`RateLimiter` — sliding-window rate limiter (task 18.1, Req 11.3 / 11.4).
* :class:`CaptchaService` et al. — captcha challenge/verification (task 18.2,
  Req 11.1 / 11.2).
* :class:`AntispamMiddleware` — aiogram middleware that wires both together
  (task 18.3, Req 11.1 / 11.2 / 11.3).
* :class:`AntispamServices` — DI bundle consumed by ``app.container`` and
  ``app.bot`` so the middleware can be constructed behind a single feature
  flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.features.antispam.captcha import (
    CAPTCHA_BLOCK_TTL_SEC,
    CAPTCHA_TTL_SEC,
    MAX_TRIES,
    OPTIONS_COUNT,
    CaptchaService,
    Challenge,
    VerifyResult,
    build_keyboard,
    render_question,
)
from app.features.antispam.captcha import router as captcha_router
from app.features.antispam.middleware import AntispamMiddleware
from app.features.antispam.ratelimit import (
    DEFAULT_BLOCK_SECONDS,
    DEFAULT_MAX_EVENTS,
    DEFAULT_WINDOW_SECONDS,
    RateLimiter,
    RateLimitResult,
)


@dataclass(slots=True)
class AntispamServices:
    """Bundle of services exposed when ``feature_antispam=True``.

    Held on ``AppServices.antispam``; the bot wiring reads it to register
    :class:`AntispamMiddleware` and :data:`captcha_router`.
    """

    rate_limiter: RateLimiter
    captcha: CaptchaService


__all__ = [
    # Bundle
    "AntispamServices",
    # Rate limiter (task 18.1)
    "DEFAULT_BLOCK_SECONDS",
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_WINDOW_SECONDS",
    "RateLimiter",
    "RateLimitResult",
    # Captcha (task 18.2)
    "CAPTCHA_BLOCK_TTL_SEC",
    "CAPTCHA_TTL_SEC",
    "MAX_TRIES",
    "OPTIONS_COUNT",
    "CaptchaService",
    "Challenge",
    "VerifyResult",
    "build_keyboard",
    "captcha_router",
    "render_question",
    # Middleware (task 18.3)
    "AntispamMiddleware",
]
