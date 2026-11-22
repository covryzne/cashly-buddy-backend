from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.pool import NullPool, QueuePool

from cashly.core.environment import ConfigurationError
from cashly.core.settings import Settings
from cashly.db.commands import (
    check_database,
    render_migration_sql,
    upgrade_database,
    verify_migration_target,
)
from cashly.db.connection import Database, build_engine, database_url


@pytest.mark.parametrize(
    "value",
    [
        "private-secret",
        "sqlite:///private-secret",
        "postgresql://u:private-secret@host:6543/db",
        "postgresql://u:private-secret@host/db?sslmode=disable",
        "postgresql://u:private-secret@host/db?sslmode=prefer",
        "postgresql:///db",
    ],
)
def test_bad_database_urls_are_sanitized(value):
    with pytest.raises(ConfigurationError) as error:
        database_url(SecretStr(value))
    assert "private-secret" not in str(error.value)


def test_url_normalization_and_tls_default():
    url = database_url(SecretStr("postgres://user:pass%40word@host:5432/postgres"))
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "pass@word"
    assert url.query["sslmode"] == "require"


def test_docker_desktop_database_without_tls_is_dev_runtime_only():
    dsn = "postgresql://u:p@host.docker.internal:54322/postgres?sslmode=disable"
    dev = Settings(environment="dev", database_url=dsn, migration_database_url=dsn)
    engine = build_engine(dev)
    try:
        assert engine.url.host == "host.docker.internal"
        assert engine.url.query["sslmode"] == "disable"
    finally:
        engine.dispose()
    with pytest.raises(ConfigurationError):
        build_engine(dev, migration=True)
    with pytest.raises(ConfigurationError):
        build_engine(Settings(environment="staging", database_url=dsn))


def test_runtime_pool_and_migration_nullpool_do_not_connect():
    settings = Settings(
        environment="dev",
        db_pool_size=2,
        db_max_overflow=0,
        database_url=SecretStr("postgresql://u:p@host/db"),
        migration_database_url=SecretStr("postgresql://m:p@host/db"),
    )
    runtime = build_engine(settings)
    migration = build_engine(settings, migration=True)
    try:
        assert isinstance(runtime.pool, QueuePool)
        assert runtime.pool.size() == 2
        assert runtime.pool._max_overflow == 0
        assert isinstance(migration.pool, NullPool)
        assert runtime.hide_parameters is True
        assert runtime.url.username == "u"
        assert migration.url.username == "m"
    finally:
        runtime.dispose()
        migration.dispose()


def test_migration_url_never_falls_back_to_runtime():
    with pytest.raises(ConfigurationError, match="not configured"):
        build_engine(
            Settings(environment="dev", database_url=SecretStr("postgresql://u:p@host/db")),
            migration=True,
        )


def test_sql_preview_does_not_load_env_or_connect():
    with (
        patch("cashly.core.environment.resolve_environment", side_effect=AssertionError),
        patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError),
    ):
        sql = render_migration_sql()
    assert "CREATE TABLE cashly.expenses" in sql
    assert "CREATE TABLE cashly.expense_items" in sql
    assert "REFERENCES auth.users" in sql
    assert "CREATE TABLE auth.users" not in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "SET search_path = pg_catalog" in sql


def test_connection_failure_hides_driver_exception():
    with patch("cashly.db.commands.build_engine") as build:
        build.return_value.connect.side_effect = RuntimeError("private-secret")
        with pytest.raises(ConfigurationError) as error:
            check_database(Settings(environment="dev"))
    assert "private-secret" not in str(error.value)
    build.return_value.dispose.assert_called_once()


@pytest.mark.parametrize("client_tls", [True, False])
def test_tls_diagnostic_uses_client_driver_not_pooler_server_view(client_tls):
    with patch("cashly.db.commands.build_engine") as build:
        connection = build.return_value.connect.return_value.__enter__.return_value
        connection.connection.driver_connection.pgconn.ssl_in_use = client_tls
        # SELECT 1, role privileges, schema existence. No server-side SSL query.
        connection.scalar.side_effect = [1, False, False]
        result = check_database(Settings(environment="dev"))
    assert result["tls"] is client_tls
    assert all("pg_stat_ssl" not in str(call.args[0]) for call in connection.scalar.call_args_list)


def test_production_upgrade_reaches_online_alembic():
    settings = Settings(
        environment="prod",
        supabase_url="https://fictional-ref.supabase.co",
        database_url="postgresql://app.fictional-ref:p@host.pooler.supabase.com/postgres",
        migration_database_url="postgresql://postgres:p@db.fictional-ref.supabase.co/postgres",
    )
    with patch("cashly.db.connection.build_engine") as build:
        build.return_value.connect.side_effect = RuntimeError("private-secret")
        with pytest.raises(
            ConfigurationError, match="Migration failed and was rolled back"
        ) as error:
            upgrade_database(settings)
    build.assert_called_once_with(settings, migration=True)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("migration_url", "message"),
    [
        ("postgresql://admin:p@db.staging-ref.supabase.co/postgres", "mismatch"),
        ("postgresql://admin:p@127.0.0.1/postgres", "mismatch"),
        ("postgresql://app:p@db.fictional-ref.supabase.co/postgres", "separate database role"),
        ("postgresql://admin:p@db.fictional-ref.supabase.co/other", "mismatch"),
        ("postgresql://admin:p@db.fictional-ref.supabase.co/postgres?host=other", "mismatch"),
    ],
)
def test_production_upgrade_rejects_mismatched_target_without_connecting(migration_url, message):
    settings = Settings(
        environment="prod",
        supabase_url="https://fictional-ref.supabase.co",
        database_url="postgresql://app.fictional-ref:p@host.pooler.supabase.com/postgres",
        migration_database_url=migration_url,
    )
    with patch("cashly.db.commands.command.upgrade") as upgrade:
        with pytest.raises(ConfigurationError, match=message):
            upgrade_database(settings)
    upgrade.assert_not_called()


def test_staging_migration_uses_selected_settings():
    settings = Settings(environment="staging")
    with (
        patch("cashly.db.commands.verify_migration_target") as verify,
        patch("cashly.db.commands.command.upgrade") as upgrade,
    ):
        upgrade_database(settings)
    verify.assert_called_once_with(settings)
    assert upgrade.call_args.args[0].attributes["settings"] is settings


def test_remote_migration_target_must_match_supabase_project():
    good = {
        "environment": "dev",
        "supabase_url": "https://fictional-ref.supabase.co",
        "database_url": SecretStr(
            "postgresql://app.fictional-ref:p@host.pooler.supabase.com:5432/postgres"
        ),
        "migration_database_url": SecretStr(
            "postgresql://postgres:p@db.fictional-ref.supabase.co:5432/postgres"
        ),
    }
    verify_migration_target(Settings(**good))
    for field, value in [
        ("supabase_url", "https://other.supabase.co"),
        (
            "migration_database_url",
            SecretStr("postgresql://postgres:p@db.other.supabase.co/postgres"),
        ),
        ("database_url", SecretStr("postgresql://app.other:p@host.pooler.supabase.com/postgres")),
    ]:
        with pytest.raises(ConfigurationError, match="mismatch"):
            verify_migration_target(Settings(**{**good, field: value}))


@pytest.mark.parametrize(
    "key", ["DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_CONNECT_TIMEOUT"]
)
def test_invalid_pool_settings_are_sanitized(key):
    with pytest.raises(ConfigurationError):
        Settings.from_values({"CASHLY_ENV": "dev", key: "private-secret"})


def test_database_transaction_sets_local_trusted_context():
    account_id, auth_user_id = uuid4(), uuid4()
    with (
        patch("cashly.db.connection.build_engine"),
        patch("cashly.db.connection.instrument_engine"),
        patch("cashly.db.connection.sessionmaker") as sm,
    ):
        db = Database(Settings(environment="dev"))
        with db.transaction(account_id=account_id, auth_user_id=auth_user_id):
            pass
        session = sm.return_value.begin.return_value.__enter__.return_value
        assert session.execute.call_args.args[1] == {
            "account_id": str(account_id),
            "auth_user_id": str(auth_user_id),
        }
