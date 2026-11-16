"""Credential-safe operational commands. Never print DB exceptions/DSNs."""

from io import StringIO
from urllib.parse import urlsplit

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from cashly.core.environment import PROJECT_ROOT, ConfigurationError
from cashly.core.settings import Settings
from cashly.db.connection import build_engine, database_url


def alembic_config(settings: Settings | None = None, output=None) -> Config:
    config = Config(
        str(PROJECT_ROOT / "alembic.ini"), stdout=output or StringIO(), output_buffer=output
    )
    if settings is not None:
        config.attributes["settings"] = settings
    return config


def check_database(settings: Settings) -> dict:
    engine = build_engine(settings)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
            # pg_stat_ssl describes the server's peer, which may be the pooler.
            # Inspect libpq for this application's actual client connection.
            tls = connection.connection.driver_connection.pgconn.ssl_in_use
            privileged = connection.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
            schema_exists = connection.scalar(
                text("SELECT to_regclass('cashly.alembic_version') IS NOT NULL")
            )
            version = (
                connection.scalar(text("SELECT version_num FROM cashly.alembic_version"))
                if schema_exists
                else None
            )
        expected = ScriptDirectory.from_config(alembic_config()).get_current_head()
        return {
            "connected": True,
            "tls": bool(tls),
            "privileged_role": bool(privileged),
            "schema_current": version == expected,
        }
    except Exception:
        raise ConfigurationError(
            "Database check failed. Verify URL, credentials, network, TLS and permissions locally. "
            "Connection details are omitted."
        ) from None
    finally:
        engine.dispose()


def upgrade_database(settings: Settings) -> None:
    if settings.environment != "dev":
        raise ConfigurationError("This Task 02 migration launcher supports dev only.")
    verify_development_target(settings)
    try:
        command.upgrade(alembic_config(settings), "head")
    except Exception:
        raise ConfigurationError(
            "Migration failed and was rolled back. Verify the development project, migration "
            "URL, auth.users reference and DDL permissions locally. Details are omitted."
        ) from None


def verify_development_target(settings: Settings) -> None:
    """Require runtime/migration URLs to match the configured Supabase project.

    This catches accidental URL mixups, not a false declaration that a live project
    is development. The operator must select the isolated dev project locally.
    """
    migration = database_url(settings.migration_database_url)
    if migration.host in {"localhost", "127.0.0.1", "::1"}:
        return
    try:
        parsed = urlsplit(settings.supabase_url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host.endswith(".supabase.co"):
            raise ValueError
        project = host.removesuffix(".supabase.co")
        if not project or "." in project:
            raise ValueError
        for url in (migration, database_url(settings.database_url)):
            direct = url.host == f"db.{project}.supabase.co"
            pooled = (url.host or "").endswith(".pooler.supabase.com") and (
                url.username or ""
            ).endswith(f".{project}")
            if not (direct or pooled):
                raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError(
            "Migration target mismatch. Set SUPABASE_URL, DATABASE_URL and "
            "MIGRATION_DATABASE_URL to the same isolated development project. Values are omitted."
        ) from None


def render_migration_sql() -> str:
    buffer = StringIO()
    command.upgrade(alembic_config(output=buffer), "head", sql=True)
    return buffer.getvalue()
