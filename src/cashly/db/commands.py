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
    verify_migration_target(settings)
    try:
        command.upgrade(alembic_config(settings), "head")
    except Exception:
        raise ConfigurationError(
            "Migration failed and was rolled back. Verify the selected project, migration "
            "URL, auth.users reference and DDL permissions locally. Details are omitted."
        ) from None


def verify_migration_target(settings: Settings) -> None:
    """Require hosted migration and runtime URLs to match the selected project."""
    migration = database_url(settings.migration_database_url)
    is_prod = settings.environment == "prod"
    if not is_prod and migration.host in {"localhost", "127.0.0.1", "::1"}:
        return
    runtime = database_url(settings.database_url)
    try:
        parsed = urlsplit(settings.supabase_url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host.endswith(".supabase.co"):
            raise ValueError
        project = host.removesuffix(".supabase.co")
        if not project or "." in project:
            raise ValueError
        roles = []
        for url in (migration, runtime):
            direct = url.host == f"db.{project}.supabase.co"
            pooled = (url.host or "").endswith(".pooler.supabase.com") and (
                url.username or ""
            ).endswith(f".{project}")
            if not (direct or pooled):
                raise ValueError
            if is_prod and url.query.keys() & {
                "host",
                "hostaddr",
                "port",
                "dbname",
                "user",
                "service",
            }:
                raise ValueError
            roles.append(url.username.removesuffix(f".{project}") if pooled else url.username)
        if is_prod and migration.database != runtime.database:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError(
            "Migration target mismatch. Set SUPABASE_URL, DATABASE_URL and "
            "MIGRATION_DATABASE_URL to the same Supabase project and database for the selected "
            "environment. Values are omitted."
        ) from None
    if is_prod and roles[0] == roles[1]:
        raise ConfigurationError(
            "Production migration requires a separate database role from the runtime role. "
            "Values are omitted."
        )


def render_migration_sql() -> str:
    buffer = StringIO()
    command.upgrade(alembic_config(output=buffer), "head", sql=True)
    return buffer.getvalue()
