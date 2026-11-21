import json
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from cashly.core.environment import ConfigurationError, EnvironmentSelection
from cashly.schemas.ai_routing import FallbackModel


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Literal["dev", "staging", "prod"]
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["auto", "pretty", "json"] = "auto"
    log_color: Literal["auto", "always", "never"] = "auto"
    cors_origins: tuple[str, ...] = ()
    # Reserved for Task 02/03; not required or consumed by the health-only app.
    database_url: SecretStr | None = Field(default=None, repr=False)
    migration_database_url: SecretStr | None = Field(default=None, repr=False)
    supabase_url: str | None = Field(default=None, repr=False)
    supabase_publishable_key: SecretStr | None = Field(default=None, repr=False)
    supabase_secret_key: SecretStr | None = Field(default=None, repr=False)
    db_pool_size: int = Field(default=2, ge=1, le=20)
    db_max_overflow: int = Field(default=0, ge=0, le=20)
    db_pool_timeout: int = Field(default=10, ge=1, le=120)
    db_connect_timeout: int = Field(default=10, ge=1, le=60)
    # Compatibility only: old env values no longer enable seeding in auth.
    dev_sample_data: bool = False
    daily_checkin_test_mode: bool = False
    gemini_api_key: SecretStr | None = Field(default=None, repr=False)
    gemini_model: str = Field(default="gemini-3.5-flash-lite", pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    gemini_image_model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    gemini_audio_model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    gemini_timeout_seconds: int = Field(default=35, ge=5, le=40)
    gemini_thinking_level: Literal["auto", "low", "medium", "high"] = "low"
    ai_provider: Literal["gemini", "openrouter"] = "gemini"
    ai_audio_provider: Literal["gemini", "openrouter"] | None = None
    openrouter_api_key: SecretStr | None = Field(default=None, repr=False)
    openrouter_model: str = Field(
        default="google/gemini-3.5-flash-lite", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"
    )
    openrouter_image_model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"
    )
    openrouter_audio_model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"
    )
    openrouter_timeout_seconds: int = Field(default=35, ge=5, le=40)

    ai_fallback_models: tuple[FallbackModel, ...] = Field(
        default=(FallbackModel(provider="openrouter", inputs=("text", "image", "audio")),),
        max_length=12,
    )
    ai_attempt_timeout_seconds: float = Field(default=20, ge=1, le=40)
    ai_total_timeout_seconds: float = Field(default=60, ge=5, le=60)
    whatsapp_enabled: bool = False
    whatsapp_recording_enabled: bool = False
    whatsapp_app_url: str = ""
    whatsapp_report_frontend_url: str = ""
    whatsapp_bot_number: str = Field(default="", pattern=r"^(?:[1-9][0-9]{7,14})?$")
    whatsapp_instance: str = Field(default="cashly-staging", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    whatsapp_integration_key: SecretStr | None = Field(default=None, repr=False)
    whatsapp_hash_secret: SecretStr | None = Field(default=None, repr=False)
    whatsapp_encryption_key: SecretStr | None = Field(default=None, repr=False)

    @field_validator("ai_provider", mode="before")
    @classmethod
    def normalize_ai_provider(cls, value):
        return (value.strip().lower() or "gemini") if isinstance(value, str) else value

    @field_validator("gemini_api_key", "openrouter_api_key", mode="before")
    @classmethod
    def normalize_ai_key(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str):
            return raw.strip() or None
        return value

    @property
    def effective_ai_provider(self) -> Literal["gemini", "openrouter"]:
        if self.ai_provider == "openrouter" and self.openrouter_api_key is None:
            return "gemini"
        return self.ai_provider

    @field_validator("supabase_url")
    @classmethod
    def validate_auth_authority(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"} or (
            info.data.get("environment") == "dev"
            and parsed.hostname == "host.docker.internal"
            and parsed.port == 54321
        )
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        ):
            raise ValueError("Auth authority must be an HTTPS origin, or loopback HTTP for tests")
        return value.rstrip("/")

    @field_validator("cors_origins")
    @classmethod
    def validate_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("CORS requires exact HTTP(S) origins without paths or wildcards")
            _ = parsed.port  # Validate the port as well.
        return origins

    @classmethod
    def from_selection(cls, selection: EnvironmentSelection) -> "Settings":
        return cls.from_values(selection.values)

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> "Settings":
        origins_default = (
            '["http://localhost:3000","http://127.0.0.1:3000"]'
            if values["CASHLY_ENV"] == "dev"
            else "[]"
        )
        try:
            settings = cls(
                environment=values["CASHLY_ENV"],
                host=values.get("HOST", "127.0.0.1"),
                port=values.get("PORT", "8000"),
                log_level=values.get("LOG_LEVEL", "INFO").upper(),
                log_format=values.get("LOG_FORMAT", "auto"),
                log_color=values.get("LOG_COLOR", "auto"),
                cors_origins=json.loads(values.get("CORS_ORIGINS", origins_default)),
                database_url=values.get("DATABASE_URL") or None,
                migration_database_url=values.get("MIGRATION_DATABASE_URL") or None,
                supabase_url=values.get("SUPABASE_URL") or None,
                supabase_publishable_key=values.get("SUPABASE_PUBLISHABLE_KEY") or None,
                supabase_secret_key=values.get("SUPABASE_SECRET_KEY") or None,
                db_pool_size=values.get("DB_POOL_SIZE", "2"),
                db_max_overflow=values.get("DB_MAX_OVERFLOW", "0"),
                db_pool_timeout=values.get("DB_POOL_TIMEOUT", "10"),
                db_connect_timeout=values.get("DB_CONNECT_TIMEOUT", "10"),
                dev_sample_data=values.get("DEV_SAMPLE_DATA", "false"),
                daily_checkin_test_mode=values.get("DAILY_CHECKIN_TEST_MODE", "false"),
                whatsapp_enabled=values.get("WHATSAPP_ENABLED", "false"),
                whatsapp_recording_enabled=values.get("WHATSAPP_RECORDING_ENABLED", "false"),
                whatsapp_app_url=values.get("WHATSAPP_APP_URL", ""),
                whatsapp_report_frontend_url=values.get("WHATSAPP_REPORT_FRONTEND_URL", ""),
                whatsapp_bot_number=values.get("WHATSAPP_BOT_NUMBER", ""),
                whatsapp_instance=values.get("WHATSAPP_INSTANCE", "cashly-staging"),
                whatsapp_integration_key=values.get("WHATSAPP_INTEGRATION_KEY") or None,
                whatsapp_hash_secret=values.get("WHATSAPP_HASH_SECRET") or None,
                whatsapp_encryption_key=values.get("WHATSAPP_ENCRYPTION_KEY") or None,
                gemini_api_key=values.get("GEMINI_API_KEY") or None,
                gemini_model=(values.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"),
                gemini_image_model=values.get("GEMINI_IMAGE_MODEL") or None,
                gemini_audio_model=values.get("GEMINI_AUDIO_MODEL") or None,
                gemini_timeout_seconds=values.get("GEMINI_TIMEOUT_SECONDS", "35"),
                gemini_thinking_level=values.get("GEMINI_THINKING_LEVEL", "low"),
                ai_provider=values.get("AI_PROVIDER", "gemini"),
                ai_audio_provider=values.get("AI_AUDIO_PROVIDER") or None,
                ai_fallback_models=json.loads(
                    values.get("AI_FALLBACK_MODELS")
                    or '[{"provider":"openrouter","inputs":["text","image","audio"]}]'
                ),
                ai_attempt_timeout_seconds=values.get("AI_ATTEMPT_TIMEOUT_SECONDS", "20"),
                ai_total_timeout_seconds=values.get("AI_TOTAL_TIMEOUT_SECONDS", "60"),
                openrouter_api_key=values.get("OPENROUTER_API_KEY") or None,
                openrouter_model=(values.get("OPENROUTER_MODEL") or "google/gemini-3.5-flash-lite"),
                openrouter_image_model=values.get("OPENROUTER_IMAGE_MODEL") or None,
                openrouter_audio_model=values.get("OPENROUTER_AUDIO_MODEL") or None,
                openrouter_timeout_seconds=values.get("OPENROUTER_TIMEOUT_SECONDS", "35"),
            )
        except (ValidationError, ValueError, TypeError):
            raise ConfigurationError(
                "Invalid settings; check PORT, LOG_LEVEL, CORS_ORIGINS and DB pool limits. "
                "Values are omitted for privacy."
            ) from None
        if settings.environment != "dev":
            for origin in settings.cors_origins:
                local_staging = settings.environment == "staging" and urlsplit(origin).hostname in {
                    "localhost",
                    "127.0.0.1",
                    "::1",
                }
                if not origin.startswith("https://") and not local_staging:
                    raise ConfigurationError(
                        "CORS requires HTTPS; staging also permits HTTP loopback for local FE."
                    )
        return settings
