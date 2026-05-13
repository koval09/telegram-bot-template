"""Модуль_Авторизации — role resolution with Redis cache and env seed.

Requirements:
- 4.1 — admin is a user whose role is ``admin`` or ``superadmin``.
- 4.2 — role checked before executing any admin command.
- 4.4 — initial superadmins are trusted only if provided via env.
- 4.5 — superadmin can grant ``admin`` role to other users.
- 4.6 — ordinary admins cannot grant/revoke admin roles.
- 4.7 — role cache invalidated within 5 seconds of a role change.
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis

from app.core.db.models import UserRole
from app.core.repositories.users import UsersRepo

log = structlog.get_logger(__name__)

ROLE_CACHE_PREFIX = "auth:role:"
ROLE_CACHE_TTL = 60  # seconds — well under the 5-second invalidation SLA


class NotAdminError(Exception):
    """Raised when a non-admin triggers an admin-only path programmatically."""


class NotSuperadminError(Exception):
    """Raised when a non-superadmin triggers a superadmin-only path."""


class Authorization:
    def __init__(self, redis: Redis, users: UsersRepo) -> None:
        self._redis = redis
        self._users = users

    # ------------------------------------------------------------------
    # Cache-aside lookup
    # ------------------------------------------------------------------
    async def get_role(self, telegram_id: int) -> UserRole:
        key = f"{ROLE_CACHE_PREFIX}{telegram_id}"
        try:
            cached = await self._redis.get(key)
        except Exception as exc:
            log.warning("auth.cache_read_failed", error=repr(exc))
            cached = None
        if cached is not None:
            try:
                return UserRole(cached)
            except ValueError:
                # Corrupted cache value — ignore and fall through to DB.
                pass

        user = await self._users.get_by_tg_id(telegram_id)
        role = user.role if user is not None else UserRole.user

        try:
            await self._redis.set(key, role.value, ex=ROLE_CACHE_TTL)
        except Exception as exc:
            log.warning("auth.cache_write_failed", error=repr(exc))

        return role

    async def is_admin(self, telegram_id: int) -> bool:
        return (await self.get_role(telegram_id)) in (UserRole.admin, UserRole.superadmin)

    async def is_superadmin(self, telegram_id: int) -> bool:
        return (await self.get_role(telegram_id)) is UserRole.superadmin

    async def require_admin(self, telegram_id: int) -> UserRole:
        role = await self.get_role(telegram_id)
        if role not in (UserRole.admin, UserRole.superadmin):
            raise NotAdminError
        return role

    async def require_superadmin(self, telegram_id: int) -> UserRole:
        role = await self.get_role(telegram_id)
        if role is not UserRole.superadmin:
            raise NotSuperadminError
        return role

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    async def invalidate(self, telegram_id: int) -> None:
        """Drop the cached role entry so the next read hits the DB (Req 4.7)."""
        try:
            await self._redis.delete(f"{ROLE_CACHE_PREFIX}{telegram_id}")
        except Exception as exc:
            log.warning("auth.invalidate_failed", error=repr(exc))

    async def grant_admin(self, target_id: int) -> bool:
        """Promote user to ``admin`` (no-op if already admin/superadmin)."""
        current = await self.get_role(target_id)
        if current in (UserRole.admin, UserRole.superadmin):
            return False
        changed = await self._users.set_role(target_id, UserRole.admin)
        if changed:
            await self.invalidate(target_id)
        return changed

    async def revoke_admin(self, target_id: int) -> bool:
        """Demote a plain ``admin`` back to ``user``; superadmin is not touched."""
        current = await self.get_role(target_id)
        if current is not UserRole.admin:
            return False
        changed = await self._users.set_role(target_id, UserRole.user)
        if changed:
            await self.invalidate(target_id)
        return changed

    # ------------------------------------------------------------------
    # Seed — env-provided superadmins (Req 4.4)
    # ------------------------------------------------------------------
    async def seed_superadmins(self, superadmin_ids: list[int]) -> None:
        """Ensure every id from env is marked as ``superadmin``.

        Called ONCE at startup. Creates a placeholder user row for each id
        that does not yet exist in the DB so that ``role = superadmin`` is
        persisted even before the user ever messages the bot.
        """
        if not superadmin_ids:
            return
        from app.core.repositories.users import TgUserData
        from app.core.utils.clock import utc_now

        now = utc_now()
        for tg_id in superadmin_ids:
            existing = await self._users.get_by_tg_id(tg_id)
            if existing is None:
                await self._users.upsert_from_tg(
                    TgUserData(
                        telegram_id=tg_id,
                        username=None,
                        first_name=None,
                        last_name=None,
                        language_code=None,
                    ),
                    now=now,
                )
            if (existing is None) or existing.role is not UserRole.superadmin:
                await self._users.set_role(tg_id, UserRole.superadmin)
                await self.invalidate(tg_id)
                log.info("auth.superadmin_seeded", telegram_id=tg_id)
