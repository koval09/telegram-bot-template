"""Модуль_Рассылки — producer side of the broadcast feature (task 22.1).

Implements the admin-facing ``/broadcast`` FSM dialog and the
``/broadcast_cancel <id>`` command. Both sides (producer here, worker in
task 22.2) talk through two Redis keys:

Queue JSON contract
-------------------
``LPUSH bcast:queue <json>`` where ``<json>`` is a UTF-8 JSON object with
the following shape::

    {
      "id": "<uuid4>",              # job id, also used for cancel flag
      "created_by": <int>,          # Telegram id of the admin who started it
      "text": "<str, 1..4096>",     # the message text to send
      "filter": {
          "kind": "all" | "active_30d" | "lang",
          "value": <str | null>     # language code for "lang", null otherwise
      },
      "created_at": "<iso8601 UTC>"
    }

Cancellation
------------
``/broadcast_cancel <uuid>`` writes ``SET bcast:cancel:<id> 1 EX 3600``
(Req 8.6). The worker must check that key before each send and stop
within 5 seconds when it is present.

Queue backpressure
------------------
At entry to ``/broadcast`` we check ``LLEN bcast:queue``. If the queue
already holds 10 jobs we refuse politely (Req 8.5) — otherwise ``LPUSH``
would keep growing it unboundedly.

Requirements: 8.1 (producer posting jobs), 8.5 (queue cap), 8.6 (cancel flag).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis

from app.admin.filters import IsAdminFilter
from app.config import Settings

log = structlog.get_logger(__name__)

router = Router(name="broadcasts.producer")

# ---------------------------------------------------------------------------
# Queue / cancel key constants — shared with the worker (task 22.2).
# ---------------------------------------------------------------------------

QUEUE_KEY = "bcast:queue"
CANCEL_KEY_PREFIX = "bcast:cancel:"
MAX_QUEUE_LEN = 10
MIN_TEXT_LEN = 1
MAX_TEXT_LEN = 4096
CANCEL_TTL_SECONDS = 3600

# Filter ``kind`` values — also used by the worker. Kept as plain strings
# so the JSON stays friendly to external inspectors.
FILTER_ALL = "all"
FILTER_ACTIVE_30D = "active_30d"
FILTER_LANG = "lang"

# Confirmation callback payloads.
CONFIRM_CALLBACK = "bcast:confirm"
CANCEL_CALLBACK = "bcast:cancel"

# BCP-47 primary subtag (2–8 chars, letters only). Matches what we store in
# ``users.language_code`` after i18n normalisation.
_LANG_CODE_RE = re.compile(r"^[a-zA-Z]{2,8}$")


class BroadcastStates(StatesGroup):
    """FSM states for the ``/broadcast`` dialog."""

    waiting_text = State()
    waiting_filter = State()
    confirming = State()


# ---------------------------------------------------------------------------
# Russian fallbacks (mirrored in ``app/locales/{ru,en}.yml`` under
# ``broadcasts.*``). We follow the same "translator returns key on miss"
# convention as the referrals/subscriptions handlers.
# ---------------------------------------------------------------------------

_MSG_QUEUE_FULL = "Очередь переполнена ({n}/{cap}). Попробуйте позже."
_MSG_PROMPT_TEXT = (
    "Введите текст рассылки (от {min} до {max} символов)."
    " Отправьте /cancel чтобы отменить."
)
_MSG_TEXT_INVALID = (
    "Текст должен быть от {min} до {max} символов. Попробуйте ещё раз."
)
_MSG_PROMPT_FILTER = (
    "Выберите фильтр получателей:\n"
    "• <code>all</code> — все пользователи\n"
    "• <code>active_30d</code> — активные за 30 дней\n"
    "• <code>lang:&lt;код&gt;</code> — по языку (например, <code>lang:ru</code>)"
)
_MSG_FILTER_INVALID = (
    "Неверный фильтр. Допустимо: <code>all</code>, <code>active_30d</code>,"
    " <code>lang:&lt;код&gt;</code>."
)
_MSG_FILTER_LANG_UNSUPPORTED = (
    "Язык {code} не поддерживается. Поддерживаемые: {supported}."
)
_MSG_PREVIEW = (
    "<b>Подтвердите рассылку</b>\n"
    "Фильтр: <code>{filter}</code>\n"
    "Текст ({length}):\n{text}"
)
_MSG_BTN_YES = "Отправить"
_MSG_BTN_NO = "Отмена"
_MSG_ENQUEUED = "Задача добавлена в очередь (id={id}). Позиция: {pos}"
_MSG_CANCELLED = "Рассылка отменена."
_MSG_CANCEL_USAGE = "Использование: /broadcast_cancel &lt;uuid&gt;"
_MSG_CANCEL_BAD_ID = "Некорректный идентификатор рассылки."
_MSG_CANCEL_OK = "Флаг отмены установлен для рассылки {id}."


# ---------------------------------------------------------------------------
# /broadcast entry — admin-only, gated by queue length (Req 8.5).
# ---------------------------------------------------------------------------


@router.message(Command("broadcast"), IsAdminFilter())
async def handle_broadcast_entry(
    message: Message,
    state: FSMContext,
    redis: Redis,
    **data: Any,
) -> None:
    """Start the FSM dialog for creating a new broadcast job."""
    translator = data.get("_")
    # Always start from a clean slate so a previously abandoned dialog
    # does not confuse state handlers below.
    await state.clear()

    queue_len = await _safe_llen(redis, QUEUE_KEY)
    if queue_len >= MAX_QUEUE_LEN:
        text = _translate(
            translator,
            "broadcasts.queue_full",
            _MSG_QUEUE_FULL,
            n=queue_len,
            cap=MAX_QUEUE_LEN,
        )
        await message.answer(text)
        log.info(
            "broadcasts.producer.queue_full",
            queue_len=queue_len,
            actor_id=message.from_user.id if message.from_user else None,
        )
        return

    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        _translate(
            translator,
            "broadcasts.prompt_text",
            _MSG_PROMPT_TEXT,
            min=MIN_TEXT_LEN,
            max=MAX_TEXT_LEN,
        )
    )


# ---------------------------------------------------------------------------
# waiting_text — accept plain text of 1..4096 chars.
# ---------------------------------------------------------------------------


@router.message(StateFilter(BroadcastStates.waiting_text), IsAdminFilter())
async def handle_waiting_text(
    message: Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Persist the text in FSM data and move on to the filter step."""
    translator = data.get("_")
    text = (message.text or "").strip()
    if not (MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN):
        await message.answer(
            _translate(
                translator,
                "broadcasts.text_invalid",
                _MSG_TEXT_INVALID,
                min=MIN_TEXT_LEN,
                max=MAX_TEXT_LEN,
            )
        )
        return

    await state.update_data(text=text)
    await state.set_state(BroadcastStates.waiting_filter)
    await message.answer(
        _translate(translator, "broadcasts.prompt_filter", _MSG_PROMPT_FILTER)
    )


# ---------------------------------------------------------------------------
# waiting_filter — parse ``all`` / ``active_30d`` / ``lang:<code>``.
# ---------------------------------------------------------------------------


@router.message(StateFilter(BroadcastStates.waiting_filter), IsAdminFilter())
async def handle_waiting_filter(
    message: Message,
    state: FSMContext,
    settings: Settings,
    **data: Any,
) -> None:
    """Validate the filter string, show a preview and await confirmation."""
    translator = data.get("_")
    raw = (message.text or "").strip()
    parsed = _parse_filter(raw, settings.supported_locales)
    if parsed is None:
        await message.answer(
            _translate(
                translator, "broadcasts.filter_invalid", _MSG_FILTER_INVALID
            )
        )
        return
    if parsed == "lang_unsupported":
        # Keep the user on the same step so they can retry.
        await message.answer(
            _translate(
                translator,
                "broadcasts.filter_lang_unsupported",
                _MSG_FILTER_LANG_UNSUPPORTED,
                code=raw.split(":", 1)[1],
                supported=", ".join(settings.supported_locales),
            )
        )
        return

    kind, value = parsed
    await state.update_data(filter_kind=kind, filter_value=value)
    await state.set_state(BroadcastStates.confirming)

    saved = await state.get_data()
    text = saved.get("text", "")
    preview_filter = kind if value is None else f"{kind}:{value}"
    await message.answer(
        _translate(
            translator,
            "broadcasts.preview",
            _MSG_PREVIEW,
            filter=preview_filter,
            length=len(text),
            text=text,
        ),
        reply_markup=_confirm_keyboard(translator),
    )


# ---------------------------------------------------------------------------
# confirming — inline Yes/No callbacks.
# ---------------------------------------------------------------------------


@router.callback_query(
    StateFilter(BroadcastStates.confirming),
    lambda cq: cq.data == CONFIRM_CALLBACK,
)
async def handle_confirm_yes(
    query: CallbackQuery,
    state: FSMContext,
    redis: Redis,
    **data: Any,
) -> None:
    """Serialize the job, push it to Redis and reply with position."""
    translator = data.get("_")
    if query.from_user is None:
        await query.answer()
        return

    saved = await state.get_data()
    text: str = saved.get("text", "")
    kind: str = saved.get("filter_kind", FILTER_ALL)
    value: str | None = saved.get("filter_value")

    # Defensive: if the dialog was somehow restarted between steps the
    # text may be missing. Abort the confirmation rather than enqueue an
    # empty job.
    if not text or not (MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN):
        await state.clear()
        await query.answer()
        if query.message:
            await query.message.answer(
                _translate(
                    translator,
                    "broadcasts.text_invalid",
                    _MSG_TEXT_INVALID,
                    min=MIN_TEXT_LEN,
                    max=MAX_TEXT_LEN,
                )
            )
        return

    job = {
        "id": str(uuid.uuid4()),
        "created_by": query.from_user.id,
        "text": text,
        "filter": {"kind": kind, "value": value},
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"))

    try:
        position = await redis.lpush(QUEUE_KEY, payload)
    except Exception as exc:
        log.error(
            "broadcasts.producer.enqueue_failed",
            error=repr(exc),
            actor_id=query.from_user.id,
        )
        await state.clear()
        await query.answer()
        if query.message:
            await query.message.answer(
                _translate(
                    translator, "broadcasts.queue_error",
                    "Не удалось поставить задачу в очередь. Попробуйте позже.",
                )
            )
        return

    await state.clear()

    reply_text = _translate(
        translator,
        "broadcasts.enqueued",
        _MSG_ENQUEUED,
        id=job["id"],
        pos=position,
    )
    if query.message:
        await query.message.answer(reply_text)
    await query.answer()
    log.info(
        "broadcasts.producer.enqueued",
        job_id=job["id"],
        actor_id=query.from_user.id,
        filter_kind=kind,
        filter_value=value,
        position=position,
        text_length=len(text),
    )


@router.callback_query(
    StateFilter(BroadcastStates.confirming),
    lambda cq: cq.data == CANCEL_CALLBACK,
)
async def handle_confirm_no(
    query: CallbackQuery,
    state: FSMContext,
    **data: Any,
) -> None:
    """User backed out of the preview — drop the FSM state."""
    translator = data.get("_")
    await state.clear()
    if query.message:
        await query.message.answer(
            _translate(translator, "broadcasts.cancelled", _MSG_CANCELLED)
        )
    await query.answer()


# ---------------------------------------------------------------------------
# /broadcast_cancel <id> — sets the cancel flag for the worker (Req 8.6).
# ---------------------------------------------------------------------------


@router.message(Command("broadcast_cancel"), IsAdminFilter())
async def handle_broadcast_cancel(
    message: Message,
    command: CommandObject,
    redis: Redis,
    **data: Any,
) -> None:
    translator = data.get("_")
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            _translate(
                translator, "broadcasts.cancel_usage", _MSG_CANCEL_USAGE
            )
        )
        return

    # Validate UUID shape so a typo does not paint the keyspace with junk.
    try:
        job_id = str(uuid.UUID(raw))
    except ValueError:
        await message.answer(
            _translate(
                translator, "broadcasts.cancel_bad_id", _MSG_CANCEL_BAD_ID
            )
        )
        return

    try:
        await redis.set(
            f"{CANCEL_KEY_PREFIX}{job_id}", "1", ex=CANCEL_TTL_SECONDS
        )
    except Exception as exc:
        log.error(
            "broadcasts.producer.cancel_failed",
            error=repr(exc),
            job_id=job_id,
        )
        await message.answer(
            _translate(
                translator,
                "broadcasts.cancel_error",
                "Не удалось установить флаг отмены. Попробуйте позже.",
            )
        )
        return

    await message.answer(
        _translate(
            translator, "broadcasts.cancel_ok", _MSG_CANCEL_OK, id=job_id
        )
    )
    log.info(
        "broadcasts.producer.cancel_flag_set",
        job_id=job_id,
        actor_id=message.from_user.id if message.from_user else None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_filter(
    raw: str, supported_locales: list[str]
) -> tuple[str, str | None] | str | None:
    """Parse a filter string.

    Returns:
        ``(kind, value)`` tuple on success;
        ``"lang_unsupported"`` when the shape is ``lang:<code>`` but the
        code is not listed in ``supported_locales`` (caller reprompts);
        ``None`` when the string does not match any known filter form.
    """
    lowered = raw.strip().lower()
    if lowered == FILTER_ALL:
        return (FILTER_ALL, None)
    if lowered == FILTER_ACTIVE_30D:
        return (FILTER_ACTIVE_30D, None)
    if lowered.startswith("lang:"):
        code = lowered.split(":", 1)[1].strip()
        if not _LANG_CODE_RE.match(code):
            return None
        # Compare case-insensitively against the configured locales so
        # users can type ``lang:EN`` and still hit the ``en`` locale.
        normalised = code.lower()
        matching = next(
            (loc for loc in supported_locales if loc.lower() == normalised),
            None,
        )
        if matching is None:
            return "lang_unsupported"
        return (FILTER_LANG, matching)
    return None


def _confirm_keyboard(translator: Any | None) -> InlineKeyboardMarkup:
    yes = _translate(translator, "broadcasts.button.yes", _MSG_BTN_YES)
    no = _translate(translator, "broadcasts.button.no", _MSG_BTN_NO)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=yes, callback_data=CONFIRM_CALLBACK),
                InlineKeyboardButton(text=no, callback_data=CANCEL_CALLBACK),
            ]
        ]
    )


async def _safe_llen(redis: Redis, key: str) -> int:
    """``LLEN`` that falls back to 0 on transient Redis errors.

    Returning ``0`` here is safe: the worst case is a single extra job
    slipping past the 10-job cap when Redis is flaky, which is strictly
    better than refusing admins outright for the same reason.
    """
    try:
        return int(await redis.llen(key))
    except Exception as exc:
        log.warning("broadcasts.producer.llen_failed", error=repr(exc))
        return 0


def _translate(
    translator: Any | None,
    key: str,
    fallback: str,
    **kwargs: Any,
) -> str:
    """Translate ``key`` via ``translator`` or return the Russian ``fallback``.

    Mirrors the helper used by ``app/features/referrals`` and
    ``app/features/subscriptions`` so behaviour is consistent across
    feature handlers.
    """
    if translator is not None:
        try:
            value = translator(key, **kwargs)
        except Exception as exc:
            log.debug(
                "broadcasts.producer.translate_failed",
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
    "CANCEL_CALLBACK",
    "CANCEL_KEY_PREFIX",
    "CANCEL_TTL_SECONDS",
    "CONFIRM_CALLBACK",
    "FILTER_ACTIVE_30D",
    "FILTER_ALL",
    "FILTER_LANG",
    "MAX_QUEUE_LEN",
    "MAX_TEXT_LEN",
    "MIN_TEXT_LEN",
    "QUEUE_KEY",
    "BroadcastStates",
    "router",
]
