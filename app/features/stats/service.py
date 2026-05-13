"""Модуль_Статистики — aggregation service for the ``/stats`` handler.

Implements Req 9.1 / 9.2 / 9.3:

* ``/stats`` overview: total users, active over the last 24 h / 7 d / 30 d
  (by ``last_seen_at``), banned count (by ``status``) and number of
  connected TON wallets (``ton_address IS NOT NULL``).
* Registrations distribution over an arbitrary period, bucketed by day.

Design notes (see design.md § Модуль_Статистики):

* All five overview queries run inside a single ``async with
  session.begin()`` block so the snapshot is consistent under concurrent
  writes. It is effectively read-only — we never touch autoflush or issue
  INSERT/UPDATE statements inside the block — but wrapping in
  ``session.begin()`` matches the repository convention.
* Each metric is a separate ``SELECT count(*)`` filtered by the relevant
  predicate. Using separate counters (instead of a single query with
  conditional aggregates) keeps each plan simple and lets the DB hit the
  pre-existing single-column indexes (``ix_users_last_seen_at``,
  ``ix_users_status``, ``ux_users_ton_address``).
* Day bucketing is dialect-aware: Postgres uses ``date_trunc('day', …)``
  (the canonical form from the design doc); SQLite does not ship
  ``date_trunc`` so we fall back to ``strftime('%Y-%m-%d', …)`` and parse
  the string into a :class:`datetime.date` on the Python side. The unit
  used for the bucket is always a calendar day, independent of dialect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db.models import User, UserStatus

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.engine import Dialect


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatsOverview:
    """Snapshot returned by :meth:`StatsService.get_overview` (Req 9.1)."""

    total: int
    active_24h: int
    active_7d: int
    active_30d: int
    banned: int
    wallets: int
    at: datetime


@dataclass(frozen=True, slots=True)
class DayCount:
    """One bucket in the per-day registrations histogram (Req 9.2)."""

    day: date
    count: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StatsService:
    """Read-only aggregations over ``users`` for the admin ``/stats`` command.

    Requirements: 9.1 (overview counters), 9.2 (registrations by day),
    9.3 (``last_seen_at``-based activity definition).
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    # ---- overview --------------------------------------------------------

    async def get_overview(self, now: datetime) -> StatsOverview:
        """Run all five aggregates inside a single transaction (Req 9.1).

        ``now`` is injected rather than read from the clock inside so the
        caller stays in control of the reference time (tests, and the
        handler passes ``Clock()``).
        """
        since_24h = now - timedelta(hours=24)
        since_7d = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)

        async with self._sm() as session, session.begin():
            total = await self._scalar_count(session)
            active_24h = await self._scalar_count(
                session, User.last_seen_at >= since_24h
            )
            active_7d = await self._scalar_count(
                session, User.last_seen_at >= since_7d
            )
            active_30d = await self._scalar_count(
                session, User.last_seen_at >= since_30d
            )
            banned = await self._scalar_count(
                session, User.status == UserStatus.banned
            )
            wallets = await self._scalar_count(
                session, User.ton_address.is_not(None)
            )

        return StatsOverview(
            total=total,
            active_24h=active_24h,
            active_7d=active_7d,
            active_30d=active_30d,
            banned=banned,
            wallets=wallets,
            at=now,
        )

    # ---- per-day histogram ----------------------------------------------

    async def get_registrations_by_day(
        self, from_: datetime, to: datetime
    ) -> list[DayCount]:
        """Return registrations grouped by calendar day, ordered ascending.

        Implements Req 9.2. The ``[from_, to]`` range is inclusive on both
        ends (``BETWEEN``). The exact SQL shape for the bucketing depends
        on the dialect: Postgres uses ``date_trunc('day', …)``, SQLite
        uses ``strftime('%Y-%m-%d', …)``.
        """
        async with self._sm() as session, session.begin():
            dialect = session.get_bind().dialect
            bucket = _day_bucket_expr(dialect, User.created_at)
            stmt = (
                select(bucket.label("day"), func.count().label("cnt"))
                .where(User.created_at.between(from_, to))
                .group_by(bucket)
                .order_by(bucket)
            )
            result = await session.execute(stmt)
            rows = result.all()

        return [DayCount(day=_to_date(row[0]), count=int(row[1])) for row in rows]

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    async def _scalar_count(session: AsyncSession, *predicates: object) -> int:
        """Return ``SELECT count(*) FROM users [WHERE ...]`` as ``int``."""
        stmt = select(func.count()).select_from(User)
        if predicates:
            stmt = stmt.where(*predicates)  # type: ignore[arg-type]
        result = await session.execute(stmt)
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Dialect-aware helpers
# ---------------------------------------------------------------------------


def _day_bucket_expr(dialect: Dialect, column):  # type: ignore[no-untyped-def]
    """Expression that truncates ``column`` (a timestamp) to its day.

    * Postgres: ``date_trunc('day', column)`` — returns ``timestamp`` which
      SQLAlchemy surfaces as a :class:`~datetime.datetime`.
    * SQLite: ``strftime('%Y-%m-%d', column)`` — returns a text
      ``'YYYY-MM-DD'`` string. The caller parses it back via
      :func:`_to_date`.
    * Anything else: fall back to ``strftime``-style behaviour if the DB
      understands it, otherwise to ``date_trunc``; parsing is defensive.
    """
    name = dialect.name
    if name == "postgresql":
        return func.date_trunc("day", column)
    if name == "sqlite":
        return func.strftime("%Y-%m-%d", column)
    # Unknown dialect — attempt ``date_trunc`` (standard SQL) first; if the
    # driver rejects it at execute time SQLAlchemy will raise and the error
    # bubbles up to the handler. Defensive parsing below handles either
    # return shape.
    return func.date_trunc("day", column)


def _to_date(value: object) -> date:
    """Best-effort coercion of a bucket value into :class:`date`.

    ``date_trunc('day', …)`` hands us a ``datetime``; ``strftime`` hands
    us a ``'YYYY-MM-DD'`` string. Anything unexpected is parsed as an
    ISO 8601 prefix so a surprise dialect cannot crash the handler.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # ``datetime.fromisoformat`` accepts both ``2025-01-31`` and
        # ``2025-01-31 03:00:00`` and preserves UTC offsets.
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return date.fromisoformat(value[:10])
    raise TypeError(f"Unsupported day bucket value type: {type(value).__name__}")


__all__ = [
    "DayCount",
    "StatsOverview",
    "StatsService",
]
