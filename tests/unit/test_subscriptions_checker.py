"""Sanity tests for ``SubscriptionChecker`` (task 19.1).

No mocks of the Telegram API surface are required; we drive the checker
with plain fakes so Redis/audit/Bot are replaced by in-memory stand-ins.
Validates Req 12.1 / 12.4 behaviour: cache hit → no API call; valid statuses
cache; forbidden/bad-request/timeout skip the channel and audit.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest  # noqa: F401 — keeps the file pytest-discoverable


@dataclass
class _FakeRedis:
    """Very small in-memory Redis stand-in used by the checker tests."""

    store: dict[str, str]

    @classmethod
    def new(cls) -> _FakeRedis:
        return cls(store={})

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        # TTL tracking is unnecessary for the flow under test.
        self.store[key] = value

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed


class _FakeAudit:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    async def record_error(self, **kwargs: Any) -> None:
        self.errors.append(kwargs)


class _FakeBot:
    """Implements just ``get_chat_member`` with a scripted reply queue."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self._replies: dict[str, list[Any]] = defaultdict(list)

    def queue(self, channel: str, reply: Any) -> None:
        self._replies[channel].append(reply)

    async def get_chat_member(self, channel: str, user_id: int) -> Any:
        self.calls.append((channel, user_id))
        queue = self._replies.get(channel, [])
        if not queue:
            raise AssertionError(f"no reply queued for {channel}")
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply() if asyncio.iscoroutinefunction(reply) else reply()
        return reply


def _member(status: str) -> Any:
    return SimpleNamespace(status=status)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_empty_required_channels_returns_empty() -> None:
    from app.features.subscriptions import SubscriptionChecker

    bot = _FakeBot()
    redis = _FakeRedis.new()
    audit = _FakeAudit()
    checker = SubscriptionChecker(bot, redis, audit, [])

    assert _run(checker.check(42)) == []
    assert bot.calls == []
    assert audit.errors == []


def test_cache_hit_skips_api_call() -> None:
    from app.features.subscriptions import SubscriptionChecker

    bot = _FakeBot()
    redis = _FakeRedis.new()
    redis.store["subs:ok:42:@news"] = "1"
    audit = _FakeAudit()
    checker = SubscriptionChecker(bot, redis, audit, ["@news"])

    assert _run(checker.check(42)) == []
    assert bot.calls == []  # cache hit — no API call
    assert audit.errors == []


def test_member_status_is_cached_as_subscribed() -> None:
    from app.features.subscriptions import SubscriptionChecker

    bot = _FakeBot()
    bot.queue("@news", _member("member"))
    redis = _FakeRedis.new()
    audit = _FakeAudit()
    checker = SubscriptionChecker(bot, redis, audit, ["@news"])

    assert _run(checker.check(42)) == []
    assert bot.calls == [("@news", 42)]
    assert redis.store["subs:ok:42:@news"] == "1"
    assert audit.errors == []


def test_left_status_returns_missing_channel() -> None:
    from app.features.subscriptions import SubscriptionChecker
    from app.features.subscriptions.checker import MissingChannel

    bot = _FakeBot()
    bot.queue("@news", _member("left"))
    redis = _FakeRedis.new()
    audit = _FakeAudit()
    checker = SubscriptionChecker(bot, redis, audit, ["@news"])

    result = _run(checker.check(42))
    assert result == [
        MissingChannel(
            chat_id="@news", title=None, invite_url="https://t.me/news"
        )
    ]
    # Negative outcomes must not be cached — user may subscribe momentarily.
    assert "subs:ok:42:@news" not in redis.store
    assert audit.errors == []


def test_forbidden_error_is_skipped_and_audited() -> None:
    from aiogram.exceptions import TelegramForbiddenError

    from app.features.subscriptions import SubscriptionChecker

    bot = _FakeBot()
    # aiogram's TelegramForbiddenError requires positional args in some versions;
    # construct defensively so the test is robust across minor aiogram bumps.
    try:
        err = TelegramForbiddenError(method=None, message="bot kicked")  # type: ignore[arg-type]
    except TypeError:
        err = TelegramForbiddenError("bot kicked")  # type: ignore[call-arg]
    bot.queue("@news", err)
    redis = _FakeRedis.new()
    audit = _FakeAudit()
    checker = SubscriptionChecker(bot, redis, audit, ["@news"])

    # Req 12.4 — no missing entry returned, error recorded in audit.
    assert _run(checker.check(42)) == []
    assert len(audit.errors) == 1
    assert audit.errors[0]["source"] == "Telegram API"


def test_timeout_is_skipped_and_audited() -> None:
    from app.features.subscriptions import SubscriptionChecker

    bot = _FakeBot()

    async def never() -> Any:
        await asyncio.sleep(10)

    bot.queue("@news", never)
    redis = _FakeRedis.new()
    audit = _FakeAudit()
    # Force a sub-second timeout so the test stays fast.
    checker = SubscriptionChecker(
        bot, redis, audit, ["@news"], get_chat_member_timeout_seconds=0.05
    )

    assert _run(checker.check(42)) == []
    assert len(audit.errors) == 1
    assert audit.errors[0]["source"] == "Telegram API"
    assert "timeout" in audit.errors[0]["message"]


def test_preserves_channel_order_and_deduplicates() -> None:
    from app.features.subscriptions import SubscriptionChecker

    bot = _FakeBot()
    bot.queue("@a", _member("member"))
    bot.queue("@b", _member("left"))
    redis = _FakeRedis.new()
    audit = _FakeAudit()
    checker = SubscriptionChecker(bot, redis, audit, ["@a", "@b", "@a"])

    result = _run(checker.check(42))
    # "@a" deduplicated; ordering preserved; only "@b" is missing.
    assert [m.chat_id for m in result] == ["@b"]
    assert bot.calls == [("@a", 42), ("@b", 42)]
