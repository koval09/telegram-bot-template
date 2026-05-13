"""ORM models for the bot template.

Tables (see design.md § "Модели данных"):
- ``users`` — one row per Telegram user (Req 1, 2, 3, 5, 7).
- ``action_log`` — moderation and system events (Req 6).
- ``payments`` — Stars & TON invoices (Req 13).
- ``broadcasts`` — broadcast jobs and counters (Req 8).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

# BigInteger that falls back to plain Integer on SQLite so that the
# ``AUTOINCREMENT`` primary key works identically across dialects.
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


# --------------------------------------------------------------------------
# Enum types
# --------------------------------------------------------------------------

class UserStatus(str, enum.Enum):
    active = "active"
    banned = "banned"
    muted = "muted"
    pending_captcha = "pending_captcha"


class UserRole(str, enum.Enum):
    user = "user"
    admin = "admin"
    superadmin = "superadmin"


class AuditLevel(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"


class PaymentProvider(str, enum.Enum):
    stars = "stars"
    ton = "ton"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    expired = "expired"
    mismatch = "mismatch"
    failed = "failed"


class BroadcastStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    username: Mapped[str | None] = mapped_column(String(32))
    first_name: Mapped[str | None] = mapped_column(String(64))
    last_name: Mapped[str | None] = mapped_column(String(64))
    language_code: Mapped[str | None] = mapped_column(String(8))

    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status"),
        nullable=False,
        default=UserStatus.active,
        server_default=UserStatus.active.value,
    )
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role"),
        nullable=False,
        default=UserRole.user,
        server_default=UserRole.user.value,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Moderation
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    banned_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"),
    )
    ban_reason: Mapped[str | None] = mapped_column(String(500))
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    muted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"),
    )

    # Referrals
    referrer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"),
    )
    referrals_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_referral_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set on the invitee row when their referral has been credited to the
    # inviter's ``referrals_count``. ``NOT NULL`` is the unique signal that
    # the inviter's counter was already incremented for this invitee, so
    # eligibility checks can be expressed as a single SQL predicate
    # (``referrer_id IS NOT NULL AND referral_credited_at IS NULL``).
    referral_credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # TON
    ton_address: Mapped[str | None] = mapped_column(String(68))
    ton_wallet_name: Mapped[str | None] = mapped_column(String(64))
    ton_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Broadcasts
    is_blocked_bot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    __table_args__ = (
        Index("ix_users_last_seen_at", "last_seen_at"),
        Index("ix_users_status", "status"),
        Index("ix_users_created_at", "created_at"),
        Index("ix_users_role", "role", postgresql_where="role <> 'user'"),
        Index(
            "ux_users_ton_address",
            "ton_address",
            unique=True,
            postgresql_where="ton_address IS NOT NULL",
        ),
    )


# --------------------------------------------------------------------------
# action_log
# --------------------------------------------------------------------------

class ActionLog(Base):
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    level: Mapped[AuditLevel] = mapped_column(
        SAEnum(AuditLevel, name="audit_level"), nullable=False
    )
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(500))
    message: Mapped[str | None] = mapped_column(String(1000))
    trace_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    __table_args__ = (
        Index("ix_action_log_created_at", "created_at", postgresql_using="btree"),
        Index("ix_action_log_level", "level"),
        Index("ix_action_log_actor", "actor_id"),
        Index("ix_action_log_target", "target_id"),
    )


# --------------------------------------------------------------------------
# payments
# --------------------------------------------------------------------------

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        SAEnum(PaymentProvider, name="payment_provider"), nullable=False
    )
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status"),
        nullable=False,
        default=PaymentStatus.pending,
        server_default=PaymentStatus.pending.value,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(64))
    tx_hash_or_charge_id: Mapped[str | None] = mapped_column(String(128))
    payload_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4, unique=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship("User", lazy="noload")

    __table_args__ = (
        UniqueConstraint(
            "provider", "tx_hash_or_charge_id", name="ux_payments_charge",
        ),
        Index("ix_payments_status_created", "status", "created_at"),
    )


# --------------------------------------------------------------------------
# broadcasts
# --------------------------------------------------------------------------

class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[BroadcastStatus] = mapped_column(
        SAEnum(BroadcastStatus, name="broadcast_status"),
        nullable=False,
        default=BroadcastStatus.queued,
        server_default=BroadcastStatus.queued.value,
    )
    filter_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    filter_value: Mapped[str | None] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, nullable=False)

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    delivered: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    blocked: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
