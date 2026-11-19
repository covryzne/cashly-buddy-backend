"""Explicit local-only maintenance. Never invoked by auth or application startup."""

from urllib.parse import urlsplit

from sqlalchemy import text

from cashly.core.environment import ConfigurationError
from cashly.core.settings import Settings
from cashly.db.commands import alembic_config
from cashly.db.connection import build_engine, database_url

CATEGORIES = (
    ("food_drink", "Makanan & Minuman"),
    ("transport", "Transportasi"),
    ("groceries", "Belanja Kebutuhan"),
    ("bills", "Tagihan"),
    ("shopping", "Belanja Lainnya"),
    ("health", "Kesehatan"),
    ("entertainment", "Hiburan"),
    ("education", "Pendidikan"),
    ("other", "Lainnya"),
)
TABLES = (
    "expenses",
    "expense_items",
    "idempotency_records",
    "expense_categories",
    "accounts",
    "profiles",
    "account_auth_links",
    "game_events",
    "game_states",
    "streak_protections",
    "account_security_challenges",
    "password_recovery_sessions",
)


def verify_local_target(settings: Settings) -> None:
    if settings.environment != "dev":
        raise ConfigurationError("Local data tools require dev.")
    authority = urlsplit(settings.supabase_url or "")
    if authority.hostname not in {"127.0.0.1", "localhost", "::1"} or authority.port != 54321:
        raise ConfigurationError("Local Auth must use loopback port 54321.")
    for secret in (settings.database_url, settings.migration_database_url):
        url = database_url(secret)
        if (
            url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.port != 54322
            or url.database != "postgres"
            or url.query.get("host") is not None
            or url.query.get("service") is not None
            or url.query.get("hostaddr") is not None
        ):
            raise ConfigurationError("Local data tools require loopback:54322/postgres.")


def local_data(settings: Settings, action: str, *, confirm: str | None = None) -> dict:
    from alembic.script import ScriptDirectory

    verify_local_target(settings)
    if action not in {"inspect", "reset", "seed-reference"}:
        raise ConfigurationError("Unknown local data action.")
    if action == "reset" and confirm != "cashly-local-transactions":
        raise ConfigurationError("Reset requires --confirm cashly-local-transactions.")
    engine = build_engine(settings, migration=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("SET LOCAL statement_timeout = '30s'"))
            conn.execute(text("SELECT pg_advisory_xact_lock(6300262001)"))
            version = conn.scalar(text("SELECT version_num FROM cashly.alembic_version"))
            if version != ScriptDirectory.from_config(alembic_config()).get_current_head():
                raise ConfigurationError("Apply migrations before local data maintenance.")
            # Fixed table allowlist; block writes while computing/deleting the same rows.
            if action == "reset":
                conn.execute(
                    text(
                        "LOCK TABLE cashly.expense_items, cashly.expenses, "
                        "cashly.idempotency_records, cashly.game_events, "
                        "cashly.game_states, cashly.streak_protections, "
                        "cashly.account_security_challenges, "
                        "cashly.password_recovery_sessions IN EXCLUSIVE MODE"
                    )
                )

            def counts():
                result = {
                    name: conn.scalar(text(f"SELECT count(*) FROM cashly.{name}"))
                    for name in TABLES
                }
                result["auth_users"] = conn.scalar(text("SELECT count(*) FROM auth.users"))
                return result

            before = counts()
            if action == "reset":
                for table in (
                    "expense_items",
                    "expenses",
                    "idempotency_records",
                    "game_events",
                    "game_states",
                    "streak_protections",
                    "account_security_challenges",
                    "password_recovery_sessions",
                ):
                    conn.execute(text(f"DELETE FROM cashly.{table}"))
            elif action == "seed-reference":
                for order, (code, label) in enumerate(CATEGORIES, 1):
                    conn.execute(
                        text(
                            "INSERT INTO cashly.expense_categories(code,label_id,sort_order) "
                            "VALUES (:code,:label,:order) ON CONFLICT (code) DO NOTHING"
                        ),
                        {"code": code, "label": label, "order": order},
                    )
            after = counts()
        return {"action": action, "revision": version, "before": before, "after": after}
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError(
            "Local data operation failed and rolled back; connection details omitted."
        ) from None
    finally:
        engine.dispose()
