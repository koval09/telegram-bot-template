"""Журнал_Действий — append-only audit log with retries and fallback.

Requirements:
- 6.1 — moderation record with required fields within 2 seconds.
- 6.2 — error record with source and message ≤ 1000 chars (truncated).
- 6.3 — paginated list for superadmin.
- 6.4 — 90-day retention; cleanup creates an ``info`` record.
- 6.6 — 3 retries on DB failure; fallback to stdout + notify superadmin.

The fallback path guarantees that moderation actions are never blocked by
audit-log unavailability (Req 6.6).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import structlog

from app.core.db.models import AuditLevel
from app.core.repositories.audit import AuditPage, AuditRecordInput, AuditRepo
from app.core.services.retry import (
    RetryExhausted,
    db_retryable_exceptions,
    with_retry,
)
from app.core.utils.clock import Clock, utc_now

log = structlog.get_logger(__name__)

MAX_REASON_LEN = 500
MAX_MESSAGE_LEN = 1000
_TRUNC_SUFFIX = " ... [truncated]"


# Whitelist of high-signal events that earn a row in ``action_log``.
# Everything else (``startup``, ``audit_cleanup``, ``missing_translation``,
# ``ratelimit_blocked``, etc.) is logged via structlog only — admins do
# not need to scroll past process-level chatter to find moderation events.
_AUDITED_INFO_EVENTS: frozenset[str] = frozenset({
    "user_joined",
    "captcha_passed",
    "subscriptions_confirmed",
})

_AUDITED_WARNING_EVENTS: frozenset[str] = frozenset({
    "captcha_block",
    "admin_cmd_unauthorized",
    "grant_admin_denied",
    "revoke_admin_denied",
})


class ModerationAction:
    """String constants for the fixed set of moderation actions (Req 6.1)."""

    ban = "ban"
    unban = "unban"
    mute = "mute"
    unmute = "unmute"
    kick = "kick"
    grant_admin = "grant_admin"
    revoke_admin = "revoke_admin"


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    budget = limit - len(_TRUNC_SUFFIX)
    if budget <= 0:
        return value[:limit]
    return value[:budget] + _TRUNC_SUFFIX


class AuditLog:
    """Audit sink used by moderation, retries, and error paths."""

    def __init__(
        self,
        repo: AuditRepo,
        *,
        superadmin_notifier: SuperadminNotifier | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._repo = repo
        self._notifier = superadmin_notifier
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def record_moderation(
        self,
        *,
        actor_id: int,
        target_id: int,
        action: str,
        reason: str | None,
        now: datetime | None = None,
    ) -> None:
        await self._save(
            AuditRecordInput(
                level=AuditLevel.info,
                created_at=now or self._clock(),
                actor_id=actor_id,
                target_id=target_id,
                action=action,
                reason=_truncate(reason, MAX_REASON_LEN),
            )
        )

    async def record_error(
        self,
        *,
        source: str,
        message: str,
        now: datetime | None = None,
        trace_id: uuid.UUID | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        await self._save(
            AuditRecordInput(
                level=AuditLevel.error,
                created_at=now or self._clock(),
                source=source,
                message=_truncate(message, MAX_MESSAGE_LEN),
                actor_id=actor_id,
                target_id=target_id,
                trace_id=trace_id,
            )
        )

    async def record_warning(
        self,
        *,
        event: str,
        actor_id: int | None = None,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        # Skip technical warnings (i18n holes, ratelimit blocks, retry
        # diagnostics) — they belong in stdout structlog, not in the
        # admin-facing audit journal.
        if event not in _AUDITED_WARNING_EVENTS:
            log.warning("audit.skip_warning", event=event, actor_id=actor_id, details=details)
            return
        message = _format_details(event, details)
        await self._save(
            AuditRecordInput(
                level=AuditLevel.warning,
                created_at=now or self._clock(),
                actor_id=actor_id,
                action=event,
                message=_truncate(message, MAX_MESSAGE_LEN),
            )
        )

    async def record_info(
        self,
        *,
        event: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        # Same filter as ``record_warning``: only events on the whitelist
        # land in the audit journal. ``startup`` / ``audit_cleanup`` are
        # logged via structlog and stay out of the admin's view.
        if event not in _AUDITED_INFO_EVENTS:
            log.info("audit.skip_info", event=event, details=details)
            return
        message = _format_details(event, details)
        await self._save(
            AuditRecordInput(
                level=AuditLevel.info,
                created_at=now or self._clock(),
                actor_id=actor_id,
                target_id=target_id,
                action=event,
                message=_truncate(message, MAX_MESSAGE_LEN),
            )
        )

    async def list_page(self, page: int, page_size: int = 50) -> AuditPage:
        return await self._repo.list_page(page, page_size)

    # ------------------------------------------------------------------
    # Internal — retry + fallback
    # ------------------------------------------------------------------
    async def _save(self, record: AuditRecordInput) -> None:
        try:
            await with_retry(
                lambda: self._repo.insert(record),
                attempts=3,
                delays=(1.0, 1.0, 1.0),
                retry_on=db_retryable_exceptions(),
                op_name="audit.insert",
            )
        except RetryExhausted as exc:
            # Never block the caller. Log locally (structured) and try to
            # notify the superadmin out-of-band.
            log.error(
                "audit_drop",
                level=record.level.value,
                action=record.action,
                source=record.source,
                target_id=record.target_id,
                actor_id=record.actor_id,
                error=repr(exc.last_error),
            )
            if self._notifier is not None:
                try:
                    await self._notifier.notify_drop(record, exc.last_error)
                except Exception as notify_exc:
                    log.error("audit_drop_notify_failed", error=repr(notify_exc))


class SuperadminNotifier:
    """Sends out-of-band fallback notices to the first superadmin.

    Used only when audit writes fail after all retries (Req 6.6).
    """

    def __init__(self, bot: Any, first_superadmin_id: int | None) -> None:
        self._bot = bot
        self._target = first_superadmin_id

    async def notify_drop(
        self, record: AuditRecordInput, error: BaseException
    ) -> None:
        if self._target is None or self._bot is None:
            return
        text = (
            "⚠️ audit log write failed after 3 retries\n"
            f"level: {record.level.value}\n"
            f"action: {record.action or '-'}\n"
            f"source: {record.source or '-'}\n"
            f"target: {record.target_id or '-'}\n"
            f"time: {record.created_at.isoformat()}\n"
            f"error: {error!r}"
        )
        await asyncio.wait_for(
            self._bot.send_message(self._target, text), timeout=5.0
        )


def _format_details(event: str, details: dict[str, Any] | None) -> str:
    if not details:
        return event
    parts = ", ".join(f"{k}={v!r}" for k, v in details.items())
    return f"{event} :: {parts}"
