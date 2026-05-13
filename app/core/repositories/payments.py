"""PaymentsRepo — persistence for the ``payments`` table."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db.models import Payment, PaymentProvider, PaymentStatus


class PaymentsRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def create_pending(
        self,
        *,
        user_id: int,
        provider: PaymentProvider,
        amount: int,
        currency: str,
        purpose: str | None,
        now: datetime,
        expires_at: datetime,
        payload_id: uuid.UUID | None = None,
    ) -> Payment:
        """Insert a new ``pending`` payment row.

        ``payload_id`` is the bot-side correlation identifier — passed to
        the payment provider as ``payload`` so the ``pre_checkout_query``
        and ``successful_payment`` handlers can match the update back to
        this row. Callers that need to know the UUID in advance (e.g. the
        Stars flow that puts it into ``Bot.create_invoice_link(payload=…)``
        before persisting) generate it themselves; otherwise a fresh UUID
        is allocated.
        """
        async with self._sm() as session, session.begin():
            payment = Payment(
                user_id=user_id,
                provider=provider,
                status=PaymentStatus.pending,
                amount=amount,
                currency=currency,
                purpose=purpose,
                payload_id=payload_id if payload_id is not None else uuid.uuid4(),
                created_at=now,
                expires_at=expires_at,
            )
            session.add(payment)
            await session.flush()
            return payment

    async def find_by_payload_id(self, payload_id: uuid.UUID) -> Payment | None:
        """Look up a payment by its bot-side ``payload_id`` UUID.

        Used by the Stars ``pre_checkout_query`` handler (basic validation
        of the inbound ``invoice_payload``) and by the ``successful_payment``
        handler (fetching the current row after ``mark_paid``).
        """
        async with self._sm() as session:
            stmt = select(Payment).where(Payment.payload_id == payload_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def exists_by_charge_id(
        self, provider: PaymentProvider, charge_id: str
    ) -> bool:
        async with self._sm() as session:
            stmt = select(Payment.id).where(
                Payment.provider == provider,
                Payment.tx_hash_or_charge_id == charge_id,
            )
            result = await session.execute(stmt)
            return result.first() is not None

    async def mark_paid(
        self,
        payload_id: uuid.UUID,
        charge_id: str,
        paid_at: datetime,
    ) -> bool:
        async with self._sm() as session, session.begin():
            stmt = (
                update(Payment)
                .where(
                    Payment.payload_id == payload_id,
                    Payment.status == PaymentStatus.pending,
                )
                .values(
                    status=PaymentStatus.paid,
                    tx_hash_or_charge_id=charge_id,
                    paid_at=paid_at,
                )
            )
            result = await session.execute(stmt)
            return bool(result.rowcount)

    async def mark_expired(self, payload_id: uuid.UUID) -> bool:
        async with self._sm() as session, session.begin():
            stmt = (
                update(Payment)
                .where(
                    Payment.payload_id == payload_id,
                    Payment.status == PaymentStatus.pending,
                )
                .values(status=PaymentStatus.expired)
            )
            result = await session.execute(stmt)
            return bool(result.rowcount)

    async def mark_mismatch(
        self, payload_id: uuid.UUID, charge_id: str | None = None
    ) -> bool:
        async with self._sm() as session, session.begin():
            values: dict[str, object] = {"status": PaymentStatus.mismatch}
            if charge_id is not None:
                values["tx_hash_or_charge_id"] = charge_id
            stmt = (
                update(Payment)
                .where(
                    Payment.payload_id == payload_id,
                    Payment.status == PaymentStatus.pending,
                )
                .values(**values)
            )
            result = await session.execute(stmt)
            return bool(result.rowcount)

    async def find_pending_ton(self, now: datetime) -> list[Payment]:
        async with self._sm() as session:
            stmt = select(Payment).where(
                and_(
                    Payment.provider == PaymentProvider.ton,
                    Payment.status == PaymentStatus.pending,
                    Payment.expires_at > now,
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def find_expired_pending(self, now: datetime) -> list[Payment]:
        async with self._sm() as session:
            stmt = select(Payment).where(
                and_(
                    Payment.status == PaymentStatus.pending,
                    Payment.expires_at <= now,
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
