"""Модуль_Подписок — aiogram middleware that gates commands behind channel membership.

Behaviour (design § Модуль_Подписок, Req 12.1 / 12.2 / 12.3):

1. Non-:class:`Message` events (callback queries, chat-member updates, etc.)
   and non-command messages (no leading ``/``) pass through unchanged — the
   module only guards *command* invocations (Req 12.1).
2. ``/start`` and ``/help`` are whitelisted so a brand-new or confused user
   can always reach entry-point docs regardless of subscription state.
3. For any other command we call :meth:`SubscriptionChecker.check`. If it
   returns a non-empty list of :class:`~.checker.MissingChannel`:

   * The original command text (plus args) is stored in FSM under
     ``pending_cmd`` so the re-check callback can tell the user what to
     repeat once they have joined (Req 12.3).
   * A single :class:`InlineKeyboardMarkup` is sent: one "join" button per
     missing channel (URL when :attr:`MissingChannel.invite_url` is set,
     otherwise a plain callback-noop button so the name is still visible),
     followed by a "Проверить подписку" button with
     ``callback_data="subs:recheck"``.
   * The handler chain is short-circuited by returning ``None``.

The companion router in :mod:`app.features.subscriptions.handlers` handles
``subs:recheck`` and does the mirror-image work: re-run the probe; on
success read ``pending_cmd`` from FSM and ask the user to repeat it, on
failure re-render the same keyboard with an "ещё не подписаны" header.

Requirements: 12.1, 12.2, 12.3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.features.subscriptions.checker import MissingChannel, SubscriptionChecker

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiogram.fsm.context import FSMContext

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public constants — shared with the recheck router.
# ---------------------------------------------------------------------------

WHITELISTED_COMMANDS: frozenset[str] = frozenset({"/start", "/help"})
"""Commands that bypass the gate (design § Модуль_Подписок, Req 12.1)."""

RECHECK_CALLBACK_DATA = "subs:recheck"
"""``callback_data`` of the "Проверить подписку" button."""

_NOOP_CALLBACK_DATA = "subs:noop"
"""Used for channel buttons that have no ``invite_url``: clicking does
nothing — the button exists purely to surface the channel identifier."""

# ---------------------------------------------------------------------------
# Fallback strings — used when ``data["_"]`` is missing or the translator
# does not know the key. Mirror the Russian wording from design.md.
# ---------------------------------------------------------------------------

_MSG_JOIN_REQUIRED = "Для использования бота подпишитесь на каналы:"
_MSG_BUTTON_RECHECK = "Проверить подписку"


class SubscriptionsMiddleware(BaseMiddleware):
    """Outer middleware implementing Req 12.1 / 12.2 / 12.3.

    Registered by ``app/bot.py`` on ``dispatcher.message`` (callback queries
    do *not* need this middleware — the re-check callback is served by the
    router regardless of subscription state, and other callback flows
    happen after the user already passed the gate once via the originating
    message).

    The checker is supplied by the container under the
    ``feature_subscriptions`` flag; when that flag is off, neither the
    middleware nor the router are registered.
    """

    def __init__(self, checker: SubscriptionChecker) -> None:
        self._checker = checker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Subscription gate only applies to Message events with a command
        # payload. Everything else (callback queries, edited messages with
        # no text, service updates, …) passes through untouched.
        if not isinstance(event, Message):
            return await handler(event, data)

        text = (event.text or "").strip()
        if not text.startswith("/"):
            return await handler(event, data)

        command, args = _split_command(text)
        if command in WHITELISTED_COMMANDS:
            return await handler(event, data)

        from_user = event.from_user
        if from_user is None:
            # Should not happen for a command, but the checker takes a
            # concrete ``user_id`` so we cannot probe without one.
            return await handler(event, data)

        missing = await self._checker.check(from_user.id)
        if not missing:
            return await handler(event, data)

        # ----- Save pending_cmd so the recheck handler can echo it back.
        state: FSMContext | None = data.get("state")
        if state is not None:
            try:
                await state.update_data(
                    pending_cmd={"command": command, "args": args}
                )
            except Exception as exc:
                log.warning(
                    "subscriptions.middleware.fsm_save_failed",
                    user_id=from_user.id,
                    error=repr(exc),
                )

        translator = data.get("_")
        header = _translate(
            translator, "subscriptions.join_required", _MSG_JOIN_REQUIRED
        )
        keyboard = build_gate_keyboard(missing, translator)
        with suppress(Exception):
            # Telegram send can fail for reasons outside our control
            # (user blocked the bot, chat deleted, …); the middleware
            # must never raise for any update.
            await event.answer(header, reply_markup=keyboard)

        log.info(
            "subscriptions.middleware.gated",
            user_id=from_user.id,
            command=command,
            missing=[m.chat_id for m in missing],
        )
        return None


# ---------------------------------------------------------------------------
# Helpers — shared with ``handlers.py`` so the keyboard rendering stays in
# a single place.
# ---------------------------------------------------------------------------


def build_gate_keyboard(
    missing: list[MissingChannel],
    translator: Any | None = None,
) -> InlineKeyboardMarkup:
    """Render the "join + recheck" inline keyboard.

    Each missing channel becomes its own row so long titles do not get
    truncated. The final row carries the single ``subs:recheck`` button.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for channel in missing:
        label = channel.title or channel.chat_id
        if channel.invite_url:
            rows.append(
                [InlineKeyboardButton(text=label, url=channel.invite_url)]
            )
        else:
            # No URL (numeric ``-100…`` id) — we still render a button so
            # the user can copy/paste the channel identifier. Tapping it
            # is a no-op (no handler is registered for ``subs:noop``).
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label, callback_data=_NOOP_CALLBACK_DATA
                    )
                ]
            )
    recheck_text = _translate(
        translator, "subscriptions.button.recheck", _MSG_BUTTON_RECHECK
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=recheck_text, callback_data=RECHECK_CALLBACK_DATA
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _split_command(text: str) -> tuple[str, str]:
    """Split ``/cmd@bot arg1 arg2`` into ``("/cmd", "arg1 arg2")``.

    The bot-mention suffix (``@my_bot``) is dropped so the whitelist check
    works uniformly for private and group chats.
    """
    head, _, rest = text.partition(" ")
    bare = head.split("@", 1)[0]
    return bare, rest.strip()


def _translate(
    translator: Any | None,
    key: str,
    fallback: str,
    **kwargs: Any,
) -> str:
    """Translate ``key`` or return the Russian ``fallback``.

    ``Translator`` from ``app.features.i18n`` returns the key unchanged
    when the string is missing; we treat that as "use the fallback" so
    the user never sees raw dotted keys.
    """
    if translator is not None:
        try:
            value = translator(key, **kwargs)
        except Exception as exc:
            log.debug(
                "subscriptions.middleware.translate_failed",
                key=key,
                error=repr(exc),
            )
            value = key
        if value != key:
            return value
    if kwargs:
        try:
            return fallback.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return fallback
    return fallback


__all__ = [
    "RECHECK_CALLBACK_DATA",
    "WHITELISTED_COMMANDS",
    "SubscriptionsMiddleware",
    "build_gate_keyboard",
]
