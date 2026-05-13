"""Scheduler task implementations, independent of APScheduler.

Keeping the callables here lets us unit/integration-test them without
importing APScheduler.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

import structlog

from app.core.utils.clock import utc_now

if TYPE_CHECKING:  # pragma: no cover
    from app.container import AppServices

log = structlog.get_logger(__name__)

AUDIT_RETENTION_DAYS = 90

# Req 13.4 — 15-minute window for TON pending payments. Mirrors
# :data:`app.features.payments.ton.PENDING_TTL` — duplicated here so the
# scheduler module does not import the payments package for a constant.
TON_PENDING_TTL = timedelta(minutes=15)

_TON_SESSION_EXPIRED_TEXT = (
    "⌛ Сессия истекла. Запустите /connect_wallet заново."
)

# Russian fallbacks for TON payment notifications — mirrored in
# ``app/locales/{ru,en}.yml`` under ``payments.ton.notify.*``. The poller
# tries the i18n loader first and falls back to these when the feature
# is disabled or a key is missing.
_TON_MSG_PAID = "Оплата TON подтверждена. Спасибо!"
_TON_MSG_MISMATCH = (
    "Получен перевод TON с другой суммой. Администратор уведомлён."
)
_TON_MSG_EXPIRED = "Время оплаты TON истекло."


async def audit_cleanup(services: AppServices) -> None:
    """Delete audit records older than 90 days and log the result (Req 6.4)."""
    cutoff = utc_now() - timedelta(days=AUDIT_RETENTION_DAYS)
    try:
        deleted = await services.audit_repo.delete_older_than(cutoff)
    except Exception as exc:
        log.error("audit_cleanup.failed", error=repr(exc))
        await services.audit.record_error(
            source="База_Данных",
            message=f"audit_cleanup failed: {exc!r}",
        )
        return

    log.info("audit_cleanup.done", deleted=deleted, cutoff=cutoff.isoformat())
    await services.audit.record_info(
        event="audit_cleanup",
        details={"deleted": deleted, "cutoff": cutoff.isoformat()},
    )


async def ton_session_cleanup(services: AppServices) -> None:
    """Close expired TON Connect sessions and notify the user (Req 3.5).

    Runs every minute via APScheduler (see :mod:`app.scheduler.jobs`). For
    every ``tc:connect_meta:*`` record:

    * consider the user timed out if ``expires_at < now`` OR no live
      ``tc:session:{id}:*`` key is left in Redis;
    * call :meth:`TonConnector.disconnect` (idempotent) to tear down the
      ``pytonconnect`` session and clear the in-process connector;
    * edit the original ``/connect_wallet`` message to the expiration
      notice — :meth:`Bot.edit_message_text` for ``kind == "text"`` and
      :meth:`Bot.edit_message_caption` for ``kind == "photo"``;
    * drop the ``tc:connect_meta`` record so the next pass does not
      repeat the notification.

    Deviation from the task wording: we scan ``tc:connect_meta:*`` rather
    than ``tc:session:*`` because ``pytonconnect`` refreshes the session
    TTL on every write, so by the time the cleanup fires the session keys
    can already be gone. The parallel ``tc:connect_meta:{id}`` hash (TTL
    ``session_ttl_seconds + 120``) keeps the chat/message context alive
    just long enough for us to edit the user's message.

    Errors for a single user are logged and swallowed — one bad record
    must never kill the whole pass.
    """
    if services.ton is None:
        return

    now = utc_now()
    processed = 0
    notified = 0
    skipped = 0

    async for meta in services.ton.iter_connect_meta():
        try:
            telegram_id = meta.telegram_id
            expired_by_time = meta.expires_at < now
            session_alive = await services.ton.has_session_keys(telegram_id)

            if not expired_by_time and session_alive:
                skipped += 1
                continue

            processed += 1

            # Teardown is idempotent: safe to call even if nothing is left.
            try:
                await services.ton.disconnect(telegram_id)
            except Exception as exc:
                log.warning(
                    "ton_session_cleanup.disconnect_failed",
                    telegram_id=telegram_id,
                    error=repr(exc),
                )

            # Edit the original outbound message to the expiration notice.
            try:
                if meta.kind == "photo":
                    await services.bot.edit_message_caption(
                        chat_id=meta.chat_id,
                        message_id=meta.message_id,
                        caption=_TON_SESSION_EXPIRED_TEXT,
                    )
                else:
                    await services.bot.edit_message_text(
                        text=_TON_SESSION_EXPIRED_TEXT,
                        chat_id=meta.chat_id,
                        message_id=meta.message_id,
                        disable_web_page_preview=True,
                    )
                notified += 1
            except Exception as exc:
                log.info(
                    "ton_session_cleanup.edit_failed",
                    telegram_id=telegram_id,
                    chat_id=meta.chat_id,
                    message_id=meta.message_id,
                    error=repr(exc),
                )

            # Drop the meta so subsequent passes do not re-notify.
            try:
                await services.ton.clear_connect_meta(telegram_id)
            except Exception as exc:
                log.warning(
                    "ton_session_cleanup.meta_clear_failed",
                    telegram_id=telegram_id,
                    error=repr(exc),
                )
        except Exception as exc:
            log.exception(
                "ton_session_cleanup.record_failed",
                telegram_id=getattr(meta, "telegram_id", None),
                error=repr(exc),
            )

    if processed or notified or skipped:
        log.info(
            "ton_session_cleanup.done",
            processed=processed,
            notified=notified,
            skipped=skipped,
        )


async def ton_payments_poll(services: AppServices) -> None:
    """Poll TonCenter for confirmations of pending TON payments (Req 13.2–13.5).

    Registered in :func:`app.scheduler.jobs.build_scheduler` when
    ``services.payments.ton`` is wired in (see task 26.2). Runs every
    minute through :class:`apscheduler.triggers.interval.IntervalTrigger`.

    For every ``Payment`` with ``provider=ton`` and ``status=pending`` we
    look up inbound transactions to ``settings.ton_receive_address`` whose
    text comment equals ``str(payment.payload_id)`` and branch:

    * match found, ``amount_nano == payment.amount`` →
      :meth:`PaymentsRepo.mark_paid` and fire ``on_paid`` hooks. The
      ``(provider, tx_hash_or_charge_id)`` unique constraint created in
      migration ``0001_initial`` guarantees idempotency (Req 13.5) — if
      the same hash is already stored ``mark_paid`` returns ``False`` and
      the hook is skipped.
    * match found, amount mismatch → :meth:`PaymentsRepo.mark_mismatch`,
      fire ``on_mismatch`` hooks, notify the user.
    * no match, ``created_at <= now - 15 min`` → :meth:`mark_expired`,
      fire ``on_expired`` hooks, notify the user, stop polling this row
      (Req 13.4).
    * no match yet, still inside the 15-minute window → leave the row
      ``pending`` and wait for the next tick.

    Per-payment failures are logged and audited but never abort the
    whole pass.
    """
    payments = services.payments
    if payments is None or payments.ton is None:
        return

    ton_bundle = payments.ton
    ton_service = ton_bundle.service
    api_client = ton_bundle.api_client
    settings = services.settings

    receive_address = settings.ton_receive_address
    if not receive_address:
        # Defensive: ``Settings`` already validates this when TON payments
        # are on, but we re-check here rather than crash the job.
        log.error("payments.ton.poll.no_receive_address")
        return

    now = utc_now()
    try:
        pending = await services.payments_repo.find_pending_ton(now)
    except Exception as exc:
        log.error("payments.ton.poll.find_pending_failed", error=repr(exc))
        await services.audit.record_error(
            source="Модуль_Платежей",
            message=f"ton_payments_poll.find_pending_ton failed: {exc!r}",
        )
        return

    if not pending:
        return

    processed = 0
    paid = 0
    expired = 0
    mismatched = 0

    for payment in pending:
        try:
            outcome = await _process_ton_payment(
                services=services,
                ton_service=ton_service,
                api_client=api_client,
                receive_address=receive_address,
                payment=payment,
                now=now,
            )
        except Exception as exc:
            log.exception(
                "payments.ton.poll.payment_failed",
                payload_id=str(getattr(payment, "payload_id", None)),
                user_id=getattr(payment, "user_id", None),
                error=repr(exc),
            )
            await services.audit.record_error(
                source="Модуль_Платежей",
                message=(
                    f"ton_payments_poll payload={payment.payload_id}"
                    f" user={payment.user_id}: {exc!r}"
                ),
                actor_id=payment.user_id,
            )
            continue

        processed += 1
        if outcome == "paid":
            paid += 1
        elif outcome == "expired":
            expired += 1
        elif outcome == "mismatch":
            mismatched += 1

    log.info(
        "payments.ton.poll.done",
        scanned=len(pending),
        processed=processed,
        paid=paid,
        expired=expired,
        mismatched=mismatched,
    )


async def _process_ton_payment(
    *,
    services: AppServices,
    ton_service: object,
    api_client: object,
    receive_address: str,
    payment: object,
    now: object,
) -> str | None:
    """Handle one pending TON payment; return the terminal branch or ``None``.

    Split out so the top-level :func:`ton_payments_poll` stays a thin
    orchestrator — the heavy lifting (lookup, branch selection,
    notification) is testable in isolation and per-row failures can be
    isolated with a single try/except in the caller.
    """
    # Narrow types locally so editors help without creating a cycle at
    # module import time.
    from app.core.db.models import Payment
    from app.features.payments.ton import (
        TonPaymentsService,
    )
    from app.features.payments.ton_api import (
        TonApiClient,
    )

    assert isinstance(payment, Payment)
    assert isinstance(ton_service, TonPaymentsService)
    assert isinstance(api_client, TonApiClient)
    from datetime import datetime

    assert isinstance(now, datetime)

    # We search TonCenter from ``payment.created_at`` rather than
    # ``now - PENDING_TTL`` so a transfer that landed near the beginning
    # of the window is always reachable. TonCenter ``start_utime`` is
    # inclusive; shaving 60s keeps us resilient to clock skew between
    # the bot host and the TON indexer.
    created_at = payment.created_at
    if created_at.tzinfo is None:
        # ``PaymentsRepo.create_pending`` always stores tz-aware UTC, but
        # SQLite round-trips naive — normalize so TonCenter gets UTC.

        created_at = created_at.replace(tzinfo=UTC)
    after = created_at - timedelta(seconds=60)

    try:
        tx = await api_client.find_by_payload(
            receive_address,
            str(payment.payload_id),
            after,
        )
    except Exception as exc:
        log.warning(
            "payments.ton.poll.lookup_failed",
            payload_id=str(payment.payload_id),
            user_id=payment.user_id,
            error=repr(exc),
        )
        await services.audit.record_error(
            source="Модуль_Платежей",
            message=(
                f"ton_payments_poll.find_by_payload payload={payment.payload_id}"
                f" user={payment.user_id}: {exc!r}"
            ),
            actor_id=payment.user_id,
        )
        # Leave the row pending — next tick retries. The 15-min window
        # still governs ultimate expiry below.
        if payment.created_at + TON_PENDING_TTL <= now:
            return await _expire_payment(
                services=services,
                ton_service=ton_service,
                payment=payment,
                now=now,
            )
        return None

    if tx is None:
        # No confirming transaction yet — expire if the window closed.
        if payment.created_at + TON_PENDING_TTL <= now:
            return await _expire_payment(
                services=services,
                ton_service=ton_service,
                payment=payment,
                now=now,
            )
        return None

    # Transaction located on-chain — Req 13.3 or mismatch branch.
    if tx.amount_nano == payment.amount:
        return await _mark_paid(
            services=services,
            ton_service=ton_service,
            payment=payment,
            tx=tx,
            now=now,
        )

    return await _mark_mismatch(
        services=services,
        ton_service=ton_service,
        payment=payment,
        tx=tx,
    )


async def _mark_paid(
    *,
    services: AppServices,
    ton_service: object,
    payment: object,
    tx: object,
    now: object,
) -> str | None:
    """Flip the row to ``paid`` and fire hooks / notifications.

    Idempotency (Req 13.5) is enforced two ways: :meth:`mark_paid`
    returns ``False`` when the row is no longer ``pending`` (another
    poll tick won the race), and the ``(provider, tx_hash_or_charge_id)``
    unique index rejects duplicate hash storage at the DB level.
    """
    from datetime import datetime

    from app.features.payments.ton_api import TonTx

    assert isinstance(tx, TonTx)
    assert isinstance(now, datetime)

    try:
        ok = await services.payments_repo.mark_paid(
            payload_id=payment.payload_id,  # type: ignore[attr-defined]
            charge_id=tx.hash,
            paid_at=now,
        )
    except Exception as exc:
        log.warning(
            "payments.ton.poll.mark_paid_failed",
            payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
            tx_hash=tx.hash,
            error=repr(exc),
        )
        await services.audit.record_error(
            source="Модуль_Платежей",
            message=(
                f"ton_payments_poll.mark_paid payload="
                f"{payment.payload_id} tx={tx.hash}: {exc!r}"  # type: ignore[attr-defined]
            ),
            actor_id=payment.user_id,  # type: ignore[attr-defined]
        )
        return None

    if not ok:
        # Someone already marked this payment paid — skip hook and
        # notification to preserve exactly-once delivery.
        log.info(
            "payments.ton.poll.mark_paid_noop",
            payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
            tx_hash=tx.hash,
        )
        return None

    refreshed = await services.payments_repo.find_by_payload_id(
        payment.payload_id  # type: ignore[attr-defined]
    )
    target = refreshed if refreshed is not None else payment

    log.info(
        "payments.ton.poll.paid",
        payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
        tx_hash=tx.hash,
        user_id=payment.user_id,  # type: ignore[attr-defined]
        amount_nano=payment.amount,  # type: ignore[attr-defined]
    )

    await ton_service.invoke_hooks("on_paid", target)  # type: ignore[attr-defined]
    await _notify_user(
        services=services,
        user_id=payment.user_id,  # type: ignore[attr-defined]
        key="payments.ton.notify.paid",
        fallback=_TON_MSG_PAID,
    )
    return "paid"


async def _mark_mismatch(
    *,
    services: AppServices,
    ton_service: object,
    payment: object,
    tx: object,
) -> str | None:
    """Flip the row to ``mismatch`` and fire hooks / notifications."""
    from app.features.payments.ton_api import TonTx

    assert isinstance(tx, TonTx)

    try:
        ok = await services.payments_repo.mark_mismatch(
            payment.payload_id,  # type: ignore[attr-defined]
            tx.hash,
        )
    except Exception as exc:
        log.warning(
            "payments.ton.poll.mark_mismatch_failed",
            payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
            tx_hash=tx.hash,
            error=repr(exc),
        )
        await services.audit.record_error(
            source="Модуль_Платежей",
            message=(
                f"ton_payments_poll.mark_mismatch payload="
                f"{payment.payload_id} tx={tx.hash}: {exc!r}"  # type: ignore[attr-defined]
            ),
            actor_id=payment.user_id,  # type: ignore[attr-defined]
        )
        return None

    if not ok:
        log.info(
            "payments.ton.poll.mark_mismatch_noop",
            payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
            tx_hash=tx.hash,
        )
        return None

    refreshed = await services.payments_repo.find_by_payload_id(
        payment.payload_id  # type: ignore[attr-defined]
    )
    target = refreshed if refreshed is not None else payment

    log.warning(
        "payments.ton.poll.mismatch",
        payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
        tx_hash=tx.hash,
        user_id=payment.user_id,  # type: ignore[attr-defined]
        expected_nano=payment.amount,  # type: ignore[attr-defined]
        got_nano=tx.amount_nano,
    )

    await ton_service.invoke_hooks("on_mismatch", target)  # type: ignore[attr-defined]
    await _notify_user(
        services=services,
        user_id=payment.user_id,  # type: ignore[attr-defined]
        key="payments.ton.notify.mismatch",
        fallback=_TON_MSG_MISMATCH,
    )
    return "mismatch"


async def _expire_payment(
    *,
    services: AppServices,
    ton_service: object,
    payment: object,
    now: object,
) -> str | None:
    """Flip the row to ``expired`` and fire hooks / notifications (Req 13.4)."""
    try:
        ok = await services.payments_repo.mark_expired(
            payment.payload_id  # type: ignore[attr-defined]
        )
    except Exception as exc:
        log.warning(
            "payments.ton.poll.mark_expired_failed",
            payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
            error=repr(exc),
        )
        await services.audit.record_error(
            source="Модуль_Платежей",
            message=(
                f"ton_payments_poll.mark_expired payload="
                f"{payment.payload_id}: {exc!r}"  # type: ignore[attr-defined]
            ),
            actor_id=payment.user_id,  # type: ignore[attr-defined]
        )
        return None

    if not ok:
        log.info(
            "payments.ton.poll.mark_expired_noop",
            payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
        )
        return None

    refreshed = await services.payments_repo.find_by_payload_id(
        payment.payload_id  # type: ignore[attr-defined]
    )
    target = refreshed if refreshed is not None else payment

    log.info(
        "payments.ton.poll.expired",
        payload_id=str(payment.payload_id),  # type: ignore[attr-defined]
        user_id=payment.user_id,  # type: ignore[attr-defined]
        created_at=payment.created_at.isoformat(),  # type: ignore[attr-defined]
    )

    await ton_service.invoke_hooks("on_expired", target)  # type: ignore[attr-defined]
    await _notify_user(
        services=services,
        user_id=payment.user_id,  # type: ignore[attr-defined]
        key="payments.ton.notify.expired",
        fallback=_TON_MSG_EXPIRED,
    )
    return "expired"


async def _notify_user(
    *,
    services: AppServices,
    user_id: int,
    key: str,
    fallback: str,
) -> None:
    """Send a localized notification to ``user_id``; never raise.

    The i18n loader (when the feature is on) resolves ``key`` against
    the user's stored ``language_code`` with a default-locale fallback
    (Req 10.4). If the feature is off or the lookup fails we send
    ``fallback`` — the Russian default from the spec.
    """
    text = fallback
    i18n = services.i18n
    if i18n is not None:
        try:
            user = await services.users_repo.get_by_tg_id(user_id)
        except Exception as exc:
            log.warning(
                "payments.ton.poll.notify_user_lookup_failed",
                user_id=user_id,
                error=repr(exc),
            )
            user = None
        lang = getattr(user, "language_code", None) if user is not None else None
        try:
            text = i18n.translate(key, lang)
        except Exception as exc:
            log.warning(
                "payments.ton.poll.notify_translate_failed",
                user_id=user_id,
                key=key,
                error=repr(exc),
            )
            text = fallback

    try:
        await services.bot.send_message(user_id, text)
    except Exception as exc:
        log.info(
            "payments.ton.poll.notify_failed",
            user_id=user_id,
            key=key,
            error=repr(exc),
        )
