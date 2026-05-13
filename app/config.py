"""Application configuration (pydantic-settings v2).

All settings are read from environment variables or a .env file. Required
secrets are validated fail-fast at startup (Requirement 15.3). Feature flags
enable/disable optional modules (Requirement 15.1). Cross-field validation
ensures that when a feature flag is on, its required parameters are present
(Requirement 16.4).
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from env / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Stage 1 (required)
    # ------------------------------------------------------------------
    bot_token: SecretStr
    db_dsn: str  # accept both postgresql+asyncpg://... and sqlite+aiosqlite://...
    redis_url: RedisDsn

    tg_mode: Literal["polling", "webhook"] = "polling"
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8080, ge=1, le=65535)

    webhook_public_url: HttpUrl | None = None
    webhook_secret: SecretStr | None = None

    fsm_timeout_minutes: int = Field(default=30, ge=1, le=1440)
    default_locale: str = "ru"
    supported_locales: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ru", "en"]
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ------------------------------------------------------------------
    # Stage 2
    # ------------------------------------------------------------------
    superadmin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Stage 3 — TON Connect
    # ------------------------------------------------------------------
    feature_ton_connector: bool = False
    # Public URL where /tonconnect-manifest.json is served (passed to pytonconnect).
    ton_manifest_url: HttpUrl | None = None
    # Fields that make up the served tonconnect-manifest.json body.
    # Required when feature_ton_connector is enabled (validated below).
    ton_app_url: HttpUrl | None = None
    ton_app_name: str | None = None
    ton_app_icon_url: HttpUrl | None = None
    ton_app_terms_url: HttpUrl | None = None
    ton_app_privacy_url: HttpUrl | None = None

    # ------------------------------------------------------------------
    # Stage 4 — UX
    # ------------------------------------------------------------------
    feature_i18n: bool = False
    feature_antispam: bool = False
    feature_subscriptions: bool = False
    required_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Stage 5 — Growth
    # ------------------------------------------------------------------
    feature_referrals: bool = False
    feature_broadcasts: bool = False
    feature_stats: bool = False

    # ------------------------------------------------------------------
    # Stage 6 — Payments
    # ------------------------------------------------------------------
    feature_payments: bool = False
    payments_provider: Literal["stars", "ton", "both"] | None = None
    ton_receive_address: str | None = None
    ton_api_url: HttpUrl | None = None
    ton_api_key: SecretStr | None = None

    # ------------------------------------------------------------------
    # Parsers for CSV-style env values
    # ------------------------------------------------------------------
    @field_validator("superadmin_ids", mode="before")
    @classmethod
    def _parse_superadmin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(x) for x in value.replace(" ", "").split(",") if x]
        return value

    @field_validator("supported_locales", "required_channels", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return value

    @field_validator("db_dsn")
    @classmethod
    def _validate_db_dsn(cls, value: str) -> str:
        if not (
            value.startswith("postgresql+asyncpg://")
            or value.startswith("sqlite+aiosqlite://")
        ):
            raise ValueError(
                "DB_DSN must be postgresql+asyncpg://... or sqlite+aiosqlite://..."
            )
        return value

    # ------------------------------------------------------------------
    # Cross-field validation (fail-fast on missing per-stage params)
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_stages(self) -> Self:
        if self.tg_mode == "webhook":
            if self.webhook_public_url is None or self.webhook_secret is None:
                raise ValueError(
                    "TG_MODE=webhook requires WEBHOOK_PUBLIC_URL and WEBHOOK_SECRET"
                )

        if self.feature_ton_connector:
            if self.ton_manifest_url is None:
                raise ValueError(
                    "FEATURE_TON_CONNECTOR=true requires TON_MANIFEST_URL"
                )
            missing_manifest_fields = [
                name
                for name, value in (
                    ("TON_APP_URL", self.ton_app_url),
                    ("TON_APP_NAME", self.ton_app_name),
                    ("TON_APP_ICON_URL", self.ton_app_icon_url),
                )
                if not value
            ]
            if missing_manifest_fields:
                raise ValueError(
                    "FEATURE_TON_CONNECTOR=true requires "
                    + ", ".join(missing_manifest_fields)
                )
            if self.webhook_public_url is None and self.tg_mode != "webhook":
                # Manifest needs a public HTTPS endpoint; for polling mode we still
                # expose the manifest on webhook_public_url (the HTTP server is
                # listening regardless of tg_mode). If no public URL is set we
                # cannot serve the manifest.
                if self.webhook_public_url is None:
                    raise ValueError(
                        "FEATURE_TON_CONNECTOR=true requires WEBHOOK_PUBLIC_URL "
                        "to serve /tonconnect-manifest.json"
                    )

        if self.feature_subscriptions and not self.required_channels:
            raise ValueError(
                "FEATURE_SUBSCRIPTIONS=true requires non-empty REQUIRED_CHANNELS"
            )

        if self.feature_i18n:
            if self.default_locale not in self.supported_locales:
                raise ValueError(
                    "DEFAULT_LOCALE must be listed in SUPPORTED_LOCALES"
                )

        if self.feature_payments:
            if self.payments_provider is None:
                raise ValueError(
                    "FEATURE_PAYMENTS=true requires PAYMENTS_PROVIDER "
                    "(one of: stars | ton | both)"
                )
            if self.payments_provider in ("ton", "both"):
                if not self.ton_receive_address:
                    raise ValueError(
                        "PAYMENTS_PROVIDER includes TON but TON_RECEIVE_ADDRESS is missing"
                    )
                if self.ton_api_url is None:
                    raise ValueError(
                        "PAYMENTS_PROVIDER includes TON but TON_API_URL is missing"
                    )

        return self

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    @property
    def fsm_timeout_seconds(self) -> int:
        return self.fsm_timeout_minutes * 60


def load_settings() -> Settings:
    """Load settings, exit the process with non-zero code on any error.

    Requirement 15.3: missing mandatory secrets must abort startup with a
    non-zero exit code and a message indicating the missing key.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pragma: no cover - startup path
        import sys

        print(f"[FATAL] configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
