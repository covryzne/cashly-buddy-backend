from alembic import context
from sqlalchemy import text

from cashly.core.environment import ConfigurationError, resolve_environment
from cashly.core.settings import Settings
from cashly.db.commands import verify_development_target
from cashly.db.connection import build_engine
from cashly.db.models import Base

config = context.config


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "cashly"
    return True


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table":
        return object_.schema == "cashly" and name != "alembic_version"
    return True


def configure(**kwargs):
    context.configure(
        target_metadata=Base.metadata,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        version_table_schema="cashly",
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


if context.is_offline_mode():
    # Offline rendering needs neither dotenv files nor real database credentials.
    configure(dialect_name="postgresql", literal_binds=True)
    with context.begin_transaction():
        context.execute("CREATE SCHEMA IF NOT EXISTS cashly")
        context.run_migrations()
else:
    # Tests/CLI pass settings directly; ordinary Alembic uses the shared env loader.
    settings = config.attributes.get("settings")
    if settings is None and "connection" not in config.attributes:
        settings = Settings.from_selection(resolve_environment())
    if settings is not None:
        if settings.environment != "dev":
            raise ConfigurationError("Task 02 online Alembic operations support dev only.")
        verify_development_target(settings)
    engine = None
    supplied = config.attributes.get("connection")
    try:
        if supplied is None:
            engine = build_engine(settings, migration=True)
            supplied = engine.connect()
        # One explicit transaction: schema bootstrap, version table and migration
        # either commit together or roll back. No create_all or startup migration.
        with supplied.begin():
            supplied.execute(text("SELECT pg_advisory_xact_lock(6300262001)"))
            supplied.execute(text("CREATE SCHEMA IF NOT EXISTS cashly"))
            configure(connection=supplied)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        if engine is not None:
            if supplied is not None:
                supplied.close()
            engine.dispose()
