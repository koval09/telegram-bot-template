"""aiogram filters gating access to admin routers.

Requirements 4.2 / 4.3:
- The filter returns False for non-admins so the dispatcher continues to
  the next handler. An unmatched-command fallback then replies with the
  generic "command not found" message, making the admin path
  indistinguishable from a typo for outsiders.
- On every denial we log a warning in Журнал_Действий.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

from app.admin.authorization import Authorization
from app.core.services.audit import AuditLog


class IsAdminFilter(BaseFilter):
    async def __call__(
        self,
        event: Message,
        authorization: Authorization,
        audit: AuditLog,
    ) -> bool | dict[str, Any]:
        if event.from_user is None:
            return False
        if await authorization.is_admin(event.from_user.id):
            return True
        # Unauthorized attempt — log and deny silently.
        text = (event.text or event.caption or "")[:200]
        await audit.record_warning(
            event="admin_cmd_unauthorized",
            actor_id=event.from_user.id,
            details={"cmd": text},
        )
        return False


class IsSuperadminFilter(BaseFilter):
    async def __call__(
        self,
        event: Message,
        authorization: Authorization,
        audit: AuditLog,
    ) -> bool | dict[str, Any]:
        if event.from_user is None:
            return False
        if await authorization.is_superadmin(event.from_user.id):
            return True
        text = (event.text or event.caption or "")[:200]
        await audit.record_warning(
            event="superadmin_cmd_unauthorized",
            actor_id=event.from_user.id,
            details={"cmd": text},
        )
        return False
