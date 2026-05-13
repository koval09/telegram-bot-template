"""UsersRepo — CRUD and query helpers for the ``users`` table.

All writes go through ``async with session.begin()`` so every operation runs
in a single transaction.

Requirements: 1.1, 1.2, 1.3, 1.6, 1.7, 2.1, 3.2, 3.4, 5.1, 5.3, 5.5, 5.7,
7.2, 8.3, 9.1, 9.3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db.models import User, UserRole, UserStatus


@dataclass(frozen=True, slots=True)
class TgUserData:
    """Slice of Telegram User fields we upsert from ``types.User``."""

    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    language_code: str | None


@dataclass(frozen=True, slots=True)
class BroadcastFilter:
    kind: Literal["all", "active_30d", "lang"]
    value: str | None = None


class UsersRepo:
    """Repository over the ``users`` table."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get_by_tg_id(self, telegram_id: int) -> User | None:
        async with self._sm() as session:
            return await session.get(User, telegram_id)

    async def count_active_since(self, since: datetime) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(User).where(User.last_seen_at >= since)
            result = await session.execute(stmt)
            return int(result.scalar_one())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def upsert_from_tg(
        self, data: TgUserData, *, now: datetime
    ) -> tuple[User, bool]:
        """Insert the user or update changed fields only.

        Returns ``(user, created)``.

        Implements Req 1.1 (initial insert) and 1.3 (partial update of only
        the changed fields).
        """
        async with self._sm() as session, session.begin():
            user = await session.get(User, data.telegram_id)
            if user is None:
                user = User(
                    telegram_id=data.telegram_id,
                    username=data.username,
                    first_name=data.first_name,
                    last_name=data.last_name,
                    language_code=data.language_code,
                    status=UserStatus.active,
                    role=UserRole.user,
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(user)
                await session.flush()
                return user, True

            changed = False
            if data.username != user.username:
                user.username = data.username
                changed = True
            if data.first_name != user.first_name:
                user.first_name = data.first_name
                changed = True
            if data.last_name != user.last_name:
                user.last_name = data.last_name
                changed = True
            if data.language_code is not None and data.language_code != user.language_code:
                user.language_code = data.language_code
                changed = True
            user.last_seen_at = now
            if changed:
                await session.flush()
            return user, False

    async def update_last_seen(self, telegram_id: int, now: datetime) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(last_seen_at=now)
            )

    async def set_referrer(
        self, telegram_id: int, referrer_id: int
    ) -> bool:
        """Set ``referrer_id`` ONLY if currently NULL (i.e. first registration).

        Requirement 1.7/1.8. Returns True if the field was updated.
        """
        async with self._sm() as session, session.begin():
            stmt = (
                update(User)
                .where(User.telegram_id == telegram_id)
                .where(User.referrer_id.is_(None))
                .values(referrer_id=referrer_id)
            )
            result = await session.execute(stmt)
            return bool(result.rowcount)

    async def increment_referrals(self, referrer_id: int, now: datetime) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == referrer_id)
                .values(
                    referrals_count=User.referrals_count + 1,
                    last_referral_at=now,
                )
            )

    async def try_credit_referral(
        self, invitee_id: int, now: datetime
    ) -> int | None:
        """Credit the invitee's referrer exactly once.

        Atomically:

        * stamps ``invitee.referral_credited_at`` (NULL → ``now``) so
          subsequent calls for the same invitee become no-ops;
        * bumps ``inviter.referrals_count`` and ``inviter.last_referral_at``.

        Returns the inviter's ``telegram_id`` when the credit was applied
        in this call, or ``None`` when there was nothing to do (no
        referrer recorded, or the credit had already been registered).

        Both writes happen inside a single transaction so a partial
        crediting (counter without marker, or marker without counter)
        is impossible.
        """
        async with self._sm() as session, session.begin():
            invitee = await session.get(User, invitee_id)
            if invitee is None:
                return None
            if invitee.referrer_id is None:
                return None
            if invitee.referral_credited_at is not None:
                return None

            referrer_id = int(invitee.referrer_id)

            # Stamp the marker first; do it conditionally so two parallel
            # calls cannot both observe NULL and double-credit.
            stamp = await session.execute(
                update(User)
                .where(User.telegram_id == invitee_id)
                .where(User.referral_credited_at.is_(None))
                .where(User.referrer_id == referrer_id)
                .values(referral_credited_at=now)
            )
            if not stamp.rowcount:
                return None

            await session.execute(
                update(User)
                .where(User.telegram_id == referrer_id)
                .values(
                    referrals_count=User.referrals_count + 1,
                    last_referral_at=now,
                )
            )
            return referrer_id

    async def set_status(
        self,
        telegram_id: int,
        status: UserStatus,
        *,
        banned_by: int | None = None,
        ban_reason: str | None = None,
        muted_until: datetime | None = None,
        muted_by: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": status}
        if status is UserStatus.banned:
            values.update(
                banned_at=now, banned_by=banned_by, ban_reason=ban_reason
            )
        elif status is UserStatus.muted:
            values.update(muted_until=muted_until, muted_by=muted_by)
        elif status is UserStatus.active:
            # Clear moderation fields on re-activation (Req 5.7).
            values.update(
                banned_at=None,
                banned_by=None,
                ban_reason=None,
                muted_until=None,
                muted_by=None,
            )
        async with self._sm() as session, session.begin():
            result = await session.execute(
                update(User).where(User.telegram_id == telegram_id).values(**values)
            )
            return bool(result.rowcount)

    async def set_role(self, telegram_id: int, role: UserRole) -> bool:
        async with self._sm() as session, session.begin():
            result = await session.execute(
                update(User).where(User.telegram_id == telegram_id).values(role=role)
            )
            return bool(result.rowcount)

    async def set_language(self, telegram_id: int, language_code: str) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(language_code=language_code)
            )

    async def set_wallet(
        self,
        telegram_id: int,
        address: str,
        wallet_name: str | None,
        now: datetime,
    ) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(
                    ton_address=address,
                    ton_wallet_name=wallet_name,
                    ton_connected_at=now,
                )
            )

    async def clear_wallet(self, telegram_id: int) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(
                    ton_address=None,
                    ton_wallet_name=None,
                    ton_connected_at=None,
                )
            )

    async def mark_blocked_bot(self, telegram_id: int) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(is_blocked_bot=True)
            )

    async def iterate_for_broadcast(
        self, filter_: BroadcastFilter, *, batch_size: int = 200
    ) -> AsyncIterator[int]:
        """Stream ``telegram_id`` values matching the broadcast filter.

        Always excludes banned users and users that blocked the bot.
        """
        async with self._sm() as session:
            stmt = select(User.telegram_id).where(
                User.status != UserStatus.banned,
                User.is_blocked_bot.is_(False),
            )
            if filter_.kind == "active_30d":
                from datetime import timedelta

                from app.core.utils.clock import utc_now

                since = utc_now() - timedelta(days=30)
                stmt = stmt.where(User.last_seen_at >= since)
            elif filter_.kind == "lang" and filter_.value:
                stmt = stmt.where(User.language_code == filter_.value)
            stmt = stmt.execution_options(yield_per=batch_size)

            result = await session.stream(stmt)
            async for row in result:
                yield int(row[0])
