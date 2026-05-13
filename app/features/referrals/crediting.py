"""Реферальная_Система — eligibility-aware referral crediting.

Antifraud requirement (refinement on Req 7.2): an inviter's counter must
only grow when the invitee is a "real" user — meaning they have

* passed the captcha (when the antispam feature is on), and
* subscribed to every required channel (when the subscriptions feature
  is on).

When neither feature is enabled the previous "credit on ``/start``"
behaviour applies and the call from
:class:`~app.core.services.registration.RegistrationService` settles
the credit immediately. With either feature on we wait for the matching
trigger (captcha pass / subscription confirmation) and call
:meth:`ReferralCreditingService.try_credit` from there.

The actual database update is exactly-once: the persistence layer uses
:meth:`UsersRepo.try_credit_referral` which stamps ``users.referral_credited_at``
in the same transaction as the inviter's counter bump, so concurrent
callers cannot double-credit the same invitee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from app.core.db.models import User, UserStatus
from app.core.utils.clock import Clock, utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.repositories.users import UsersRepo
    from app.core.services.audit import AuditLog
    from app.features.subscriptions.checker import SubscriptionChecker


log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreditOutcome:
    """Result of a :meth:`ReferralCreditingService.try_credit` call.

    Attributes:
        credited: ``True`` iff the inviter's counter was incremented in
            this call. Always ``False`` for repeat invocations.
        reason: Short tag explaining why no credit was applied. ``None``
            when ``credited`` is ``True``. Used for structured logs and
            (eventually) tests.
        inviter_id: ``telegram_id`` of the user who got the credit, when
            ``credited`` is ``True``.
    """

    credited: bool
    reason: str | None = None
    inviter_id: int | None = None


class ReferralCreditingService:
    """Coordinator that decides *when* to bump the inviter's counter.

    The persistence layer enforces exactly-once via a unique-stamp on
    ``users.referral_credited_at``; this service handles the *gating*
    logic — captcha + subscriptions.

    Args:
        users: Repository over the ``users`` table.
        audit: Audit sink. Crediting failures are logged here so an
            admin can reconcile manually if a downstream call ever
            crashes after the DB row has been stamped.
        subs_checker: Optional subscription checker. When provided, a
            non-empty ``MissingChannel`` list short-circuits the credit.
            Pass ``None`` when the subscriptions feature is disabled.
        require_captcha: ``True`` when the antispam feature is on. Forces
            ``user.status == active`` (i.e. captcha is no longer pending).
            ``False`` skips the captcha gate entirely.
        clock: Injectable clock for tests.
    """

    def __init__(
        self,
        users: UsersRepo,
        audit: AuditLog,
        *,
        subs_checker: SubscriptionChecker | None = None,
        require_captcha: bool = False,
        clock: Clock = utc_now,
    ) -> None:
        self._users = users
        self._audit = audit
        self._subs_checker = subs_checker
        self._require_captcha = require_captcha
        self._clock = clock

    @property
    def gates_active(self) -> bool:
        """Whether either gate is configured.

        When ``False`` the credit can be settled the moment a referrer
        is recorded — that is the "only on /start" behaviour the user
        asked for.
        """
        return self._require_captcha or self._subs_checker is not None

    async def try_credit(self, invitee_id: int) -> CreditOutcome:
        """Attempt to credit the invitee's referrer.

        Idempotent across calls: once the credit has been applied,
        subsequent invocations short-circuit on ``referral_credited_at``
        and return ``credited=False, reason="already_credited"``.
        """
        try:
            invitee = await self._users.get_by_tg_id(invitee_id)
        except Exception as exc:
            await self._audit_error(
                f"referrals.try_credit.lookup_failed for {invitee_id}: {exc!r}",
                invitee_id,
            )
            log.warning(
                "referrals.try_credit.lookup_failed",
                invitee_id=invitee_id,
                error=repr(exc),
            )
            return CreditOutcome(credited=False, reason="lookup_failed")

        if invitee is None:
            return CreditOutcome(credited=False, reason="invitee_missing")
        if invitee.referrer_id is None:
            return CreditOutcome(credited=False, reason="no_referrer")
        if invitee.referral_credited_at is not None:
            return CreditOutcome(credited=False, reason="already_credited")

        gate_failure = self._gates_blocking_for(invitee)
        if gate_failure is not None:
            return CreditOutcome(credited=False, reason=gate_failure)

        if self._subs_checker is not None:
            try:
                missing = await self._subs_checker.check(invitee_id)
            except Exception as exc:
                await self._audit_error(
                    f"referrals.try_credit.subs_check_failed for {invitee_id}: {exc!r}",
                    invitee_id,
                )
                log.warning(
                    "referrals.try_credit.subs_check_failed",
                    invitee_id=invitee_id,
                    error=repr(exc),
                )
                return CreditOutcome(credited=False, reason="subs_check_failed")
            if missing:
                return CreditOutcome(credited=False, reason="missing_subscriptions")

        now = self._clock()
        try:
            inviter_id = await self._users.try_credit_referral(invitee_id, now)
        except Exception as exc:
            await self._audit_error(
                f"referrals.try_credit.persist_failed for {invitee_id}: {exc!r}",
                invitee_id,
            )
            log.warning(
                "referrals.try_credit.persist_failed",
                invitee_id=invitee_id,
                error=repr(exc),
            )
            return CreditOutcome(credited=False, reason="persist_failed")

        if inviter_id is None:
            return CreditOutcome(credited=False, reason="already_credited")

        log.info(
            "referrals.try_credit.credited",
            invitee_id=invitee_id,
            inviter_id=inviter_id,
        )
        return CreditOutcome(credited=True, inviter_id=inviter_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _gates_blocking_for(self, invitee: User) -> str | None:
        """Return a short tag when a gate forbids crediting, else ``None``."""
        if self._require_captcha and invitee.status != UserStatus.active:
            return "captcha_pending"
        return None

    async def _audit_error(self, message: str, actor_id: int | None) -> None:
        try:
            await self._audit.record_error(
                source="Реферальная_Система",
                message=message,
                actor_id=actor_id,
            )
        except Exception as exc:
            log.warning("referrals.try_credit.audit_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# Convenience helper for callers that only have the bundle handy
# ---------------------------------------------------------------------------


async def maybe_credit(service: Any | None, invitee_id: int) -> CreditOutcome:
    """Call ``service.try_credit`` if the service is wired in.

    Trigger sites (registration / captcha pass / subscription recheck)
    pull the bundle from ``dispatcher["referrals"]`` which may be
    ``None`` when the feature is disabled. This helper centralises the
    "skip when missing" branch so the trigger sites stay readable.
    """
    if service is None:
        return CreditOutcome(credited=False, reason="feature_off")
    return await service.try_credit(invitee_id)


__all__ = [
    "CreditOutcome",
    "ReferralCreditingService",
    "maybe_credit",
]
