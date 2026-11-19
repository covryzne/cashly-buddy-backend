import runpy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from test_auth import auth_authority, prepare, runtime_database
from test_auth import auth_postgres_settings as auth_postgres_settings

from cashly.core.auth import Identity
from cashly.core.environment import ConfigurationError
from cashly.core.settings import Settings
from cashly.db.connection import build_engine
from cashly.services.accounts import bootstrap


@pytest.fixture
def seeder():
    return runpy.run_path("scripts/seed_development.py")


def hosted(environment="staging", project="testref"):
    return Settings(
        environment=environment,
        supabase_url="https://testref.supabase.co",
        database_url=SecretStr(
            f"postgresql+psycopg://cashly_app.{project}:fake@"
            "aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require"
        ),
        migration_database_url=SecretStr(
            "postgresql+psycopg://postgres.testref:fake@"
            "aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require"
        ),
    )


@pytest.mark.parametrize("environment", ["dev", "staging"])
def test_hosted_seed_accepts_matching_project(seeder, environment):
    seeder["verify_seed_target"](hosted(environment))


@pytest.mark.parametrize("settings", [hosted("prod"), hosted(project="wrongref")])
def test_seed_rejects_production_or_project_mismatch(seeder, settings):
    with pytest.raises(ConfigurationError):
        seeder["verify_seed_target"](settings)


def test_seed_rejects_local_database_mixed_with_hosted_auth(seeder):
    mixed = hosted().model_copy(
        update={
            "migration_database_url": SecretStr(
                "postgresql+psycopg://postgres:fake@127.0.0.1:54322/postgres?sslmode=disable"
            ),
        }
    )
    with pytest.raises(ConfigurationError):
        seeder["verify_seed_target"](mixed)


@pytest.mark.parametrize("environment", ["dev", "staging"])
def test_existing_form_account_seed_is_owned_and_repeat_safe(
    seeder, auth_postgres_settings, monkeypatch, environment
):
    identity = Identity(uuid4(), "tester@example.invalid", "Seed", datetime.now(UTC))
    other = Identity(uuid4(), "other@example.invalid", "Other", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity, other])
    seed = seeder["seed_account"]
    seed.__globals__["Database"] = runtime_database
    monkeypatch.setattr("cashly.main.Database", runtime_database)
    with auth_authority(identity.user_id) as (url, calls):
        settings = auth_postgres_settings.model_copy(
            update={
                "environment": environment,
                "supabase_url": url,
                "supabase_publishable_key": SecretStr("synthetic-key"),
            }
        )
        db = runtime_database(settings)
        try:
            other_account = bootstrap(db, settings, other)
        finally:
            db.dispose()
        credentials = {"email": identity.email, "password": "Local-Test123!"}
        first = seed(settings, credentials)
        assert not first["already_seeded"]
        assert first["gamification"]["current_streak"] == 12
        assert first["gamification"]["protection_remaining"] == 5
        admin = build_engine(settings, migration=True)
        try:
            with admin.begin() as conn:
                conn.execute(text("UPDATE cashly.expenses SET description='Edited through test'"))
            assert seed(settings, credentials)["already_seeded"]
            with admin.connect() as conn:
                assert conn.scalar(text("SELECT count(*) FROM cashly.expenses")) == 16
                assert conn.scalar(text("SELECT count(*) FROM cashly.expense_items")) == 16
                assert (
                    conn.scalar(
                        text(
                            "SELECT count(*) FROM cashly.expenses "
                            "WHERE description='Edited through test'"
                        )
                    )
                    == 16
                )
                assert (
                    conn.scalar(
                        text("SELECT count(*) FROM cashly.expenses WHERE account_id=:id"),
                        {"id": other_account.account_id},
                    )
                    == 0
                )
        finally:
            admin.dispose()
        assert not any("signup" in call for call in calls)
        assert calls.count("/auth/v1/logout?scope=local") == 2


@pytest.mark.parametrize(
    "email,password",
    [
        ("tester@example.invalid", "Wrong-Pass999!"),
        ("unconfirmed@example.invalid", "Local-Test123!"),
    ],
)
def test_hosted_seed_never_registers_or_seeds_on_login_rejection(
    seeder, auth_postgres_settings, email, password
):
    identity = Identity(uuid4(), "tester@example.invalid", "Seed", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    seed = seeder["seed_account"]
    seeded = []
    seed.__globals__["seed_reference"] = lambda _: seeded.append(True)
    seed.__globals__["seed_scenario"] = lambda *_: seeded.append(True)
    with auth_authority(identity.user_id) as (url, calls):
        settings = auth_postgres_settings.model_copy(
            update={
                "environment": "staging",
                "supabase_url": url,
                "supabase_publishable_key": SecretStr("synthetic-key"),
            }
        )
        with pytest.raises(seeder["SeedError"], match="Create the account through the Cashly form"):
            seed(settings, {"email": email, "password": password})
        assert not seeded
        assert not any("signup" in call for call in calls)
