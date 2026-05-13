"""I18n middleware — selects a language per update and exposes a translator.

Requirements:
- **10.1** — works with any locale listed in ``Loader.supported_locales``.
- **10.2** — first subtag of ``user.language_code`` selects the locale;
  ``en-US`` → ``en``. When the primary subtag is not supported, fall back to
  ``loader.default_locale``.
- **10.3** — prefers an explicit saved choice in ``User.language_code`` over
  whatever Telegram reports (Registration already persists the DB value).

The middleware never raises. If anything about the update is unexpected it
logs a debug line and falls through with the default locale so handlers still
get a working translator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.features.i18n.loader import Loader, Translator

log = structlog.get_logger(__name__)


class I18nMiddleware(BaseMiddleware):
    """Outer middleware that injects ``data["_"]`` / ``data["t"]``."""

    def __init__(self, loader: Loader) -> None:
        self._loader = loader

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        lang = self._loader.default_locale
        try:
            raw = _extract_language_code(event, data)
            lang = _normalise_lang(raw, self._loader.supported_locales, self._loader.default_locale)
        except Exception as exc:
            log.debug("i18n.middleware.extract_failed", error=repr(exc))

        translator = Translator(self._loader, lang)
        data["lang"] = lang
        data["_"] = translator
        data["t"] = translator  # alias for handlers that dislike ``_``
        return await handler(event, data)


def _extract_language_code(event: TelegramObject, data: dict[str, Any]) -> str | None:
    """Pick the most authoritative language code available.

    Priority:
        1. ``User.language_code`` from the DB (set by Registration) — this is
           what Req 10.3 means by "use the saved choice".
        2. ``event.from_user.language_code`` from Telegram — used for the very
           first message of a brand-new user (Req 10.2).
    """
    user = data.get("user")
    if user is not None:
        code = getattr(user, "language_code", None)
        if code:
            return code
    tg_user = getattr(event, "from_user", None)
    if tg_user is not None:
        code = getattr(tg_user, "language_code", None)
        if code:
            return code
    return None


def _normalise_lang(
    raw: str | None, supported: tuple[str, ...], default: str
) -> str:
    """Map a BCP-47-ish code to a supported locale (Req 10.2)."""
    if not raw:
        return default
    if raw in supported:
        return raw
    primary = raw.split("-", 1)[0].strip().lower()
    if primary in supported:
        return primary
    return default
