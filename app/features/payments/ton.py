"""Модуль_Платежей — TON integration (task 26.1).

Covers the bot-side of Req 13.2 / 13.3 for native TON payments:

* :meth:`TonPaymentsService.create_ton_payment` — creates a ``pending``
  :class:`~app.core.db.models.Payment` row and asks the user's already-
  connected TON wallet (via :class:`~app.ton.connector.TonConnector`) to
  sign a transfer to ``settings.ton_receive_address`` carrying the
  ``payload_id`` UUID as a text comment. The payload identifier is the
  same value that :class:`app.features.payments.ton_api.TonApiClient`
  later uses to find the confirming transaction on-chain (task 26.2).
* Hook registry (``on_paid`` / ``on_expired`` / ``on_mismatch``) — the
  polling job (task 26.2) invokes these once per terminal state
  transition so other features can credit the user / cancel the invoice.

This module *only* exposes the service and its data types. The
APScheduler polling job is deliberately not wired here — task 26.2
consumes :class:`TonApiClient.find_by_payload` from a separate entry
point.

The Stars counterpart lives in :mod:`app.features.payments.stars`; both
services share :class:`app.core.repositories.payments.PaymentsRepo` and
the :class:`~app.core.services.audit.AuditLog` sink.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog

from app.core.db.models import Payment, PaymentProvider
from app.core.utils.clock import Clock, utc_now

if TYPE_CHECKING:  # pragma: no cover — typing only
    from aiogram import Bot
    from redis.asyncio import Redis

    from app.config import Settings
    from app.core.repositories.payments import PaymentsRepo
    from app.core.repositories.users import UsersRepo
    from app.core.services.audit import AuditLog
    from app.features.payments.ton_api import TonApiClient
    from app.ton.connector import TonConnector


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TON_CURRENCY = "TON"

# Req 13.4 — pending payments that do not confirm within 15 minutes are
# marked ``expired`` by the polling job. This is also the window we pass
# to ``TonApiClient.get_incoming_transactions(after=now-PENDING_TTL)``.
PENDING_TTL = timedelta(minutes=15)

# Hook kinds — kept as module constants so callers using
# ``register_hook(kind, cb)`` don't have to import literals.
HookKind = Literal["on_paid", "on_expired", "on_mismatch"]
_ALLOWED_HOOK_KINDS: frozenset[str] = frozenset(
    {"on_paid", "on_expired", "on_mismatch"}
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WalletNotConnectedError(Exception):
    """Raised by :meth:`TonPaymentsService.create_ton_payment`.

    Signals that the user has not completed ``/connect_wallet`` yet and
    therefore cannot sign a TON transfer. Handlers catch this and prompt
    the user to run ``/connect_wallet`` first — see task 15 for the
    connection flow. The service performs *no* fallback: spending
    without the user's explicit wallet binding would violate Req 3.6
    (one active wallet per user, bound on-chain through proof).
    """


# ---------------------------------------------------------------------------
# Hook type & data
# ---------------------------------------------------------------------------

Hook = Callable[[Payment], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    """Return value of :meth:`TonPaymentsService.create_ton_payment`.

    Held together so handlers get everything they need in one place:
    the ``payload_id`` to show in the UI (and reference later in the
    audit log), the DB ``payment_db_id`` for ``/buy`` status lookups,
    the ``expires_at`` cutoff so the UI can display a countdown, plus
    any optional deeplink/QR the TON Connect SDK handed back (the modern
    SDK returns ``None`` — wallet signs in-place — but tests and older
    wallets may surface a link the user can paste into a browser).

    Attributes:
        payload_id: Bot-side correlation UUID, echoed on-chain as the
            text comment of the transfer.
        payment_db_id: Primary key of the ``payments`` row created by
            this call (useful for admin tooling / ``/buy`` status).
        expires_at: UTC cutoff after which the polling job marks the
            payment ``expired`` (Req 13.4).
        amount_nano: Expected transfer value in nanoTON. Stored here
            (not only in the DB row) so callers can show it to the user
            without re-fetching.
        to_address: Destination address — always
            ``settings.ton_receive_address``.
        deeplink: Optional wallet deeplink returned by the SDK. ``None``
            when the wallet signs without surfacing one (typical).
        qr_base64: Optional base64 PNG QR encoding ``deeplink``. Same
            caveat as above — ``None`` when the SDK doesn't provide one.
    """

    payload_id: uuid.UUID
    payment_db_id: int
    expires_at: datetime
    amount_nano: int
    to_address: str
    deeplink: str | None = None
    qr_base64: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TonPaymentsService:
    """TON Connect payments service — invoice creation + hook registry.

    Instantiated by :mod:`app.container` when ``feature_payments=True``
    and ``payments_provider in ("ton", "both")``. The companion
    :class:`app.features.payments.ton_api.TonApiClient` is passed to the
    scheduler job (task 26.2); the service itself does not poll.

    Hooks are grouped by kind so the polling job can dispatch terminal
    state transitions to the right callbacks — Req 13.3 (``on_paid``),
    Req 13.4 (``on_expired``), and the ``mismatch`` branch of the TON
    amount check (``on_mismatch``).

    Args:
        bot: Shared :class:`~aiogram.Bot` — reserved for future
            notifications (e.g. pre-signing prompt). Stored but unused
            in this task's scope.
        redis: Redis client. Not used directly here; kept on the
            constructor signature so task 26.2's poller can share state
            through the same service.
        users_repo: Used to verify the user has a bound TON wallet before
            creating an invoice.
        payments_repo: Persistence layer for the ``payments`` table.
        audit: :class:`~app.core.services.audit.AuditLog` sink.
        ton_connector: The :class:`~app.ton.connector.TonConnector`
            instance — used here only for :meth:`TonConnector.send_transaction`.
        ton_api_client: Thin TonCenter wrapper. Stored so the service
            can expose it to the scheduler job via ``.api_client``.
        settings: Runtime configuration — ``ton_receive_address`` and
            ``ton_api_url`` are mandatory when TON payments are on
            (already validated in :class:`~app.config.Settings`).
        on_paid / on_expired / on_mismatch: Optional initial hooks.
            Additional hooks can be added via :meth:`register_hook`.
        pending_ttl: Override :data:`PENDING_TTL` (tests).
        clock: Injectable clock.
    """

    def __init__(
        self,
        bot: Bot,
        redis: Redis,
        users_repo: UsersRepo,
        payments_repo: PaymentsRepo,
        audit: AuditLog,
        ton_connector: TonConnector,
        ton_api_client: TonApiClient,
        settings: Settings,
        *,
        on_paid: Hook | None = None,
        on_expired: Hook | None = None,
        on_mismatch: Hook | None = None,
        pending_ttl: timedelta = PENDING_TTL,
        clock: Clock = utc_now,
    ) -> None:
        if not getattr(settings, "ton_receive_address", None):
            raise ValueError(
                "TonPaymentsService requires settings.ton_receive_address"
            )
        self._bot = bot
        self._redis = redis
        self._users_repo = users_repo
        self._payments = payments_repo
        self._audit = audit
        self._ton_connector = ton_connector
        self._api_client = ton_api_client
        self._settings = settings
        self._pending_ttl = pending_ttl
        self._clock = clock

        self._hooks: dict[str, list[Hook]] = {
            "on_paid": [on_paid] if on_paid is not None else [],
            "on_expired": [on_expired] if on_expired is not None else [],
            "on_mismatch": [on_mismatch] if on_mismatch is not None else [],
        }

    # ------------------------------------------------------------------
    # Accessors (used by the scheduler job — task 26.2)
    # ------------------------------------------------------------------
    @property
    def api_client(self) -> TonApiClient:
        """Return the bundled :class:`TonApiClient`.

        Task 26.2's poller reaches it via ``services.payments.ton.api_client``
        so it can call :meth:`TonApiClient.find_by_payload` without a
        second DI field.
        """
        return self._api_client

    @property
    def pending_ttl(self) -> timedelta:
        """Return the pending-payment TTL (Req 13.4)."""
        return self._pending_ttl

    # ------------------------------------------------------------------
    # Hook registry
    # ------------------------------------------------------------------
    def register_hook(self, kind: HookKind, callback: Hook) -> None:
        """Register a terminal-state callback.

        ``kind`` picks the stage: ``on_paid`` (Req 13.3), ``on_expired``
        (Req 13.4), or ``on_mismatch`` (amount does not match the
        expected value). Hooks are invoked in registration order by the
        polling job — one failing hook is logged via :mod:`structlog`
        and reported to the audit log but does not block other hooks.
        """
        if kind not in _ALLOWED_HOOK_KINDS:
            raise ValueError(
                f"unknown hook kind {kind!r}; expected one of {sorted(_ALLOWED_HOOK_KINDS)}"
            )
        self._hooks[kind].append(callback)

    def hooks(self, kind: HookKind) -> list[Hook]:
        """Return a shallow copy of the hooks registered for ``kind``.

        Task 26.2's poller iterates this list when a payment transitions;
        returning a copy keeps the registry immutable from the caller's
        side.
        """
        if kind not in _ALLOWED_HOOK_KINDS:
            raise ValueError(f"unknown hook kind {kind!r}")
        return list(self._hooks[kind])

    async def invoke_hooks(self, kind: HookKind, payment: Payment) -> None:
        """Invoke every hook of ``kind`` with ``payment``.

        Shared between this service and the scheduler job — centralised
        so error handling / audit reporting is consistent. Each hook is
        awaited; failures are isolated and never stop later hooks.
        """
        for hook in self.hooks(kind):
            try:
                await hook(payment)
            except Exception as exc:
                log.error(
                    "payments.ton.hook_failed",
                    kind=kind,
                    hook=getattr(hook, "__qualname__", repr(hook)),
                    payload_id=str(payment.payload_id),
                    error=repr(exc),
                )
                await self._audit_error(
                    f"ton.{kind} hook {hook!r} failed for"
                    f" payload={payment.payload_id}: {exc!r}",
                    payment.user_id,
                )

    # ------------------------------------------------------------------
    # create_ton_payment — Req 13.2
    # ------------------------------------------------------------------
    async def create_ton_payment(
        self,
        user: Any,
        amount: int,
        purpose: str,
    ) -> PaymentIntent:
        """Create a pending TON payment and ask the wallet to sign.

        Steps:

        1. Verify ``user.ton_address`` is set; otherwise raise
           :class:`WalletNotConnectedError`.
        2. Pre-allocate ``payload_id`` (UUID) so the DB row and the
           on-chain text comment share the same identifier.
        3. Insert a :class:`~app.core.db.models.Payment` row with
           ``provider=ton``, ``status=pending``, ``amount=amount_nano``,
           ``currency="TON"``, ``expires_at=now + PENDING_TTL``.
        4. Call :meth:`TonConnector.send_transaction` to surface the
           signing request to the user's wallet with ``payload`` set to
           ``str(payload_id)`` (encoded as a text comment cell inside
           the connector wrapper).

        Args:
            user: Either an :class:`~app.core.db.models.User` row or any
                object exposing ``telegram_id`` and ``ton_address``.
                The permissive type lets handlers pass the already-loaded
                user from middleware without a re-fetch.
            amount: Transfer value in **nanoTON** (1 TON = 1e9 nanoTON).
                Must be ``>= 1``.
            purpose: Short business identifier stored as
                ``payments.purpose`` (``String(64)``).

        Returns:
            A :class:`PaymentIntent` with the ``payload_id``, DB row id,
            expiry, and any deeplink/QR the SDK returned.

        Raises:
            WalletNotConnectedError: User has no bound TON wallet.
            ValueError: ``amount < 1``.
        """
        if amount < 1:
            raise ValueError("TON invoice amount must be >= 1 nanoTON")

        telegram_id = int(user.telegram_id)
        ton_address = getattr(user, "ton_address", None)
        if not ton_address:
            raise WalletNotConnectedError(
                f"telegram_id={telegram_id} has no connected TON wallet"
            )

        payload_id = uuid.uuid4()
        now = self._clock()
        expires_at = now + self._pending_ttl
        to_address = str(self._settings.ton_receive_address)

        payment = await self._payments.create_pending(
            user_id=telegram_id,
            provider=PaymentProvider.ton,
            amount=amount,
            currency=TON_CURRENCY,
            purpose=purpose,
            now=now,
            expires_at=expires_at,
            payload_id=payload_id,
        )

        # Surface the signing request. Failures here are logged + audited
        # but leave the pending row in place — the poller will expire it
        # after PENDING_TTL if the user never signs (Req 13.4).
        deeplink: str | None = None
        qr_base64: str | None = None
        try:
            result = await self._ton_connector.send_transaction(
                telegram_id,
                to=to_address,
                amount_nano=amount,
                payload=str(payload_id),
                valid_until=int(expires_at.timestamp()),
            )
        except Exception as exc:
            log.error(
                "payments.ton.send_transaction_failed",
                telegram_id=telegram_id,
                payload_id=str(payload_id),
                amount=amount,
                error=repr(exc),
            )
            await self._audit_error(
                f"ton.send_transaction failed for user={telegram_id}"
                f" payload={payload_id} amount={amount}: {exc!r}",
                telegram_id,
            )
            # Re-raise so the caller can present a user-facing error and
            # the pending row will be expired by the poller. No rollback
            # of the DB row — task 26.2 handles abandoned pendings.
            raise
        else:
            # Best-effort extract of deeplink / QR if the SDK returned them.
            deeplink, qr_base64 = _extract_deeplink(result)

        log.info(
            "payments.ton.invoice_created",
            telegram_id=telegram_id,
            payload_id=str(payload_id),
            payment_db_id=payment.id,
            amount_nano=amount,
            purpose=purpose,
            to=to_address,
            expires_at=expires_at.isoformat(),
        )

        return PaymentIntent(
            payload_id=payload_id,
            payment_db_id=payment.id,
            expires_at=expires_at,
            amount_nano=amount,
            to_address=to_address,
            deeplink=deeplink,
            qr_base64=qr_base64,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _audit_error(self, message: str, actor_id: int | None) -> None:
        try:
            await self._audit.record_error(
                source="Модуль_Платежей",
                message=message,
                actor_id=actor_id,
            )
        except Exception as exc:
            log.warning("payments.ton.audit_failed", error=repr(exc))


def _extract_deeplink(result: Any) -> tuple[str | None, str | None]:
    """Best-effort extraction of ``(deeplink, qr_base64)`` from an SDK reply.

    ``pytonconnect.send_transaction`` normally returns the signed BoC
    (success) or ``None`` — there is no deeplink to surface. Custom wallet
    integrations may return a dict carrying one; we tolerate that shape
    without taking a hard dependency on it.
    """
    if result is None:
        return None, None
    if isinstance(result, dict):
        deeplink = result.get("deeplink") or result.get("universal_link")
        qr = result.get("qr_base64") or result.get("qr")
        return (
            str(deeplink) if deeplink else None,
            str(qr) if qr else None,
        )
    return None, None


__all__ = [
    "PENDING_TTL",
    "TON_CURRENCY",
    "Hook",
    "HookKind",
    "PaymentIntent",
    "TonPaymentsService",
    "WalletNotConnectedError",
]
