"""TON Connect manifest endpoint.

Serves ``/tonconnect-manifest.json`` describing the bot for TON Connect 2
wallets. The manifest body is built from :class:`app.config.Settings` fields
(``ton_app_url``, ``ton_app_name``, ``ton_app_icon_url``, optional
``ton_app_terms_url`` and ``ton_app_privacy_url``). Requirement 3.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings


MANIFEST_PATH = "/tonconnect-manifest.json"


def build_manifest(settings: Settings) -> dict[str, str]:
    """Build the TON Connect manifest dict from settings.

    Only ``url``, ``name`` and ``iconUrl`` are required by the TON Connect 2
    spec; ``termsOfUseUrl`` and ``privacyPolicyUrl`` are added only when
    configured, to keep the response minimal.
    """
    if (
        settings.ton_app_url is None
        or settings.ton_app_name is None
        or settings.ton_app_icon_url is None
    ):
        raise RuntimeError(
            "TON manifest endpoint is enabled but TON_APP_URL / TON_APP_NAME / "
            "TON_APP_ICON_URL are not configured"
        )

    manifest: dict[str, str] = {
        "url": str(settings.ton_app_url),
        "name": settings.ton_app_name,
        "iconUrl": str(settings.ton_app_icon_url),
    }
    if settings.ton_app_terms_url is not None:
        manifest["termsOfUseUrl"] = str(settings.ton_app_terms_url)
    if settings.ton_app_privacy_url is not None:
        manifest["privacyPolicyUrl"] = str(settings.ton_app_privacy_url)
    return manifest


def manifest_handler_factory(settings: Settings):
    """Return an aiohttp handler that serves the TON Connect manifest JSON."""
    # Build once at registration time — settings are immutable at runtime.
    body = build_manifest(settings)

    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(body)

    return handler


def register_manifest_route(app: web.Application, settings: Settings) -> None:
    """Register the ``GET /tonconnect-manifest.json`` route on ``app``."""
    app.router.add_get(MANIFEST_PATH, manifest_handler_factory(settings))
