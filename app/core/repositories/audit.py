"""AuditRepo — persistence for the ``action_log`` table.

Used by the Journal_Действий service (Req 6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db.models import ActionLog, AuditLevel


@dataclass(frozen=True, slots=True)
class AuditRecordInput:
    level: AuditLevel
    created_at: datetime
    actor_id: int | None = None
    target_id: int | None = None
    action: str | None = None
    source: str | None = None
    reason: str | None = None
    message: str | None = None
    trace_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: list[ActionLog]
    page: int
    page_size: int
    total: int
    total_pages: int


class AuditRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def insert(self, record: AuditRecordInput) -> None:
        async with self._sm() as session, session.begin():
            session.add(
                ActionLog(
                    level=record.level,
                    created_at=record.created_at,
                    actor_id=record.actor_id,
                    target_id=record.target_id,
                    action=record.action,
                    source=record.source,
                    reason=record.reason,
                    message=record.message,
                    trace_id=record.trace_id,
                )
            )

    async def list_page(self, page: int, page_size: int = 50) -> AuditPage:
        if page < 1:
            page = 1
        async with self._sm() as session:
            total = int(
                (await session.execute(select(func.count()).select_from(ActionLog))).scalar_one()
            )
            stmt = (
                select(ActionLog)
                .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            result = await session.execute(stmt)
            items = list(result.scalars().all())
        total_pages = max(1, (total + page_size - 1) // page_size)
        return AuditPage(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )

    async def delete_older_than(self, cutoff: datetime) -> int:
        async with self._sm() as session, session.begin():
            result = await session.execute(
                delete(ActionLog).where(ActionLog.created_at < cutoff)
            )
            return int(result.rowcount or 0)
