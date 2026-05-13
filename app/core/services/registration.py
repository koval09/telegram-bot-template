"""Модуль_Регистрации — user bookkeeping on every update.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.7, 1.8, 7.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from aiogram.types import User as TgUser

from app.core.db.models import User
from app.core.repositories.users import TgUserData, UsersRepo
from app.core.services.audit import AuditLog
from app.core.services.retry import (
    RetryExhausted,
    db_retryable_exceptions,
    with_retry,
)
from app.core.utils.clock import Clock, utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.features.referrals.crediting import ReferralCreditingService

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user: User
    created: bool
    db_error: bool = False


def _to_tg_data(tg_user: TgUser) -> TgUserData:
    return TgUserData(
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        language_code=tg_user.language_code,
    )


def _parse_referrer_id(start_arg: str | None) -> int | None:
    """Extract ``ref_<int>`` from a /start argument."""
    if not start_arg:
        return None
    # aiogram strips the '/start ' prefix already; we might also get the full
    # payload from deeplinks. Accept both 'ref_123' and just the digits if the
    # app is configured to drop the prefix.
    if start_arg.startswith("ref_"):
        start_arg = start_arg[4:]
    if start_arg.isdigit():
        try:
            return int(start_arg)
        except ValueError:
            return None
    return None


class RegistrationService:
    """Upsert Telegram users and apply referral bookkeeping on /start."""

    def __init__(
        self,
        users: UsersRepo,
        audit: AuditLog,
        *,
        clock: Clock = utc_now,
        crediting: ReferralCreditingService | None = None,
    ) -> None:
        self._users = users
        self._audit = audit
        self._clock = clock
        # Optional crediting service (Реферальная_Система). When ``None``
        # the registration service falls back to the legacy "no credit
        # here" behaviour and downstream triggers (captcha pass /
        # subscription recheck) are responsible for the call.
        self._crediting = crediting

    def attach_crediting(
        self, crediting: ReferralCreditingService | None
    ) -> None:
        """Late-bind the crediting service after the container is built.

        ``ReferralCreditingService`` may need a ``SubscriptionChecker``
        instance that is itself constructed alongside the registration
        service in :func:`app.container.build_services`. Allowing a late
        ``attach_crediting`` keeps the construction order flexible
        without breaking the public ctor.
        """
        self._crediting = crediting

    async def ensure_user(
        self,
        tg_user: TgUser,
        *,
        start_arg: str | None = None,
    ) -> RegistrationResult:
        now = self._clock()
        try:
            user, created = await with_retry(
                lambda: self._users.upsert_from_tg(_to_tg_data(tg_user), now=now),
                attempts=3,
                delays=(1.0, 1.0, 1.0),
                retry_on=db_retryable_exceptions(),
                op_name="registration.upsert",
            )
        except RetryExhausted as exc:
            log.error("registration.upsert_failed", telegram_id=tg_user.id, error=repr(exc))
            await self._audit.record_error(
                source="База_Данных",
                message=f"registration.upsert failed for {tg_user.id}: {exc.last_error!r}",
                actor_id=tg_user.id,
                target_id=tg_user.id,
                now=now,
            )
            return RegistrationResult(user=_sentinel_user(tg_user, now), created=False, db_error=True)

        if created:
            # Audit-log "user joined" once per real user. Username + name
            # are captured here so the journal renders nicely without
            # fetching the row again.
            await self._audit.record_info(
                event="user_joined",
                actor_id=tg_user.id,
                target_id=tg_user.id,
                details={
                    "username": tg_user.username,
                    "name": (tg_user.first_name or tg_user.last_name or ""),
                    "language": tg_user.language_code,
                },
                now=now,
            )

            ref_id = _parse_referrer_id(start_arg)
            if ref_id is not None and ref_id != tg_user.id:
                # Only honour the referrer if it points to a real user (Req 1.8).
                referrer = await self._users.get_by_tg_id(ref_id)
                if referrer is not None:
                    saved = await self._users.set_referrer(tg_user.id, ref_id)
                    if saved:
                        # Settle the credit on /start ONLY when neither
                        # gate (captcha, subscriptions) is enabled. With
                        # any gate active the credit is deferred to the
                        # post-gate trigger (captcha pass / subscription
                        # recheck), which call ``try_credit`` themselves.
                        # Calling it here would race the captcha
                        # middleware which has not yet flipped the status
                        # to ``pending_captcha`` — see Req 7.2 antifraud
                        # refinement.
                        if (
                            self._crediting is not None
                            and not self._crediting.gates_active
                        ):
                            try:
                                outcome = await self._crediting.try_credit(
                                    tg_user.id
                                )
                            except Exception as exc:
                                log.warning(
                                    "registration.try_credit_failed",
                                    telegram_id=tg_user.id,
                                    referrer_id=ref_id,
                                    error=repr(exc),
                                )
                            else:
                                if outcome.credited:
                                    log.info(
                                        "registration.credit_settled_on_start",
                                        telegram_id=tg_user.id,
                                        inviter_id=outcome.inviter_id,
                                    )
                                else:
                                    log.debug(
                                        "registration.credit_skipped_on_start",
                                        telegram_id=tg_user.id,
                                        referrer_id=ref_id,
                                        reason=outcome.reason,
                                    )
                        else:
                            log.debug(
                                "registration.credit_deferred_to_gates",
                                telegram_id=tg_user.id,
                                referrer_id=ref_id,
                            )
                        user.referrer_id = ref_id

        return RegistrationResult(user=user, created=created)


def _sentinel_user(tg_user: TgUser, now: datetime) -> User:
    """Build an in-memory ``User`` placeholder when the DB is unreachable.

    The bot still needs a ``user`` object in the middleware chain to produce
    a localized response; everything downstream must tolerate the
    ``db_error`` flag on the result.
    """
    return User(
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        language_code=tg_user.language_code,
        created_at=now,
        last_seen_at=now,
    )
