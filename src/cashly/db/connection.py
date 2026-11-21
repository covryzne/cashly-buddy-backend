from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from cashly.core.environment import ConfigurationError
from cashly.core.performance import instrument_engine, performance
from cashly.core.settings import Settings


def database_url(secret: SecretStr | None, *, allow_docker_dev: bool = False) -> URL:
    if secret is None:
        raise ConfigurationError("Database URL is not configured.")
    try:
        value = secret.get_secret_value()
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        url = make_url(value)
        if url.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError
        if not url.host or not url.database or not url.username:
            raise ValueError
        if url.port == 6543:
            raise ValueError
        local = url.host in {"localhost", "127.0.0.1", "::1"} or (
            allow_docker_dev
            and url.host == "host.docker.internal"
            and url.port == 54322
            and url.database == "postgres"
        )
        mode = url.query.get("sslmode", "require")
        if not local and mode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError
        if mode not in {"require", "verify-ca", "verify-full", "disable"}:
            raise ValueError
        return url.set(drivername="postgresql+psycopg").update_query_dict({"sslmode": mode})
    except (ValueError, TypeError, SQLAlchemyError):
        raise ConfigurationError(
            "Invalid database URL. Use PostgreSQL/psycopg, direct/session port (not 6543), "
            "and TLS require/verify-ca/verify-full. Values are omitted."
        ) from None


def build_engine(settings: Settings, *, migration: bool = False) -> Engine:
    secret = settings.migration_database_url if migration else settings.database_url
    url = database_url(secret, allow_docker_dev=settings.environment == "dev" and not migration)
    options = {
        "echo": False,
        "hide_parameters": True,
        "connect_args": {
            "connect_timeout": settings.db_connect_timeout,
            "application_name": "cashly-migration" if migration else "cashly-api",
            "prepare_threshold": None,
        },
    }
    if migration:
        return create_engine(url, poolclass=NullPool, **options)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=1800,
        **options,
    )


class Database:
    def __init__(self, settings: Settings):
        self.engine = build_engine(settings)
        instrument_engine(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def transaction(
        self,
        *,
        account_id: UUID | None = None,
        auth_user_id: UUID | None = None,
    ) -> Iterator[Session]:
        """Trusted server identity only; transaction-local context cannot leak across requests.

        This does not authenticate callers. Future services must verify JWTs and scope
        repository queries as well, especially when using an owner/BYPASSRLS role.
        """
        with self._sessions.begin() as session:
            started = perf_counter()
            session.connection()
            metrics = performance.get()
            if metrics is not None:
                metrics.db_connect_ms += (perf_counter() - started) * 1000
            session.execute(
                text(
                    "SELECT set_config('cashly.account_id', :account_id, true), "
                    "set_config('cashly.auth_user_id', :auth_user_id, true)"
                ),
                {
                    "account_id": str(account_id) if account_id else "",
                    "auth_user_id": str(auth_user_id) if auth_user_id else "",
                },
            )
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
