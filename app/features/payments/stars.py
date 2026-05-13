"""Модуль_Платежей — Telegram Stars (XTR) integration (task 25.1).

Covers the Stars side of Req 13:

* **13.1** — creates an invoice through the Telegram Bot API
  (``Bot.create_invoice_link``) and persists a ``pending`` ``Payment`` row
  before the link is returned.
* **13.3** — on ``successful_payment`` stores ``tx_hash_or_charge_id``
  (Telegram's ``telegram_payment_charge_id``) together with ``paid_at``
  and invokes every registered post-payment hook.
* **13.5** — idempotent: a ``successful_payment`` update whose
  ``telegram_payment_charge_id`` already exists is ignored, so callbacks
  run exactly once per charge.

Wiring
------
:class:`StarsPaymentsService` owns the business logic. The aiogram
:data:`router` is module-level (matching the pattern used by the other
feature modules: referrals, broadcasts, stats). Its handlers reach the
service instance via :mod:`aiogram` DI — :mod:`app.bot.register_routers`
sets ``dispatcher["stars"] = services.payments.stars`` when the service
exists, so the handlers just take a ``stars`` keyword argument.

Hooks registered via :meth:`StarsPaymentsService.register_hook` receive the
freshly-updated :class:`~app.core.db.models.Payment` row as their single
argument and run sequentially; one failing hook is logged but never blocks
subsequent hooks or the idempotency invariant. This lets different bot
features attach their own "credit the user" logic without taking a
dependency on this service.

Payload format
--------------
``payload_id`` is a UUID generated on invoice creation, persisted as
``payments.payload_id`` (a ``UNIQUE`` column) and passed into
``Bot.create_invoice_link(payload=str(payload_id))`` so Telegram echoes it
back on both the ``pre_checkout_query`` and the ``successful_payment``
updates as ``invoice_payload``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import Bot, F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

from app.core.db.models import Payment, PaymentProvider, PaymentStatus
from app.core.utils.clock import Clock, utc_now

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.core.repositories.payments import PaymentsRepo
    from app.core.services.audit import AuditLog


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables & constants
# ---------------------------------------------------------------------------

STARS_CURRENCY = "XTR"

# Pending invoices expire after 1 hour — Stars normally completes in
# seconds but we need a deterministic cutoff so the existing APScheduler
# cleanup job (task 27.x) can mark abandoned rows as ``expired``.
_PENDING_TTL = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Russian fallbacks — mirrored in app/locales/{ru,en}.yml under payments.stars.*
# ---------------------------------------------------------------------------

_MSG_PRE_CHECKOUT_ERROR = (
    "Платёж не распознан. Попробуйте создать счёт заново."
)


# ---------------------------------------------------------------------------
# Hook type
# ---------------------------------------------------------------------------

OnPaidHook = Callable[[Payment], Awaitable[None]]


@dataclass(slots=True)
class _InvoicePlan:
    """Internal: fields computed in ``create_stars_invoice`` before side effects."""

    payload_id: uuid.UUID
    title: str
    description: str
    label: str
    amount: int
    purpose: str


class StarsPaymentsService:
    """Telegram Stars (XTR) invoice generation and payment processing.

    Constructed by :mod:`app.container` when ``feature_payments=True`` and
    ``payments_provider in ("stars", "both")``. :mod:`app.bot` includes
    :data:`router` only when the instance is present.

    Args:
        bot: Shared :class:`~aiogram.Bot` — used for
            ``create_invoice_link`` and ``answer_pre_checkout_query``.
        payments_repo: Persistence layer for the ``payments`` table.
        audit: :class:`~app.core.services.audit.AuditLog` sink for error
            reporting (Req 6.2 — ``source="Модуль_Платежей"``).
        on_paid: Optional initial post-payment hook. Additional hooks can
            be added via :meth:`register_hook`.
        clock: Injectable clock so tests can freeze/shift time.
    """

    def __init__(
        self,
        bot: Bot,
        payments_repo: PaymentsRepo,
        audit: AuditLog,
        *,
        on_paid: OnPaidHook | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._bot = bot
        self._payments = payments_repo
        self._audit = audit
        self._hooks: list[OnPaidHook] = [on_paid] if on_paid is not None else []
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API — hook management
    # ------------------------------------------------------------------
    def register_hook(self, callback: OnPaidHook) -> None:
        """Append a post-payment callback.

        Callbacks are invoked in registration order on every
        ``successful_payment`` update that transitioned a ``pending`` row
        to ``paid``. A failing hook is logged via :mod:`structlog` and
        reported to the audit log but does **not** short-circuit other
        hooks.
        """
        self._hooks.append(callback)

    # ------------------------------------------------------------------
    # Public API — invoice creation
    # ------------------------------------------------------------------
    async def create_stars_invoice(
        self,
        user_id: int,
        amount: int,
        purpose: str,
        *,
        title: str,
        description: str,
        label: str | None = None,
    ) -> str:
        """Persist a ``pending`` Stars payment and return an invoice link.

        Requirement 13.1. The ``payload_id`` UUID is generated first,
        persisted via :meth:`PaymentsRepo.create_pending` and then passed
        into ``Bot.create_invoice_link(payload=str(payload_id))`` so the
        inbound ``pre_checkout_query`` and ``successful_payment`` updates
        can be matched back to this row.

        Args:
            user_id: The invoice recipient's ``telegram_id``.
            amount: Amount in XTR (Telegram Stars). Must be ``>= 1``.
            purpose: Short business identifier stored as ``payments.purpose``.
            title: Invoice title shown to the user in the Telegram UI
                (1..32 chars per Bot API).
            description: Invoice description shown to the user
                (1..255 chars per Bot API).
            label: Optional ``LabeledPrice.label`` override. Defaults to
                ``purpose`` when omitted so the visible line item always
                has a non-empty label.

        Returns:
            The URL returned by ``Bot.create_invoice_link`` — callers
            typically render it as a deeplink button.

        Raises:
            ValueError: When ``amount < 1``.
        """
        if amount < 1:
            raise ValueError("Stars invoice amount must be >= 1 XTR")

        plan = _InvoicePlan(
            payload_id=uuid.uuid4(),
            title=title,
            description=description,
            label=label if label else purpose,
            amount=amount,
            purpose=purpose,
        )

        now = self._clock()
        await self._payments.create_pending(
            user_id=user_id,
            provider=PaymentProvider.stars,
            amount=plan.amount,
            currency=STARS_CURRENCY,
            purpose=plan.purpose,
            now=now,
            expires_at=now + _PENDING_TTL,
            payload_id=plan.payload_id,
        )

        try:
            link = await self._bot.create_invoice_link(
                title=plan.title,
                description=plan.description,
                payload=str(plan.payload_id),
                currency=STARS_CURRENCY,
                prices=[LabeledPrice(label=plan.label, amount=plan.amount)],
            )
        except Exception as exc:
            log.error(
                "payments.stars.create_invoice_failed",
                user_id=user_id,
                payload_id=str(plan.payload_id),
                amount=plan.amount,
                error=repr(exc),
            )
            # Best-effort audit entry; never mask the original error.
            try:
                await self._audit.record_error(
                    source="Модуль_Платежей",
                    message=(
                        f"stars.create_invoice_link failed for user={user_id}:"
                        f" {exc!r}"
                    ),
                    actor_id=user_id,
                )
            except Exception as audit_exc:
                log.warning(
                    "payments.stars.audit_failed", error=repr(audit_exc)
                )
            raise

        log.info(
            "payments.stars.invoice_created",
            user_id=user_id,
            payload_id=str(plan.payload_id),
            amount=plan.amount,
            purpose=plan.purpose,
        )
        return link

    # ------------------------------------------------------------------
    # Update processing — invoked by the module-level router handlers
    # ------------------------------------------------------------------
    async def process_pre_checkout_query(self, query: PreCheckoutQuery) -> None:
        """Respond to ``pre_checkout_query`` — basic payload validation.

        The check is intentionally minimal per spec: parse the payload as
        a UUID and confirm a matching ``pending`` Stars row exists. That
        keeps the handler fast (Telegram requires a response within ~10 s)
        and delegates deeper verification to the ``successful_payment``
        path where we also have the ``telegram_payment_charge_id``.
        """
        payload_raw = (query.invoice_payload or "").strip()
        ok, reason = await self._validate_pre_checkout_payload(payload_raw)
        if ok:
            try:
                await self._bot.answer_pre_checkout_query(query.id, ok=True)
            except Exception as exc:
                log.error(
                    "payments.stars.pre_checkout_answer_failed",
                    payload=payload_raw[:64],
                    error=repr(exc),
                )
            else:
                log.info(
                    "payments.stars.pre_checkout_ok", payload=payload_raw
                )
            return

        # Denial path — respond with ``ok=False`` and a user-facing error
        # message. Telegram shows this message to the user.
        log.warning(
            "payments.stars.pre_checkout_denied",
            payload=payload_raw[:64],
            reason=reason,
        )
        try:
            await self._bot.answer_pre_checkout_query(
                query.id,
                ok=False,
                error_message=_MSG_PRE_CHECKOUT_ERROR,
            )
        except Exception as exc:
            log.error(
                "payments.stars.pre_checkout_answer_failed",
                payload=payload_raw[:64],
                error=repr(exc),
            )

    async def process_successful_payment(self, message: Message) -> None:
        """Process ``message.successful_payment`` — persist + fire hooks.

        Idempotent (Req 13.5):

        1. If a row with the same ``(stars, telegram_payment_charge_id)``
           already exists we return immediately — the hooks have already
           fired for this charge.
        2. Otherwise call :meth:`PaymentsRepo.mark_paid` which flips the
           ``pending`` row to ``paid`` and stamps
           ``tx_hash_or_charge_id`` + ``paid_at``. ``mark_paid`` itself
           short-circuits if the row is already in a terminal state.
        3. Re-read the row and invoke each registered hook with it.
        """
        sp = message.successful_payment
        if sp is None:  # pragma: no cover - defensive: F filter guards this
            return
        if sp.currency != STARS_CURRENCY:
            # Not a Stars payment — let the TON handler (task 26.1) deal
            # with it. Leaving the update unclaimed matches aiogram's
            # usual "one handler wins" semantics.
            return

        charge_id = sp.telegram_payment_charge_id
        payload_raw = (sp.invoice_payload or "").strip()
        actor_id = message.from_user.id if message.from_user else None

        if not charge_id:
            log.error(
                "payments.stars.successful_payment_missing_charge_id",
                payload=payload_raw[:64],
                actor_id=actor_id,
            )
            return

        # (1) Idempotency gate.
        try:
            already = await self._payments.exists_by_charge_id(
                PaymentProvider.stars, charge_id
            )
        except Exception as exc:
            log.error(
                "payments.stars.exists_check_failed",
                charge_id=charge_id,
                error=repr(exc),
            )
            await self._audit_error(
                f"stars.exists_by_charge_id failed: {exc!r}", actor_id
            )
            return
        if already:
            log.info(
                "payments.stars.duplicate_charge_ignored",
                charge_id=charge_id,
                payload=payload_raw[:64],
            )
            return

        # (2) Parse payload and flip the row.
        try:
            payload_id = uuid.UUID(payload_raw)
        except (ValueError, AttributeError):
            log.error(
                "payments.stars.bad_invoice_payload",
                payload=payload_raw[:64],
                charge_id=charge_id,
                actor_id=actor_id,
            )
            await self._audit_error(
                f"stars.successful_payment with unparseable payload="
                f"{payload_raw!r} charge={charge_id!r}",
                actor_id,
            )
            return

        now = self._clock()
        try:
            updated = await self._payments.mark_paid(
                payload_id=payload_id,
                charge_id=charge_id,
                paid_at=now,
            )
        except Exception as exc:
            log.error(
                "payments.stars.mark_paid_failed",
                payload_id=str(payload_id),
                charge_id=charge_id,
                error=repr(exc),
            )
            await self._audit_error(
                f"stars.mark_paid failed for payload={payload_id}"
                f" charge={charge_id}: {exc!r}",
                actor_id,
            )
            return

        if not updated:
            # Row either missing or already in a terminal state — treat
            # the same as the idempotency short-circuit above.
            log.info(
                "payments.stars.mark_paid_noop",
                payload_id=str(payload_id),
                charge_id=charge_id,
            )
            return

        # (3) Re-read so hooks get a fresh Payment row.
        try:
            payment = await self._payments.find_by_payload_id(payload_id)
        except Exception as exc:
            log.error(
                "payments.stars.find_by_payload_failed",
                payload_id=str(payload_id),
                error=repr(exc),
            )
            payment = None

        if payment is None:
            log.error(
                "payments.stars.payment_missing_after_mark_paid",
                payload_id=str(payload_id),
                charge_id=charge_id,
            )
            await self._audit_error(
                f"stars.payment vanished after mark_paid payload={payload_id}",
                actor_id,
            )
            return

        log.info(
            "payments.stars.paid",
            payload_id=str(payload_id),
            charge_id=charge_id,
            user_id=payment.user_id,
            amount=payment.amount,
            purpose=payment.purpose,
        )

        await self._invoke_hooks(payment)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _validate_pre_checkout_payload(
        self, payload_raw: str
    ) -> tuple[bool, str | None]:
        """Best-effort validation of an inbound ``invoice_payload``.

        Returns ``(True, None)`` when the payload is a UUID pointing at a
        ``pending`` Stars row; ``(False, reason)`` otherwise. Transient
        DB errors are treated as "deny" so Telegram does not charge a
        user on an unverified row.
        """
        if not payload_raw:
            return False, "empty_payload"
        try:
            payload_id = uuid.UUID(payload_raw)
        except ValueError:
            return False, "malformed_payload"

        try:
            payment = await self._payments.find_by_payload_id(payload_id)
        except Exception as exc:
            log.error(
                "payments.stars.find_by_payload_failed",
                payload_id=str(payload_id),
                error=repr(exc),
            )
            await self._audit_error(
                f"stars.find_by_payload_id failed for {payload_id}: {exc!r}",
                None,
            )
            return False, "lookup_failed"

        if payment is None:
            return False, "unknown_payload"
        if payment.provider != PaymentProvider.stars:
            return False, "provider_mismatch"
        if payment.status != PaymentStatus.pending:
            return False, f"status_{payment.status.value}"
        return True, None

    async def _invoke_hooks(self, payment: Payment) -> None:
        for hook in list(self._hooks):
            try:
                await hook(payment)
            except Exception as exc:
                log.error(
                    "payments.stars.hook_failed",
                    hook=getattr(hook, "__qualname__", repr(hook)),
                    payload_id=str(payment.payload_id),
                    error=repr(exc),
                )
                await self._audit_error(
                    f"stars.on_paid hook {hook!r} failed for"
                    f" payload={payment.payload_id}: {exc!r}",
                    payment.user_id,
                )

    async def _audit_error(self, message: str, actor_id: int | None) -> None:
        try:
            await self._audit.record_error(
                source="Модуль_Платежей",
                message=message,
                actor_id=actor_id,
            )
        except Exception as exc:
            log.warning("payments.stars.audit_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# Module-level router — handlers resolve the service via aiogram DI
# ---------------------------------------------------------------------------

router: Router = Router(name="payments.stars")


@router.pre_checkout_query()
async def handle_pre_checkout_query(
    query: PreCheckoutQuery,
    stars: StarsPaymentsService | None = None,
    **_data: Any,
) -> None:
    """Module-level entry point for ``pre_checkout_query`` updates.

    ``stars`` is populated by aiogram DI from ``dispatcher["stars"]``
    which :func:`app.bot.register_routers` sets when the service exists.
    The defensive ``None`` branch keeps the handler safe if the router
    gets registered without the bundle (e.g. misconfigured wiring). In
    that case we deny the query so Telegram does not charge the user on
    an unverified invoice.
    """
    if stars is None:
        log.warning("payments.stars.handler.no_service")
        try:
            await query.bot.answer_pre_checkout_query(
                query.id,
                ok=False,
                error_message=_MSG_PRE_CHECKOUT_ERROR,
            )
        except Exception as exc:
            log.error(
                "payments.stars.pre_checkout_answer_failed",
                error=repr(exc),
            )
        return
    await stars.process_pre_checkout_query(query)


@router.message(F.successful_payment)
async def handle_successful_payment(
    message: Message,
    stars: StarsPaymentsService | None = None,
    **_data: Any,
) -> None:
    """Module-level entry point for ``successful_payment`` updates.

    The ``F.successful_payment`` filter restricts the handler to messages
    whose ``successful_payment`` field is set — i.e. the Telegram
    payment-completed notification.
    """
    if stars is None:
        log.warning(
            "payments.stars.handler.no_service",
            has_successful_payment=True,
        )
        return
    await stars.process_successful_payment(message)


__all__ = [
    "STARS_CURRENCY",
    "OnPaidHook",
    "StarsPaymentsService",
    "router",
]
