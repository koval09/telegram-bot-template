"""Модуль_Подписок — aiogram middleware that gates updates behind channel membership.

Behaviour (design § Модуль_Подписок, Req 12.1 / 12.2 / 12.3):

1. Bot-side bootstrap traffic always passes through unchanged:

   * ``/start`` (with or without a ``ref_<id>`` payload) — the entry-point
     command. Blocking it would prevent registration, including referral
     attribution.
   * ``/help``.
   * The captcha callback (``callback_data`` starting with ``cap:``) so
     the user can actually solve the captcha.
   * The recheck callback (``subs:recheck``) — that *is* the way to
     escape the gate.

2. For every other update from a user with at least one
   :class:`MissingChannel` the middleware:

   * If it is a :class:`Message` with text starting with ``/``, stores
     ``pending_cmd`` in FSM so the re-check callback can tell the user
     what to repeat once they have joined (Req 12.3).
   * Sends a single :class:`InlineKeyboardMarkup`: one "join" button per
     missing channel (URL when :attr:`MissingChannel.invite_url` is set,
     otherwise a callback-noop button so the channel identifier is still
     visible), followed by a "Проверить подписку" button with
     ``callback_data="subs:recheck"``.
   * Short-circuits the handler chain by returning ``None``.

The companion router in :mod:`app.features.subscriptions.handlers` handles
``subs:recheck`` and does the mirror-image work: re-run the probe; on
success read ``pending_cmd`` from FSM and announce, on failure re-render
the same keyboard with an "ещё не подписаны" header.

Requirements: 12.1, 12.2, 12.3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
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

WHITELISTED_COMMANDS: frozenset[str] = frozenset({"/help"})
"""Commands that bypass the gate unconditionally. ``/start`` no longer sits
here on its own — only ``/start ref_<id>`` is bypassed, and that exception
is checked dynamically inside :func:`_is_bypass_event`."""

RECHECK_CALLBACK_DATA = "subs:recheck"
"""``callback_data`` of the "Проверить подписку" button."""

CAPTCHA_CALLBACK_PREFIX = "cap:"
"""``callback_data`` prefix used by the captcha service buttons."""

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

    Registered by ``app/bot.py`` on both ``dispatcher.message`` and
    ``dispatcher.callback_query`` so every interaction (text, slash
    command, button press) is gated until the user joins every required
    channel. Bootstrap traffic — ``/start``, ``/help``, the captcha
    callback and the recheck button itself — bypasses the gate so
    registration and gate-clearing flows still work.

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
        if _is_bypass_event(event):
            return await handler(event, data)

        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return await handler(event, data)

        missing = await self._checker.check(from_user.id)
        if not missing:
            return await handler(event, data)

        # ----- Save pending_cmd so the recheck handler can echo it back
        # for slash-command messages. Free-form text and callback queries
        # do not need replay — the user can repeat the action manually.
        state: FSMContext | None = data.get("state")
        if state is not None and isinstance(event, Message):
            text = (event.text or "").strip()
            if text.startswith("/"):
                command, args = _split_command(text)
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
            if isinstance(event, Message):
                await event.answer(header, reply_markup=keyboard)
            elif isinstance(event, CallbackQuery):
                # Acknowledge the spinner and surface the gate in the
                # originating chat so the user sees the same UI as for
                # text-driven gating.
                await event.answer()
                if event.message is not None:
                    await event.message.answer(header, reply_markup=keyboard)

        log.info(
            "subscriptions.middleware.gated",
            user_id=from_user.id,
            event_kind=type(event).__name__,
            missing=[m.chat_id for m in missing],
        )
        return None


# ---------------------------------------------------------------------------
# Bypass logic — bootstrap traffic that must reach handlers regardless of
# subscription state.
# ---------------------------------------------------------------------------


def _is_bypass_event(event: TelegramObject) -> bool:
    """Return True when the gate must let the event through unchanged.

    Bypass list:

    * captcha button presses (``cap:*``) — the user must be able to answer
      the captcha even before subscribing;
    * the recheck button (``subs:recheck``) — that *is* the way out of the
      gate;
    * ``/help`` — minimal docs must always be reachable;
    * ``/start ref_<digits>`` — the referral entry-point. The bot has to
      see the ``ref_<id>`` payload at least once to record ``referrer_id``;
      after that the user goes through the full gate on every other
      message, including a plain ``/start``.

    Plain ``/start`` (without the ``ref_…`` payload) is intentionally
    NOT in the bypass list — it should also trigger the gate so users
    cannot keep re-running ``/start`` to skip the subscription requirement.
    """
    if isinstance(event, CallbackQuery):
        data = event.data or ""
        return data == RECHECK_CALLBACK_DATA or data.startswith(
            CAPTCHA_CALLBACK_PREFIX
        )
    if isinstance(event, Message):
        text = (event.text or "").strip()
        if not text.startswith("/"):
            return False
        command, args = _split_command(text)
        if command == "/help":
            return True
        if command == "/start" and args.startswith("ref_"):
            return True
        return False
    return False


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
