"""i18n loader and translator.

Implements the core of ``Модуль_Локализации``:

- **Req 10.1** — support at least ``ru`` and ``en``, extensible via YAML files.
- **Req 10.2** — first subtag of ``language_code`` picks the locale; otherwise
  fall back to the configured default locale. (Normalisation lives in the
  middleware; the loader accepts pre-normalised codes.)
- **Req 10.3** — an explicit language choice takes precedence over Telegram's
  ``language_code``. The loader just looks up whatever locale the caller gave
  it, so this requirement is satisfied at the callsite.
- **Req 10.4** — if a key is missing in the chosen locale, fall back to the
  default locale; if missing there too, return the key and record a warning
  via ``audit.record_warning(event="missing_translation", ...)``.
- **Req 15.3** — startup is fail-fast: unreadable files, non-flat YAML
  contents, or a default locale missing keys that another locale defines
  raise ``RuntimeError`` during ``load()``.

The loader is **intentionally synchronous** so that ``build_services`` can
validate catalogs before we start accepting updates. The only async surface is
the best-effort ``audit.record_warning`` call from ``translate``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import structlog
import yaml

from app.core.services.audit import AuditLog

log = structlog.get_logger(__name__)


class _SafeDict(dict):
    """Dict that preserves unknown ``{placeholders}`` in ``format_map``."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Loader:
    """Loads and queries YAML translation catalogs.

    Catalogs live in ``locales_dir`` as ``{locale}.yml``, one flat
    ``dict[str, str]`` per file. The ``default_locale`` catalog is the source
    of truth for the key set (Req 10.4).
    """

    def __init__(
        self,
        locales_dir: Path,
        default_locale: str,
        supported_locales: Sequence[str],
        *,
        audit: AuditLog | None = None,
    ) -> None:
        if default_locale not in supported_locales:
            raise RuntimeError(
                f"i18n: default_locale={default_locale!r} is not in "
                f"supported_locales={list(supported_locales)!r}"
            )
        self._dir = Path(locales_dir)
        self._default = default_locale
        self._supported: tuple[str, ...] = tuple(supported_locales)
        self._audit = audit
        self._locales: Mapping[str, Mapping[str, str]] = MappingProxyType({})
        self._loaded = False
        # Keep strong references to fire-and-forget ``record_warning`` tasks
        # so the garbage collector cannot drop them mid-flight (RUF006).
        self._audit_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def default_locale(self) -> str:
        return self._default

    @property
    def supported_locales(self) -> tuple[str, ...]:
        return self._supported

    @property
    def loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------
    # Loading / validation (sync, fail-fast)
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Read every supported locale from disk and validate the catalog.

        Raises:
            RuntimeError: if a file is missing, not a flat ``dict[str, str]``,
                or if ``default_locale`` is missing any key that another
                locale defines (Req 10.4, 15.3).
        """
        catalogs: dict[str, dict[str, str]] = {}
        for locale in self._supported:
            path = self._dir / f"{locale}.yml"
            if not path.is_file():
                raise RuntimeError(
                    f"i18n: missing translation file for locale "
                    f"{locale!r}: expected {path}"
                )
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise RuntimeError(
                    f"i18n: {path} is not valid YAML: {exc}"
                ) from exc
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"i18n: {path} must be a flat mapping, got {type(raw).__name__}"
                )
            validated: dict[str, str] = {}
            for key, value in raw.items():
                if not isinstance(key, str):
                    raise RuntimeError(
                        f"i18n: {path} contains non-string key {key!r}"
                    )
                if not isinstance(value, str):
                    raise RuntimeError(
                        f"i18n: {path} key {key!r} maps to {type(value).__name__}, "
                        "expected a string (catalogs must be flat dict[str, str])"
                    )
                validated[key] = value
            catalogs[locale] = validated

        # Fail-fast: default_locale must cover every key any other locale has.
        default_keys = set(catalogs[self._default])
        missing_from_default: set[str] = set()
        for locale, catalog in catalogs.items():
            if locale == self._default:
                continue
            missing_from_default.update(set(catalog) - default_keys)
            extras_in_locale = sorted(default_keys - set(catalog))
            if extras_in_locale:
                log.warning(
                    "i18n.locale_missing_keys",
                    locale=locale,
                    default=self._default,
                    missing=extras_in_locale[:10],
                    missing_count=len(extras_in_locale),
                )
        if missing_from_default:
            raise RuntimeError(
                f"i18n: default_locale={self._default!r} is missing keys that "
                f"other locales define (must be the source of truth): "
                f"{sorted(missing_from_default)}"
            )

        self._locales = MappingProxyType(
            {k: MappingProxyType(v) for k, v in catalogs.items()}
        )
        self._loaded = True
        log.info(
            "i18n.loaded",
            default=self._default,
            supported=list(self._supported),
            key_counts={k: len(v) for k, v in catalogs.items()},
        )

    # ------------------------------------------------------------------
    # Runtime translation (Req 10.2 / 10.4)
    # ------------------------------------------------------------------
    def translate(self, key: str, lang: str | None) -> str:
        """Resolve ``key`` in ``lang`` with fallback to the default locale."""
        if not self._loaded:
            # Defensive: we want a clear signal rather than a KeyError chain.
            raise RuntimeError("i18n: Loader.load() has not been called")

        normalised = self._resolve_locale(lang)
        catalog = self._locales.get(normalised)
        if catalog is not None:
            value = catalog.get(key)
            if value is not None:
                return value

        # Fallback to default locale (Req 10.4).
        default_catalog = self._locales[self._default]
        value = default_catalog.get(key)
        if value is not None:
            return value

        # Missing everywhere — record a warning and return the key as a
        # visible safety placeholder.
        log.warning("i18n.missing_translation", key=key, lang=normalised)
        self._schedule_audit_warning(key, normalised)
        return key

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_locale(self, lang: str | None) -> str:
        if not lang:
            return self._default
        if lang in self._locales:
            return lang
        # Defensive normalisation (the middleware normally does this first).
        primary = lang.split("-", 1)[0].lower()
        if primary in self._locales:
            return primary
        return self._default

    def _schedule_audit_warning(self, key: str, lang: str) -> None:
        if self._audit is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        coro = self._audit.record_warning(
            event="missing_translation",
            details={"key": key, "lang": lang},
        )
        try:
            task = loop.create_task(coro)
        except RuntimeError:
            # Loop may be shutting down; swallow to keep translate() safe.
            coro.close()
            return
        # Retain the task reference until it completes so it is not
        # garbage-collected mid-flight (RUF006).
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)


class Translator:
    """Callable translator bound to a single language.

    Handlers receive one of these as ``data["_"]``/``data["t"]`` via the
    ``I18nMiddleware`` and use it as ``_("some.key", name=user.first_name)``.

    Unknown ``{placeholders}`` are preserved verbatim instead of raising, so a
    translation bug never crashes a handler.
    """

    __slots__ = ("_lang", "_loader")

    def __init__(self, loader: Loader, lang: str) -> None:
        self._loader = loader
        self._lang = lang

    @property
    def lang(self) -> str:
        return self._lang

    def __call__(self, key: str, /, **kwargs: Any) -> str:
        template = self._loader.translate(key, self._lang)
        if not kwargs:
            return template
        try:
            return template.format_map(_SafeDict(kwargs))
        except (IndexError, ValueError):
            # Broken ``{...}`` literal in the template — return raw template
            # rather than crash the handler.
            log.warning("i18n.format_failed", key=key, lang=self._lang)
            return template
