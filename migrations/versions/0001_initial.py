"""initial schema: users, action_log, payments, broadcasts

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-10

"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


USER_STATUS = sa.Enum(
    "active", "banned", "muted", "pending_captcha", name="user_status"
)
USER_ROLE = sa.Enum("user", "admin", "superadmin", name="user_role")
AUDIT_LEVEL = sa.Enum("info", "warning", "error", name="audit_level")
PAYMENT_PROVIDER = sa.Enum("stars", "ton", name="payment_provider")
PAYMENT_STATUS = sa.Enum(
    "pending", "paid", "expired", "mismatch", "failed", name="payment_status"
)
BROADCAST_STATUS = sa.Enum(
    "queued", "running", "completed", "cancelled", "failed", name="broadcast_status"
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # SQLite does not support AUTOINCREMENT on BigInteger — fall back to
    # plain Integer there. Postgres keeps BIGSERIAL-equivalent behaviour.
    BigIntPk = sa.BigInteger() if is_pg else sa.Integer()

    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(32)),
        sa.Column("first_name", sa.String(64)),
        sa.Column("last_name", sa.String(64)),
        sa.Column("language_code", sa.String(8)),
        sa.Column(
            "status",
            USER_STATUS if is_pg else sa.String(16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "role",
            USER_ROLE if is_pg else sa.String(16),
            nullable=False,
            server_default="user",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("banned_at", sa.DateTime(timezone=True)),
        sa.Column(
            "banned_by",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="SET NULL"),
        ),
        sa.Column("ban_reason", sa.String(500)),
        sa.Column("muted_until", sa.DateTime(timezone=True)),
        sa.Column(
            "muted_by",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="SET NULL"),
        ),
        sa.Column(
            "referrer_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="SET NULL"),
        ),
        sa.Column("referrals_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_referral_at", sa.DateTime(timezone=True)),
        sa.Column("ton_address", sa.String(68)),
        sa.Column("ton_wallet_name", sa.String(64)),
        sa.Column("ton_connected_at", sa.DateTime(timezone=True)),
        sa.Column(
            "is_blocked_bot", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"])
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index("ix_users_created_at", "users", ["created_at"])
    if is_pg:
        op.create_index(
            "ix_users_role",
            "users",
            ["role"],
            postgresql_where=sa.text("role <> 'user'"),
        )
        op.create_index(
            "ux_users_ton_address",
            "users",
            ["ton_address"],
            unique=True,
            postgresql_where=sa.text("ton_address IS NOT NULL"),
        )
    else:
        op.create_index("ix_users_role", "users", ["role"])
        op.create_index("ux_users_ton_address", "users", ["ton_address"], unique=True)

    # ------------------------------------------------------------- action_log
    op.create_table(
        "action_log",
        sa.Column("id", BigIntPk, primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("level", AUDIT_LEVEL if is_pg else sa.String(16), nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        sa.Column("target_id", sa.BigInteger()),
        sa.Column("action", sa.String(32)),
        sa.Column("source", sa.String(32)),
        sa.Column("reason", sa.String(500)),
        sa.Column("message", sa.String(1000)),
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
        ),
    )
    op.create_index("ix_action_log_created_at", "action_log", ["created_at"])
    op.create_index("ix_action_log_level", "action_log", ["level"])
    op.create_index("ix_action_log_actor", "action_log", ["actor_id"])
    op.create_index("ix_action_log_target", "action_log", ["target_id"])

    # ------------------------------------------------------------- payments
    op.create_table(
        "payments",
        sa.Column("id", BigIntPk, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", PAYMENT_PROVIDER if is_pg else sa.String(16), nullable=False),
        sa.Column(
            "status",
            PAYMENT_STATUS if is_pg else sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("purpose", sa.String(64)),
        sa.Column("tx_hash_or_charge_id", sa.String(128)),
        sa.Column(
            "payload_id",
            postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider", "tx_hash_or_charge_id", name="ux_payments_charge"
        ),
    )
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])

    # ------------------------------------------------------------- broadcasts
    op.create_table(
        "broadcasts",
        sa.Column("id", BigIntPk, primary_key=True, autoincrement=True),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="SET NULL"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status",
            BROADCAST_STATUS if is_pg else sa.String(16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("filter_kind", sa.String(32), nullable=False),
        sa.Column("filter_value", sa.String(16)),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_table("broadcasts")

    op.drop_index("ix_payments_status_created", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_action_log_target", table_name="action_log")
    op.drop_index("ix_action_log_actor", table_name="action_log")
    op.drop_index("ix_action_log_level", table_name="action_log")
    op.drop_index("ix_action_log_created_at", table_name="action_log")
    op.drop_table("action_log")

    op.drop_index("ux_users_ton_address", table_name="users")
    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_index("ix_users_last_seen_at", table_name="users")
    op.drop_table("users")

    if is_pg:
        BROADCAST_STATUS.drop(bind, checkfirst=True)
        PAYMENT_STATUS.drop(bind, checkfirst=True)
        PAYMENT_PROVIDER.drop(bind, checkfirst=True)
        AUDIT_LEVEL.drop(bind, checkfirst=True)
        USER_ROLE.drop(bind, checkfirst=True)
        USER_STATUS.drop(bind, checkfirst=True)
