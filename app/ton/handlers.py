"""TON Connect handlers — ``/connect_wallet`` and ``/disconnect_wallet``.

Requirements:
- 2.3 — pressing "Отвязать кошелёк" in the profile tears the binding down
  through :meth:`TonConnector.disconnect` (re-wires the core profile callback
  registered in task 6.5).
- 3.1 — ``/connect_wallet`` returns a TON Connect 2 deeplink and QR.
- 3.4 — ``/disconnect_wallet`` removes the binding and closes the session.
- 3.6 — only one active wallet per user; the command refuses to start a
  second connection while one is already bound.

Router is registered in :func:`app.bot.register_routers` only when
``services.ton`` is not ``None`` (i.e. ``feature_ton_connector`` is on).
The connector instance is exposed as ``dispatcher["ton"]`` so aiogram
injects it into every handler via the ``ton`` keyword argument.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Coroutine
from typing import TYPE_CHECKING

import structlog
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.core.db.models import User
from app.ton.connector import (
    AlreadyConnectedError,
    ConnectionFailure,
    ConnectionSuccess,
    StartResult,
    TonConnector,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


log = structlog.get_logger(__name__)

router = Router(name="ton")


# Localised mapping of ``ConnectionFailure.reason`` tags (produced by
# :mod:`app.ton.verifier` / :class:`TonConnector`) to user-facing Russian
# strings. Pre-i18n; Stage 4 will move these into ``app/locales``.
_FAILURE_MESSAGES: dict[str, str] = {
    "no_session": "Сессия не найдена.",
    "timeout": "Время ожидания истекло.",
    "telegram_id_mismatch": "Не удалось проверить подпись кошелька.",
    "nonce": "Не удалось проверить подпись кошелька.",
    "signature": "Не удалось проверить подпись кошелька.",
    "payload_shape": "Не удалось проверить подпись кошелька.",
    "timestamp_out_of_window": "Не удалось проверить подпись кошелька.",
    "wallet_pubkey_length": "Не удалось проверить подпись кошелька.",
    "signature_length": "Не удалось проверить подпись кошелька.",
    "address_hash_length": "Не удалось проверить подпись кошелька.",
    "sdk_error": "Ошибка подключения. Попробуйте ещё раз.",
}
_FAILURE_FALLBACK = "Не удалось подключить кошелёк."

_FEATURE_OFF_MESSAGE = "Функция привязки кошелька недоступна."


# Strong references to fire-and-forget background tasks so the GC cannot
# reap them mid-flight (RUF006). Each task removes itself from the set via
# ``add_done_callback`` once it finishes.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _spawn_background(coro: Coroutine[object, object, None], *, name: str) -> None:
    """Schedule ``coro`` as a detached task and retain a reference to it."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# ---------------------------------------------------------------------------
# /connect_wallet
# ---------------------------------------------------------------------------


@router.message(Command("connect_wallet"))
async def handle_connect_wallet(
    message: Message,
    user: User,
    ton: TonConnector | None = None,
) -> None:
    """Start a TON Connect 2 session and reply with deeplink + QR.

    Requirements 3.1, 3.6.
    """
    if ton is None:
        # Defensive: router is only registered when the feature is on, but
        # guard anyway so that a misconfigured wiring never crashes the bot.
        await message.answer(_FEATURE_OFF_MESSAGE)
        return

    if message.bot is None or message.chat is None:  # pragma: no cover - defensive
        return

    try:
        start: StartResult = await ton.start_connection(user.telegram_id)
    except AlreadyConnectedError:
        await message.answer(
            "У вас уже привязан кошелёк. "
            "Используйте /disconnect_wallet чтобы отвязать."
        )
        return
    except Exception as exc:
        log.exception(
            "ton_connect.start_failed",
            telegram_id=user.telegram_id,
            error=repr(exc),
        )
        await message.answer("Не удалось запустить подключение. Попробуйте позже.")
        return

    expires = start.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    caption = (
        f'🔗 <a href="{start.deeplink}">Открыть кошелёк</a>\n'
        f"Отсканируйте QR или откройте ссылку в приложении кошелька.\n"
        f"Срок действия: до {expires}."
    )
    plain_text = (
        f'🔗 <a href="{start.deeplink}">Открыть кошелёк</a>\n'
        f"Откройте ссылку в приложении кошелька.\n"
        f"Срок действия: до {expires}."
    )

    if start.qr_base64:
        try:
            qr_bytes = base64.b64decode(start.qr_base64)
        except (ValueError, base64.binascii.Error) as exc:
            log.warning(
                "ton_connect.qr_decode_failed",
                telegram_id=user.telegram_id,
                error=repr(exc),
            )
            sent = await message.answer(plain_text, disable_web_page_preview=True)
            kind = "text"
        else:
            sent = await message.answer_photo(
                BufferedInputFile(qr_bytes, filename="wallet_qr.png"),
                caption=caption,
            )
            kind = "photo"
    else:
        sent = await message.answer(plain_text, disable_web_page_preview=True)
        kind = "text"

    # Persist chat/message metadata so ``ton_session_cleanup`` (task 15.2) can
    # edit this very message once the session TTL expires (Requirement 3.5).
    try:
        await ton.save_connect_meta(
            telegram_id=user.telegram_id,
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            kind=kind,
            expires_at=start.expires_at,
        )
    except Exception as exc:
        log.warning(
            "ton_connect.meta_save_failed",
            telegram_id=user.telegram_id,
            error=repr(exc),
        )

    # Hand off to a background task so the command handler can return
    # immediately. The task awaits the wallet approve-callback and sends
    # the outcome back to the same chat.
    _spawn_background(
        _await_and_reply(ton, message.bot, message.chat.id, user.telegram_id),
        name=f"ton-await-{user.telegram_id}",
    )


async def _await_and_reply(
    ton: TonConnector,
    bot: Bot,
    chat_id: int,
    telegram_id: int,
) -> None:
    """Background worker: await wallet approval and notify the user.

    Requirements 3.2, 3.3, 3.6. Any exception is logged but never re-raised —
    this task runs detached and must not take the event loop down with it.
    """
    try:
        result = await ton.await_connection(telegram_id)
    except Exception as exc:
        log.exception(
            "ton_connect.await_crashed",
            telegram_id=telegram_id,
            error=repr(exc),
        )
        try:
            await bot.send_message(chat_id, _FAILURE_FALLBACK)
        except Exception:
            pass
        await _safe_clear_meta(ton, telegram_id)
        return

    if isinstance(result, ConnectionSuccess):
        wallet = result.wallet_name or "кошелёк"
        text = (
            f"✅ Кошелёк привязан: <code>{result.address}</code> ({wallet})"
        )
    elif isinstance(result, ConnectionFailure):
        text = _FAILURE_MESSAGES.get(result.reason, _FAILURE_FALLBACK)
    else:  # pragma: no cover - defensive, result is a Union
        text = _FAILURE_FALLBACK

    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:
        log.warning(
            "ton_connect.notify_failed",
            telegram_id=telegram_id,
            error=repr(exc),
        )
    # Either outcome is terminal — drop the cleanup-job hint so it does
    # not double-notify the user (Requirement 3.5).
    await _safe_clear_meta(ton, telegram_id)


async def _safe_clear_meta(ton: TonConnector, telegram_id: int) -> None:
    """Best-effort ``clear_connect_meta`` — swallows Redis errors."""
    try:
        await ton.clear_connect_meta(telegram_id)
    except Exception as exc:
        log.warning(
            "ton_connect.meta_clear_failed",
            telegram_id=telegram_id,
            error=repr(exc),
        )


# ---------------------------------------------------------------------------
# /disconnect_wallet
# ---------------------------------------------------------------------------


@router.message(Command("disconnect_wallet"))
async def handle_disconnect_wallet(
    message: Message,
    user: User,
    ton: TonConnector | None = None,
) -> None:
    """Tear down the TON Connect session and clear the DB binding.

    Requirement 3.4.
    """
    if ton is None:
        await message.answer(_FEATURE_OFF_MESSAGE)
        return

    if user.ton_address is None:
        await message.answer("У вас нет привязанного кошелька.")
        return

    try:
        await ton.disconnect(user.telegram_id)
    except Exception as exc:
        log.exception(
            "ton_connect.disconnect_failed",
            telegram_id=user.telegram_id,
            error=repr(exc),
        )
        await message.answer("Не удалось отвязать кошелёк. Попробуйте позже.")
        return

    # Keep the in-memory ``user`` consistent with the DB for this update.
    user.ton_address = None
    user.ton_wallet_name = None
    user.ton_connected_at = None
    await message.answer("Кошелёк отвязан.")


__all__ = ["router"]
