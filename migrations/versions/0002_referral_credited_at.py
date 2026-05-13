"""add users.referral_credited_at

Revision ID: 0002_referral_credited_at
Revises: 0001_initial
Create Date: 2026-05-13

The new column is the canonical "the inviter has been credited for this
invitee" marker. It lets the credit step run exactly-once via a single
``UPDATE ... WHERE referral_credited_at IS NULL`` statement (Req 7.2 +
the antifraud refinement: credit only after captcha + required-channel
subscription).

Backfill rationale: existing rows where ``referrer_id IS NOT NULL`` were
already credited under the legacy "credit on /start" code path, so we
stamp ``referral_credited_at = COALESCE(last_referral_at, NOW())`` for
every such row. That preserves history and prevents the new gating logic
from re-crediting them on the next eligible event.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_referral_credited_at"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("referral_credited_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill: any user whose referrer was already recorded under the
    # legacy flow has, by definition, already been credited. We use the
    # invitee row itself as the source of truth — ``last_referral_at``
    # belongs to the inviter and is unrelated to the invitee, so we fall
    # back to ``users.created_at`` which is always populated.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE users SET referral_credited_at = COALESCE(created_at, NOW()) "
                "WHERE referrer_id IS NOT NULL AND referral_credited_at IS NULL"
            )
        )
    else:
        # SQLite: use CURRENT_TIMESTAMP as the fallback. ``created_at`` is
        # already preferred via COALESCE.
        bind.execute(
            sa.text(
                "UPDATE users SET referral_credited_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
                "WHERE referrer_id IS NOT NULL AND referral_credited_at IS NULL"
            )
        )


def downgrade() -> None:
    op.drop_column("users", "referral_credited_at")
