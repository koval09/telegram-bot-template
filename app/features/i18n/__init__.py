"""Модуль_Локализации — i18n feature (Req 10)."""

from app.features.i18n.loader import Loader, Translator
from app.features.i18n.middleware import I18nMiddleware

__all__ = ["I18nMiddleware", "Loader", "Translator"]
