"""Unit tests for :mod:`app.features.subscriptions.middleware` (task 19.2).

Covers the gating contract defined by design.md § Модуль_Подписок and
Req 12.1 / 12.2 / 12.3:

* non-:class:`Message` events pass through unchanged,
* non-command messages pass through,
* ``/start`` and ``/help`` bypass the check even when channels are missing,
* when subscriptions are satisfied the handler is invoked,
* when any channel is missing we short-circuit, send an inline keyboard
  with a "Проверить подписку" button, and save ``pending_cmd`` to FSM,
* the re-check handler re-renders on partial missing and announces the
  saved command when the user is now fully subscribed.

The tests build real :class:`aiogram.types.Message` / ``CallbackQuery``
objects via ``model_construct`` (pydantic v2 escape hatch) so we exercise
the actual type guards inside the middleware rather than mocking
``isinstance``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Message, User

from app.features.subscriptions.checker import MissingChannel
from app.features.subscriptions.middleware import (
    RECHECK_CALLBACK_DATA,
    SubscriptionsMiddleware,
    build_gate_keyboard,
)

# ---------------------------------------------------------------------------
# Lightweight stand-ins
# ---------------------------------------------------------------------------


class _StubChecker:
    """Mimics :class:`SubscriptionChecker.check` with a scripted reply queue."""

    def __init__(self, replies: list[list[MissingChannel]]) -> None:
        self._replies = list(replies)
        self.calls: list[int] = []

    async def check(self, user_id: int) -> list[MissingChannel]:
        self.calls.append(user_id)
        if not self._replies:
            return []
        return self._replies.pop(0)


class _FakeState:
    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.store: dict[str, Any] = dict(initial or {})

    async def update_data(self, **kwargs: Any) -> None:
        self.store.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        # aiogram returns a live dict reference; tests rely on this.
        return self.store

    async def set_data(self, data: dict[str, Any]) -> None:
        self.store = dict(data)


@dataclass
class _Replies:
    """Captures ``message.answer`` calls for assertions."""

    sent: list[dict[str, Any]] = field(default_factory=list)


def _make_message(
    text: str,
    *,
    user_id: int = 42,
    replies: _Replies | None = None,
) -> Message:
    """Build a real :class:`aiogram.types.Message` with a captured ``answer``."""
    tg_user = User.model_construct(id=user_id, is_bot=False, first_name="X")
    msg = Message.model_construct(
        message_id=1, date=None, chat=None, from_user=tg_user, text=text
    )
    if replies is None:
        replies = _Replies()

    async def _answer(text: str, reply_markup: Any = None, **_: Any) -> None:
        replies.sent.append({"text": text, "reply_markup": reply_markup})

    object.__setattr__(msg, "answer", _answer)
    object.__setattr__(msg, "_captured", replies)
    return msg


async def _recording_handler(event: Any, data: dict[str, Any]) -> str:
    data.setdefault("_called_with", []).append(event)
    return "handled"


# ---------------------------------------------------------------------------
# Tests — middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_message_event_passes_through() -> None:
    checker = _StubChecker(replies=[[MissingChannel(chat_id="@ch")]])
    mw = SubscriptionsMiddleware(checker)  # type: ignore[arg-type]

    class _Update:  # not a Message instance
        pass

    result = await mw(_recording_handler, _Update(), {})

    assert result == "handled"
    assert checker.calls == []


@pytest.mark.asyncio
async def test_non_command_message_is_gated() -> None:
    """Non-command text now triggers the gate (every message is checked)."""
    checker = _StubChecker(replies=[[MissingChannel(chat_id="@ch")]])
    mw = SubscriptionsMiddleware(checker)  # type: ignore[arg-type]
    replies = _Replies()
    msg = _make_message("hello", replies=replies)

    result = await mw(_recording_handler, msg, {})

    assert result is None
    assert checker.calls == [42]
    assert len(replies.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "/help",
        "/help@mybot",
        "/start ref_42",
        "/start@mybot ref_42",
    ],
)
async def test_whitelisted_commands_bypass(command: str) -> None:
    """Bypass list: /help and /start ref_<id>. Plain /start no longer bypasses."""
    checker = _StubChecker(replies=[[MissingChannel(chat_id="@ch")]])
    mw = SubscriptionsMiddleware(checker)  # type: ignore[arg-type]
    msg = _make_message(command)

    result = await mw(_recording_handler, msg, {})

    assert result == "handled"
    assert checker.calls == []


@pytest.mark.asyncio
async def test_satisfied_subscription_invokes_handler() -> None:
    checker = _StubChecker(replies=[[]])
    mw = SubscriptionsMiddleware(checker)  # type: ignore[arg-type]
    replies = _Replies()
    msg = _make_message("/profile", user_id=7, replies=replies)

    result = await mw(_recording_handler, msg, {})

    assert result == "handled"
    assert checker.calls == [7]
    assert replies.sent == []


@pytest.mark.asyncio
async def test_missing_channel_short_circuits_and_stores_pending_cmd() -> None:
    missing = [
        MissingChannel(chat_id="@news", invite_url="https://t.me/news"),
        MissingChannel(chat_id="-1001234"),  # no URL — falls back to noop button
    ]
    checker = _StubChecker(replies=[missing])
    mw = SubscriptionsMiddleware(checker)  # type: ignore[arg-type]
    replies = _Replies()
    msg = _make_message("/profile arg1 arg2", user_id=7, replies=replies)
    state = _FakeState()

    result = await mw(_recording_handler, msg, {"state": state})

    assert result is None
    assert checker.calls == [7]
    assert state.store["pending_cmd"] == {
        "command": "/profile",
        "args": "arg1 arg2",
    }
    assert len(replies.sent) == 1
    kb = replies.sent[0]["reply_markup"]
    # 2 channel rows + 1 recheck row.
    assert len(kb.inline_keyboard) == 3
    assert kb.inline_keyboard[-1][0].callback_data == RECHECK_CALLBACK_DATA
    assert kb.inline_keyboard[0][0].url == "https://t.me/news"
    assert kb.inline_keyboard[1][0].url is None


@pytest.mark.asyncio
async def test_build_gate_keyboard_preserves_ordering() -> None:
    missing = [
        MissingChannel(chat_id="@a", title="A", invite_url="https://t.me/a"),
        MissingChannel(chat_id="@b", title="B", invite_url="https://t.me/b"),
    ]
    kb = build_gate_keyboard(missing)

    assert [row[0].text for row in kb.inline_keyboard[:2]] == ["A", "B"]
    assert kb.inline_keyboard[-1][0].callback_data == RECHECK_CALLBACK_DATA


# ---------------------------------------------------------------------------
# Tests — recheck router
# ---------------------------------------------------------------------------


def _make_callback(
    user_id: int = 9,
) -> tuple[CallbackQuery, _Replies, _Replies]:
    tg_user = User.model_construct(id=user_id, is_bot=False, first_name="X")
    message = Message.model_construct(
        message_id=1, date=None, chat=None, from_user=tg_user, text=""
    )
    edits = _Replies()
    answers = _Replies()

    async def _edit(text: str, reply_markup: Any = None, **_: Any) -> None:
        edits.sent.append({"text": text, "reply_markup": reply_markup})

    object.__setattr__(message, "edit_text", _edit)

    query = CallbackQuery.model_construct(
        id="cb1",
        from_user=tg_user,
        chat_instance="",
        data=RECHECK_CALLBACK_DATA,
        message=message,
    )

    async def _answer(text: str = "", show_alert: bool = False, **_: Any) -> None:
        answers.sent.append({"text": text, "show_alert": show_alert})

    object.__setattr__(query, "answer", _answer)
    return query, answers, edits


@pytest.mark.asyncio
async def test_recheck_still_missing_rerenders() -> None:
    from app.features.subscriptions.handlers import handle_recheck

    missing = [MissingChannel(chat_id="@ch")]
    checker = _StubChecker(replies=[missing])
    query, answers, edits = _make_callback(user_id=9)
    state = _FakeState(initial={"pending_cmd": {"command": "/profile", "args": ""}})

    await handle_recheck(
        query,
        state,  # type: ignore[arg-type]
        subscriptions=checker,
    )

    assert checker.calls == [9]
    assert len(answers.sent) == 1
    assert len(edits.sent) == 1
    kb = edits.sent[0]["reply_markup"]
    assert kb.inline_keyboard[-1][0].callback_data == RECHECK_CALLBACK_DATA
    # pending_cmd left intact for a later retry.
    assert state.store.get("pending_cmd") == {"command": "/profile", "args": ""}


@pytest.mark.asyncio
async def test_recheck_confirmed_announces_saved_command() -> None:
    from app.features.subscriptions.handlers import handle_recheck

    checker = _StubChecker(replies=[[]])
    query, _answers, edits = _make_callback(user_id=9)
    state = _FakeState(
        initial={"pending_cmd": {"command": "/profile", "args": "foo"}}
    )

    await handle_recheck(
        query,
        state,  # type: ignore[arg-type]
        subscriptions=checker,
    )

    assert checker.calls == [9]
    assert len(edits.sent) == 1
    assert "/profile foo" in edits.sent[0]["text"]
    assert "pending_cmd" not in state.store
