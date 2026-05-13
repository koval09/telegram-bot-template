"""``Модуль_Антиспам`` aiogram middleware — ties RateLimiter + CaptchaService.

Order of operations (see task 18.3 / design § антиспам / Req 11.1–11.3):

1. **Block short-circuit** — if the user is currently inside a captcha
   lockout (``captcha:block:{id}``) or a rate-limit block (``rl:block:{id}``)
   the update is swallowed silently. The user has already been told during
   the call that created the block, so we do not flood them further.
2. **Captcha gate** — a brand-new user (``registration.created=True``) or
   one whose status is ``pending_captcha`` must solve a captcha before any
   handler runs. Mid-way through this path the user's DB status is flipped
   to ``pending_captcha`` so the gate survives a bot restart.
3. **Rate-limit** — everyone else goes through
   :meth:`RateLimiter.check_and_record`. On the very first call that trips
   the block we send a one-shot "Слишком много сообщений" notice
   (``block_triggered=True``); every subsequent update inside the 30-second
   block window is silently swallowed by step 1.

Callback queries whose ``callback_data`` starts with ``cap:`` bypass the
middleware entirely so the captcha handler can resolve answers even when
the user is in ``pending_captcha``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis

from app.core.db.models import User, UserStatus
from app.core.repositories.users import UsersRepo
from app.core.utils.clock import Clock, utc_now
from app.features.antispam.captcha import (
    CB_PREFIX,
    CaptchaService,
    build_keyboard,
    render_question,
)
from app.features.antispam.ratelimit import RateLimiter

log = structlog.get_logger(__name__)

# Fallback strings — used when the i18n translator is absent or does not
# know the key. They mirror the Russian wording used elsewhere in the bot
# (see app/locales/ru.yml and app/features/antispam/captcha.py).
_MSG_RATELIMIT = "Слишком много сообщений. Подождите немного."
_MSG_CAPTCHA_REMIND = "Пожалуйста, решите капчу, прежде чем продолжить."

# Redis key used by ``CaptchaService`` — duplicated here so the middleware
# can ask "is there an active challenge?" without modifying the service
# contract. Matches ``captcha:challenge:{user_id}`` in ``captcha.py``.
_CAPTCHA_CHALLENGE_KEY = "captcha:challenge:{user_id}"


class AntispamMiddleware(BaseMiddleware):
    """Outer middleware implementing Req 11.1 / 11.2 / 11.3."""

    def __init__(
        self,
        rate_limiter: RateLimiter,
        captcha: CaptchaService,
        users: UsersRepo,
        redis: Redis,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._rl = rate_limiter
        self._captcha = captcha
        self._users = users
        self._redis = redis
        self._clock = clock

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Captcha button presses must always reach the captcha router even
        # when the user is in ``pending_captcha``. Everything else goes
        # through the full pipeline below.
        if _is_captcha_callback(event):
            return await handler(event, data)

        user = data.get("user")
        if not isinstance(user, User) or getattr(event, "from_user", None) is None:
            # No registered user — registration middleware either skipped
            # this event (e.g. chat_member updates) or failed. Nothing for
            # the antispam module to do in either case.
            return await handler(event, data)

        user_id = user.telegram_id
        translator = data.get("_")

        # ------------------------------------------------------------------
        # (a) Block short-circuit.
        # ------------------------------------------------------------------
        if await self._captcha.is_blocked(user_id):
            log.debug("antispam.middleware.captcha_blocked", user_id=user_id)
            return None
        if await self._rl.is_blocked(user_id):
            log.debug("antispam.middleware.ratelimit_blocked", user_id=user_id)
            return None

        # ------------------------------------------------------------------
        # (b) Captcha gate for fresh users and anyone in pending_captcha.
        # ------------------------------------------------------------------
        registration = data.get("registration")
        is_new = bool(getattr(registration, "created", False))
        if is_new or user.status == UserStatus.pending_captcha:
            await self._gate_with_captcha(event, user, translator)
            return None

        # ------------------------------------------------------------------
        # (c) Regular rate-limit path.
        # ------------------------------------------------------------------
        result = await self._rl.check_and_record(user_id)
        if result.allowed:
            return await handler(event, data)

        if result.block_triggered:
            # Exactly-once notification per 30-second block window
            # (Req 11.3). Subsequent updates inside the window are silenced
            # by step (a) above.
            await self._notify_ratelimit(event, translator)
        return None

    # ------------------------------------------------------------------
    # Captcha gating
    # ------------------------------------------------------------------
    async def _gate_with_captcha(
        self,
        event: TelegramObject,
        user: User,
        translator: Any,
    ) -> None:
        """Issue or re-surface a captcha for ``user``.

        A brand-new challenge is built only when no hash exists in Redis
        (task 18.3: *"do not re-ask captcha on every wrong answer — only
        when no active challenge hash exists OR it expired"*). When a
        challenge is still alive we send a short text reminder without a
        new keyboard so the user's ``tries`` counter is preserved.
        """
        key = _CAPTCHA_CHALLENGE_KEY.format(user_id=user.telegram_id)
        has_active = bool(await self._redis.exists(key))

        if not has_active:
            challenge = await self._captcha.build_challenge(user.telegram_id)
            text = render_question(challenge, translator)
            await self._send_chat_message(
                event, text, reply_markup=build_keyboard(challenge)
            )
            # Persist ``pending_captcha`` so the gate survives across
            # restarts and re-reads of the user (Req 11.1 / 11.2).
            if user.status != UserStatus.pending_captcha:
                await self._users.set_status(
                    user.telegram_id,
                    UserStatus.pending_captcha,
                    now=self._clock(),
                )
                user.status = UserStatus.pending_captcha
            log.info(
                "antispam.middleware.captcha_issued",
                user_id=user.telegram_id,
                new_user=user.status == UserStatus.pending_captcha,
            )
            return

        # Challenge still alive — just nudge the user.
        text = _translate(
            translator,
            "antispam.captcha.remind",
            _MSG_CAPTCHA_REMIND,
        )
        await self._send_chat_message(event, text)
        log.debug(
            "antispam.middleware.captcha_reminded",
            user_id=user.telegram_id,
        )

    # ------------------------------------------------------------------
    # Rate-limit notification
    # ------------------------------------------------------------------
    async def _notify_ratelimit(
        self,
        event: TelegramObject,
        translator: Any,
    ) -> None:
        text = _translate(
            translator,
            "antispam.ratelimit.blocked",
            _MSG_RATELIMIT,
        )
        await self._send_chat_message(event, text)

    # ------------------------------------------------------------------
    # Low-level send helper
    # ------------------------------------------------------------------
    @staticmethod
    async def _send_chat_message(
        event: TelegramObject,
        text: str,
        *,
        reply_markup: Any | None = None,
    ) -> None:
        """Send ``text`` in the same chat as ``event``, swallowing errors.

        Telegram can fail for reasons outside our control (user blocked
        the bot, permission revoked, etc.). The middleware must never
        propagate those errors because it runs for *every* update.
        """
        if isinstance(event, Message):
            with suppress(Exception):
                await event.answer(text, reply_markup=reply_markup)
            return
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                # Dismiss the spinner on the button so the UI does not hang.
                await event.answer()
            target = event.message
            if target is not None:
                with suppress(Exception):
                    await target.answer(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _is_captcha_callback(event: TelegramObject) -> bool:
    return (
        isinstance(event, CallbackQuery)
        and (event.data or "").startswith(CB_PREFIX)
    )


def _translate(
    translator: Any,
    key: str,
    fallback: str,
    **kwargs: Any,
) -> str:
    """Translate ``key`` or return the Russian ``fallback``.

    ``Translator`` from ``app.features.i18n`` returns the key unchanged
    when the string is missing; we treat that as "use the fallback" so
    the user never sees raw dotted keys.
    """
    if translator is not None:
        try:
            value = translator(key, **kwargs)
        except Exception as exc:
            log.debug(
                "antispam.middleware.translate_failed",
                key=key,
                error=repr(exc),
            )
            value = key
        if value != key:
            return value
    if kwargs:
        try:
            return fallback.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return fallback
    return fallback
