"""BroadcastsRepo — persistence for the ``broadcasts`` table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db.models import Broadcast, BroadcastStatus


class BroadcastsRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def create_running(
        self,
        *,
        created_by: int,
        text: str,
        filter_kind: str,
        filter_value: str | None,
        now: datetime,
    ) -> Broadcast:
        async with self._sm() as session, session.begin():
            broadcast = Broadcast(
                created_by=created_by,
                status=BroadcastStatus.running,
                filter_kind=filter_kind,
                filter_value=filter_value,
                text=text,
                created_at=now,
                started_at=now,
            )
            session.add(broadcast)
            await session.flush()
            return broadcast

    async def update_counters(
        self,
        broadcast_id: int,
        *,
        total: int | None = None,
        delivered: int | None = None,
        failed: int | None = None,
        blocked: int | None = None,
    ) -> None:
        values: dict[str, object] = {}
        if total is not None:
            values["total"] = total
        if delivered is not None:
            values["delivered"] = delivered
        if failed is not None:
            values["failed"] = failed
        if blocked is not None:
            values["blocked"] = blocked
        if not values:
            return
        async with self._sm() as session, session.begin():
            await session.execute(
                update(Broadcast).where(Broadcast.id == broadcast_id).values(**values)
            )

    async def finish(
        self, broadcast_id: int, status: BroadcastStatus, now: datetime
    ) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(Broadcast)
                .where(Broadcast.id == broadcast_id)
                .values(status=status, finished_at=now)
            )
