"""Модуль_Подписок — callback router for the "Проверить подписку" button.

The middleware (``middleware.py``) blocks commands when the user is not
subscribed and presents an inline keyboard with
``callback_data="subs:recheck"``. This router reacts to that button.

Contract (design § Модуль_Подписок, Req 12.3):

* Re-run :meth:`SubscriptionChecker.check` for ``query.from_user.id``.
* If the user is now subscribed to every required channel:
    - read ``pending_cmd`` from FSM and clear it,
    - answer the callback, edit the keyboard message to a "confirmed"
      acknowledgement that tells the user which command to repeat. We
      deliberately do *not* re-dispatch the stored command: aiogram's
      re-dispatch primitives (``dispatcher.feed_update`` /
      ``dispatcher.propagate_event``) would bypass the registration
      middleware's ``last_seen_at`` update and the antispam rate-limit
      counters, so asking the user to retype is both simpler and keeps
      every other middleware honest.
* If any channel is still missing, re-render the keyboard with a new
  header noting the remaining channels. The ``pending_cmd`` entry is left
  intact so a later retry still works.

Requirement 12.3.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.features.subscriptions.checker import SubscriptionChecker
from app.features.subscriptions.middleware import (
    RECHECK_CALLBACK_DATA,
    _translate,
    build_gate_keyboard,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Fallback strings (see middleware.py — ``data["_"]`` is preferred when
# available, these act as Russian defaults).
# ---------------------------------------------------------------------------

_MSG_STILL_MISSING = "Вы ещё не подписаны на: {channels}"
_MSG_CONFIRMED_WITH_CMD = (
    "Подписка подтверждена. Повторите команду: {command}"
)
_MSG_CONFIRMED_NO_CMD = "Подписка подтверждена."
_MSG_TOAST_OK = "Подписка подтверждена"
_MSG_TOAST_MISSING = "Всё ещё не хватает подписок"


router = Router(name="subscriptions.recheck")


@router.callback_query(F.data == RECHECK_CALLBACK_DATA)
async def handle_recheck(
    query: CallbackQuery,
    state: FSMContext,
    subscriptions: SubscriptionsServices | SubscriptionChecker,  # injected by container
    **data: Any,
) -> None:
    """Callback handler for the "Проверить подписку" button (Req 12.3)."""
    translator = data.get("_")
    checker = _resolve_checker(subscriptions)

    user = query.from_user
    if user is None or checker is None:
        # No user to probe / no checker wired → dismiss silently so the
        # spinner clears.
        with suppress(Exception):
            await query.answer()
        return

    missing = await checker.check(user.id)

    if missing:
        await _render_still_missing(query, missing, translator)
        log.info(
            "subscriptions.recheck.still_missing",
            user_id=user.id,
            missing=[m.chat_id for m in missing],
        )
        return

    # All subscriptions confirmed — recover ``pending_cmd`` and announce.
    pending_cmd: dict[str, Any] | None = None
    with suppress(Exception):
        stored = await state.get_data()
        candidate = stored.get("pending_cmd") if isinstance(stored, dict) else None
        if isinstance(candidate, dict) and candidate.get("command"):
            pending_cmd = candidate
        # Clear the pending_cmd key regardless of whether we recovered a
        # valid entry so stale data does not linger across sessions.
        if "pending_cmd" in stored:
            stored.pop("pending_cmd", None)
            await state.set_data(stored)

    # Antifraud refinement on Req 7.2: a confirmed subscription is the
    # second eligibility gate for referral crediting. Try to settle the
    # credit now — the service is exactly-once and short-circuits when
    # the captcha gate is still pending or the credit has already been
    # applied. Failures are isolated so a recheck cannot crash because
    # of a downstream issue.
    referrals_bundle = data.get("referrals")
    if referrals_bundle is not None and getattr(referrals_bundle, "crediting", None) is not None:
        try:
            outcome = await referrals_bundle.crediting.try_credit(user.id)
        except Exception as exc:
            log.warning(
                "subscriptions.recheck.credit_failed",
                user_id=user.id,
                error=repr(exc),
            )
        else:
            if outcome.credited:
                log.info(
                    "subscriptions.recheck.credit_settled",
                    user_id=user.id,
                    inviter_id=outcome.inviter_id,
                )

    await _render_confirmed(query, pending_cmd, translator)
    log.info(
        "subscriptions.recheck.confirmed",
        user_id=user.id,
        pending_cmd=(pending_cmd or {}).get("command"),
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


async def _render_still_missing(
    query: CallbackQuery,
    missing: list,
    translator: Any | None,
) -> None:
    channel_list = ", ".join(
        (channel.title or channel.chat_id) for channel in missing
    )
    header = _translate(
        translator,
        "subscriptions.still_missing",
        _MSG_STILL_MISSING,
        channels=channel_list,
    )
    toast = _translate(
        translator, "subscriptions.toast.missing", _MSG_TOAST_MISSING
    )
    with suppress(Exception):
        await query.answer(toast, show_alert=False)
    target = query.message
    if target is None:
        return
    keyboard = build_gate_keyboard(missing, translator)
    with suppress(Exception):
        await target.edit_text(header, reply_markup=keyboard)


async def _render_confirmed(
    query: CallbackQuery,
    pending_cmd: dict[str, Any] | None,
    translator: Any | None,
) -> None:
    if pending_cmd and pending_cmd.get("command"):
        command = pending_cmd["command"]
        args = pending_cmd.get("args") or ""
        full = f"{command} {args}".strip()
        text = _translate(
            translator,
            "subscriptions.confirmed_with_cmd",
            _MSG_CONFIRMED_WITH_CMD,
            command=full,
        )
    else:
        text = _translate(
            translator, "subscriptions.confirmed", _MSG_CONFIRMED_NO_CMD
        )
    toast = _translate(translator, "subscriptions.toast.ok", _MSG_TOAST_OK)
    with suppress(Exception):
        await query.answer(toast, show_alert=False)
    target = query.message
    if target is None:
        return
    # Clear the keyboard so the user cannot re-click a stale button.
    with suppress(Exception):
        await target.edit_text(text)


# ---------------------------------------------------------------------------
# Wiring helper
# ---------------------------------------------------------------------------


def _resolve_checker(value: Any) -> Any | None:
    """Accept either the bundle (``SubscriptionsServices``) or a raw checker.

    ``dispatcher["subscriptions"]`` is populated by :func:`app.bot.register_routers`
    with :class:`~.SubscriptionsServices`; pulling ``.checker`` off it keeps the
    handler a one-liner. The fallback branch accepts anything exposing an
    awaitable ``check(user_id)`` — useful for unit tests that supply a
    lightweight stub instead of the full checker.
    """
    if value is None:
        return None
    # Prefer the bundle attribute if present; otherwise trust a duck-typed
    # object with a ``check`` coroutine.
    candidate = getattr(value, "checker", value)
    if callable(getattr(candidate, "check", None)):
        return candidate
    return None


# ---------------------------------------------------------------------------
# TYPE_CHECKING-only imports kept out of runtime to avoid a circular import
# (the bundle lives in ``__init__.py`` which imports this module).
# ---------------------------------------------------------------------------

from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from app.features.subscriptions import SubscriptionsServices


__all__ = ["router"]
