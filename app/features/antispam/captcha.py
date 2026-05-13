"""Модуль_Антиспам — captcha challenge service and callback handler.

Requirements 11.1, 11.2:

* **11.1** — when Модуль_Антиспам is enabled, a new user must pass a captcha
  before reaching other commands. The middleware (task 18.3) decides *when*
  to show the captcha; this module builds and verifies it.
* **11.2** — if the user does not complete the captcha within 60 seconds the
  challenge in Redis expires, the user is left at ``status=pending_captcha``
  and the middleware reissues a fresh challenge on the next message.

Redis layout (all keys scoped by the user's ``telegram_id``):

* ``captcha:challenge:{id}`` — hash with fields ``correct``, ``tries``,
  ``expires_at`` (ISO 8601 UTC). TTL 60 s.
* ``captcha:block:{id}`` — lockout flag set after 3 wrong answers. TTL 300 s.

Callback data format: ``cap:{user_id}:{value}`` — the ``user_id`` is used as
a scope check against ``query.from_user.id`` in the handler.

Public surface:

* :class:`Challenge`, :class:`VerifyResult` — result dataclasses.
* :class:`CaptchaService` — stateless service bound to Redis + deps.
* :data:`router` — aiogram Router with the callback handler.
* :func:`build_keyboard`, :func:`render_question` — rendering helpers used by
  the middleware (task 18.3).
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from redis.asyncio import Redis

from app.core.db.models import UserStatus
from app.core.repositories.users import UsersRepo
from app.core.services.audit import AuditLog
from app.core.utils.clock import Clock, utc_now

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants (locked by the spec — do not change without updating the
# design document).
# ---------------------------------------------------------------------------

CAPTCHA_TTL_SEC = 60
CAPTCHA_BLOCK_TTL_SEC = 300
MAX_TRIES = 3
OPTIONS_COUNT = 4

CB_PREFIX = "cap:"

_CHALLENGE_KEY = "captcha:challenge:{user_id}"
_BLOCK_KEY = "captcha:block:{user_id}"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Challenge:
    """Captcha challenge issued for a single user."""

    user_id: int
    a: int
    b: int
    correct: int
    options: tuple[int, ...]
    expires_at: datetime

    @property
    def question_text(self) -> str:
        return f"{self.a} + {self.b}"


VerifyStatus = Literal[
    "correct",
    "wrong",
    "blocked",
    "expired",
    "no_challenge",
]


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of :meth:`CaptchaService.verify_answer`."""

    status: VerifyStatus
    tries: int = 0  # number of wrong attempts so far on this challenge

    @property
    def ok(self) -> bool:
        return self.status == "correct"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CaptchaService:
    """Builds, verifies and blocks captcha challenges.

    The service is intentionally small: it owns the Redis layout and the
    math-captcha generation. Presentation (message text, keyboard) and
    dispatch (when to ask, which chat to answer in) belong to the middleware
    and the handler below.
    """

    def __init__(
        self,
        redis: Redis,
        users: UsersRepo,
        audit: AuditLog,
        *,
        clock: Clock = utc_now,
        rng: random.Random | None = None,
        on_passed: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._redis = redis
        self._users = users
        self._audit = audit
        self._clock = clock
        self._rng = rng or random.Random()
        # Optional callback invoked exactly once per challenge that
        # transitions to ``correct``. Used by the referrals feature to
        # settle deferred credits (antifraud refinement on Req 7.2);
        # the captcha service itself stays oblivious to that wiring.
        self._on_passed = on_passed

    def attach_on_passed(
        self, callback: Callable[[int], Awaitable[None]] | None
    ) -> None:
        """Late-bind the post-pass callback.

        Mirrors :meth:`RegistrationService.attach_crediting`: lets the
        container construct services in any order without circular
        ctor dependencies.
        """
        self._on_passed = callback

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    async def is_blocked(self, user_id: int) -> bool:
        """Return True if the user is currently in the 5-minute lockout."""
        return bool(await self._redis.exists(_BLOCK_KEY.format(user_id=user_id)))

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    async def build_challenge(self, user_id: int) -> Challenge:
        """Generate a new challenge for ``user_id`` and persist it.

        Overwrites any existing challenge for the same user — the
        middleware calls this both for the very first captcha and when a
        previous one expired (Req 11.2).
        """
        now = self._clock()
        expires_at = now + timedelta(seconds=CAPTCHA_TTL_SEC)

        a = self._rng.randint(1, 9)
        b = self._rng.randint(1, 9)
        correct = a + b
        options = self._make_options(correct)

        key = _CHALLENGE_KEY.format(user_id=user_id)
        # Replace any previous challenge wholesale so ``tries`` always
        # starts at 0 for a fresh captcha.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(
                key,
                mapping={
                    "correct": str(correct),
                    "tries": "0",
                    "expires_at": _iso(expires_at),
                },
            )
            pipe.expire(key, CAPTCHA_TTL_SEC)
            await pipe.execute()

        log.info(
            "antispam.captcha.issued",
            user_id=user_id,
            expires_at=expires_at.isoformat(),
        )
        return Challenge(
            user_id=user_id,
            a=a,
            b=b,
            correct=correct,
            options=options,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------
    async def verify_answer(self, user_id: int, answer: int) -> VerifyResult:
        """Check ``answer`` against the stored challenge.

        Outcomes:

        * ``correct`` — match: challenge is deleted, user flipped to
          ``status=active`` (lifts ``pending_captcha`` per Req 11.1/11.2).
        * ``wrong`` — mismatch: ``tries`` counter incremented. If it reached
          :data:`MAX_TRIES` the caller gets ``blocked`` instead.
        * ``blocked`` — 3rd wrong answer in this challenge. The block flag
          is set for 300 s and a warning is written to the audit log.
        * ``expired`` / ``no_challenge`` — the Redis hash vanished (TTL or
          never issued). The middleware will rebuild the challenge.
        """
        # A user already in the lockout should never reach verification,
        # but guard against racing clicks that landed just after the block.
        if await self.is_blocked(user_id):
            return VerifyResult(status="blocked")

        key = _CHALLENGE_KEY.format(user_id=user_id)
        correct_raw = await self._redis.hget(key, "correct")
        if correct_raw is None:
            return VerifyResult(status="expired")

        try:
            correct = int(correct_raw)
        except ValueError:
            # Corrupted value — treat as expired so a new challenge is issued.
            await self._redis.delete(key)
            return VerifyResult(status="expired")

        if answer == correct:
            await self._redis.delete(key)
            await self._users.set_status(
                user_id, UserStatus.active, now=self._clock()
            )
            log.info("antispam.captcha.passed", user_id=user_id)
            try:
                await self._audit.record_info(
                    event="captcha_passed",
                    actor_id=user_id,
                    target_id=user_id,
                )
            except Exception as exc:
                log.warning(
                    "antispam.captcha.audit_pass_failed",
                    user_id=user_id,
                    error=repr(exc),
                )
            if self._on_passed is not None:
                # The post-pass callback (typically referral crediting)
                # must never block the caller's response; isolate any
                # failure so a broken downstream cannot revert the
                # captcha pass the user just earned.
                try:
                    await self._on_passed(user_id)
                except Exception as exc:
                    log.warning(
                        "antispam.captcha.on_passed_failed",
                        user_id=user_id,
                        error=repr(exc),
                    )
            return VerifyResult(status="correct")

        # Wrong answer — bump the counter atomically.
        tries = int(await self._redis.hincrby(key, "tries", 1))
        if tries >= MAX_TRIES:
            await self._redis.delete(key)
            # SET (no NX) — if two racing clicks blocked at the same time
            # this just resets the 5 minute window, which is the safe side.
            await self._redis.set(
                _BLOCK_KEY.format(user_id=user_id),
                "1",
                ex=CAPTCHA_BLOCK_TTL_SEC,
            )
            await self._audit.record_warning(
                event="captcha_block",
                actor_id=user_id,
                details={"tries": tries},
            )
            log.info(
                "antispam.captcha.blocked",
                user_id=user_id,
                tries=tries,
                block_ttl=CAPTCHA_BLOCK_TTL_SEC,
            )
            return VerifyResult(status="blocked", tries=tries)

        log.debug("antispam.captcha.wrong", user_id=user_id, tries=tries)
        return VerifyResult(status="wrong", tries=tries)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_options(self, correct: int) -> tuple[int, ...]:
        """Return 4 unique integers including ``correct``.

        Distractors are chosen close to ``correct`` so they look plausible
        without ever being negative or identical to it.
        """
        values: set[int] = {correct}
        # Seed a small pool around the correct answer.
        low = max(2, correct - 5)
        high = correct + 5
        pool = [v for v in range(low, high + 1) if v != correct]
        self._rng.shuffle(pool)
        for candidate in pool:
            if len(values) >= OPTIONS_COUNT:
                break
            values.add(candidate)
        # Top up if the local pool was too small (unlikely for a,b ∈ [1..9]).
        while len(values) < OPTIONS_COUNT:
            values.add(self._rng.randint(2, 20))
        options = list(values)
        self._rng.shuffle(options)
        return tuple(options)


# ---------------------------------------------------------------------------
# Rendering helpers (used by the middleware in task 18.3)
# ---------------------------------------------------------------------------

def build_keyboard(challenge: Challenge) -> InlineKeyboardMarkup:
    """Build a 4-button inline keyboard for ``challenge``."""
    buttons = [
        InlineKeyboardButton(
            text=str(value),
            callback_data=f"{CB_PREFIX}{challenge.user_id}:{value}",
        )
        for value in challenge.options
    ]
    # Two rows of two buttons for a stable, compact layout.
    rows = [buttons[:2], buttons[2:4]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_question(
    challenge: Challenge,
    translator: Callable[..., str] | None = None,
) -> str:
    """Render the challenge prompt, optionally localised."""
    if translator is not None:
        try:
            return translator(
                "antispam.captcha.prompt",
                question=challenge.question_text,
            )
        except Exception as exc:
            log.debug("antispam.captcha.translate_failed", error=repr(exc))
    return (
        "Подтвердите, что вы не бот.\n"
        f"Сколько будет {challenge.question_text}?"
    )


# ---------------------------------------------------------------------------
# Router / handler
# ---------------------------------------------------------------------------

router = Router(name="antispam.captcha")


def _t(translator: Callable[..., str] | None, key: str, fallback: str, **kwargs: Any) -> str:
    """Translate ``key`` or return the Russian ``fallback``."""
    if translator is not None:
        try:
            value = translator(key, **kwargs)
        except Exception as exc:
            log.debug("antispam.captcha.translate_failed", key=key, error=repr(exc))
            value = key
        # ``Translator`` returns the key itself when the string is missing.
        if value != key:
            return value
    if kwargs:
        try:
            return fallback.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return fallback
    return fallback


def _parse_callback(data: str) -> tuple[int, int] | None:
    """Parse ``cap:{user_id}:{value}`` into ``(user_id, value)`` or None."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "cap":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.startswith(CB_PREFIX))
async def handle_captcha_answer(
    query: CallbackQuery,
    captcha: CaptchaService,
    **data: Any,
) -> None:
    """Process a captcha button press."""
    translator = data.get("_")
    raw = query.data or ""
    parsed = _parse_callback(raw)
    if parsed is None:
        await query.answer()
        return
    scoped_user_id, answer = parsed

    if query.from_user is None or query.from_user.id != scoped_user_id:
        # Another user trying to press someone else's captcha button.
        await query.answer(
            _t(translator, "antispam.captcha.not_yours", "Эта капча не для вас."),
            show_alert=True,
        )
        return

    result = await captcha.verify_answer(scoped_user_id, answer)

    if result.status == "correct":
        text = _t(
            translator,
            "antispam.captcha.passed",
            "✅ Капча пройдена. Можете продолжать.",
        )
        await query.answer("✅")
        if query.message is not None:
            with suppress(Exception):
                await query.message.edit_text(text)
        return

    if result.status == "blocked":
        text = _t(
            translator,
            "antispam.captcha.blocked",
            "Слишком много неверных ответов. Попробуйте снова через 5 минут.",
        )
        await query.answer(text, show_alert=True)
        if query.message is not None:
            with suppress(Exception):
                await query.message.edit_text(text)
        return

    if result.status == "wrong":
        text = _t(
            translator,
            "antispam.captcha.wrong",
            "Неверно. Попытка {tries} из {max}.",
            tries=result.tries,
            max=MAX_TRIES,
        )
        await query.answer(text, show_alert=False)
        return

    # expired / no_challenge — the middleware will regenerate on the next
    # message from the user (Req 11.2).
    text = _t(
        translator,
        "antispam.captcha.expired",
        "Капча истекла. Отправьте любое сообщение, чтобы получить новую.",
    )
    await query.answer(text, show_alert=True)
    if query.message is not None:
        with suppress(Exception):
            await query.message.edit_text(text)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()
