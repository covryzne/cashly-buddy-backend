import json
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from cashly.core.environment import ConfigurationError, EnvironmentSelection


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
    db_pool_size: int = Field(default=2, ge=1, le=20)
    db_max_overflow: int = Field(default=0, ge=0, le=20)
    db_pool_timeout: int = Field(default=10, ge=1, le=120)
    db_connect_timeout: int = Field(default=10, ge=1, le=60)
    # Compatibility only: old env values no longer enable seeding in auth.
    dev_sample_data: bool = False
    weekly_review_test_mode: bool = False
    daily_checkin_test_mode: bool = False

    @field_validator("supabase_url")
    @classmethod
    def validate_auth_authority(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
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
                db_pool_size=values.get("DB_POOL_SIZE", "2"),
                db_max_overflow=values.get("DB_MAX_OVERFLOW", "0"),
                db_pool_timeout=values.get("DB_POOL_TIMEOUT", "10"),
                db_connect_timeout=values.get("DB_CONNECT_TIMEOUT", "10"),
                dev_sample_data=values.get("DEV_SAMPLE_DATA", "false"),
                weekly_review_test_mode=values.get("WEEKLY_REVIEW_TEST_MODE", "false"),
                daily_checkin_test_mode=values.get("DAILY_CHECKIN_TEST_MODE", "false"),
            )
        except (ValidationError, ValueError, TypeError):
            raise ConfigurationError(
                "Invalid settings; check PORT, LOG_LEVEL, CORS_ORIGINS and DB pool limits. "
                "Values are omitted for privacy."
            ) from None
        if settings.environment != "dev" and any(
            not origin.startswith("https://") for origin in settings.cors_origins
        ):
            raise ConfigurationError("Staging/prod CORS origins must use HTTPS.")
        return settings
