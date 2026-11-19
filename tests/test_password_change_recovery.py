from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_auth import auth_authority, prepare, runtime_database
from test_auth import auth_postgres_settings as auth_postgres_settings

from cashly.core.auth import Identity
from cashly.main import create_app
from cashly.services.accounts import bootstrap


def test_current_password_required_before_otp_and_on_save(auth_postgres_settings):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    with auth_authority(identity.user_id) as (url, calls):
        settings = auth_postgres_settings.model_copy(
            update={"supabase_url": url, "supabase_publishable_key": SecretStr("synthetic-key")}
        )
        with TestClient(create_app(settings)) as api:
            original = api.app.state.database
            db = runtime_database(settings)
            api.app.state.database = db
            try:
                bootstrap(db, settings, identity, True)
                headers = {"Authorization": "Bearer google-valid"}
                path = "/api/v1/me/security/password/change"
                assert (
                    api.post(path, headers=headers, json={"password": "New-Pass123!"}).status_code
                    == 422
                )
                wrong = api.post(
                    path,
                    headers=headers,
                    json={"password": "New-Pass123!", "current_password": "Wrong-Pass999!"},
                )
                assert wrong.status_code == 400
                assert wrong.json()["error"]["code"] == "CURRENT_PASSWORD_INVALID"
                assert "Wrong-Pass999!" not in wrong.text
                assert (
                    api.post(
                        path,
                        headers=headers,
                        json={"password": "Old-Pass123!", "current_password": "Old-Pass123!"},
                    ).status_code
                    == 422
                )
                saved = api.post(
                    path,
                    headers=headers,
                    json={"password": "New-Pass123!", "current_password": "Old-Pass123!"},
                )
                assert saved.status_code == 200, saved.text
                assert saved.json()["data"]["status"] == "password_changed"
                assert not any("/recover" in call or "/verify" in call for call in calls)
                assert "access_token" not in saved.text and "Old-Pass123!" not in saved.text
            finally:
                db.dispose()
                api.app.state.database = original


@pytest.fixture
def recovery_api(auth_postgres_settings):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    with auth_authority(identity.user_id) as (url, calls):
        settings = auth_postgres_settings.model_copy(
            update={
                "supabase_url": url,
                "supabase_publishable_key": SecretStr("synthetic-key"),
                "cors_origins": ("http://localhost:3000",),
            }
        )
        with TestClient(create_app(settings)) as api:
            original = api.app.state.database
            db = runtime_database(settings)
            api.app.state.database = db
            try:
                bootstrap(db, settings, identity, True)
                yield api, calls, db, identity
            finally:
                db.dispose()
                api.app.state.database = original


def start(api, email="tester@example.invalid", token_hash="a" * 64):
    return api.post(
        "/api/v1/auth/password/reset/start", json={"email": email, "token_hash": token_hash}
    )


def proof(data):
    return {key: data[key] for key in ("recovery_id", "recovery_token")}


def test_recovery_retry_retains_deadline_then_consumes_once(recovery_api):
    from uuid import UUID

    from cashly.db.models import PasswordRecoverySession

    api, calls, db, identity = recovery_api
    path = "/api/v1/auth/password/reset/"
    body = {"email": identity.email, "redirect_to": "http://localhost:3000/auth/password/callback"}
    known = api.post(path + "request", json=body)
    unknown = api.post(path + "request", json={**body, "email": "unknown@example.invalid"})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    assert start(api, token_hash="z" * 64).status_code == 400
    started = start(api)
    assert started.status_code == 200, started.text
    data = started.json()["data"]
    grant = proof(data)
    assert start(api).status_code == 400
    assert (
        api.get(
            "/api/v1/me", headers={"Authorization": "Bearer " + grant["recovery_token"]}
        ).status_code
        == 403
    )
    with db.transaction(auth_user_id=identity.user_id) as session:
        row = session.get(PasswordRecoverySession, UUID(grant["recovery_id"]))
        deadline = row.expires_at
        assert row.token_digest != grant["recovery_token"] and row.consumed_at is None
    for _ in range(2):
        rejected = api.post(path + "verify", json={**grant, "password": "Old-Pass123!"})
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "PASSWORD_UNCHANGED"
    with db.transaction(auth_user_id=identity.user_id) as session:
        row = session.get(PasswordRecoverySession, UUID(grant["recovery_id"]))
        assert row.expires_at == deadline and row.consumed_at is None
    assert "/auth/v1/logout?scope=local" not in calls
    verifies = calls.count("/auth/v1/verify")
    saved = api.post(path + "verify", json={**grant, "password": "New-Pass123!"})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"data": {"status": "password_reset"}}
    assert "recovery_token" not in saved.text
    assert calls.count("/auth/v1/verify") == verifies
    assert (
        api.post(path + "verify", json={**grant, "password": "Other-Pass123!"}).status_code == 409
    )
    assert calls.count("/auth/v1/logout?scope=local") == 1


@pytest.mark.parametrize("action", ["expire", "cancel"])
def test_recovery_expiry_or_cancel_is_terminal(recovery_api, action):
    from datetime import timedelta
    from uuid import UUID

    from cashly.db.models import PasswordRecoverySession

    api, calls, db, identity = recovery_api
    grant = proof(start(api).json()["data"])
    if action == "expire":
        with db.transaction(auth_user_id=identity.user_id) as session:
            session.get(PasswordRecoverySession, UUID(grant["recovery_id"])).expires_at = (
                datetime.now(UTC) - timedelta(seconds=1)
            )
    else:
        assert api.post("/api/v1/auth/password/reset/cancel", json=grant).status_code == 200
    assert (
        api.post(
            "/api/v1/auth/password/reset/verify", json={**grant, "password": "New-Pass123!"}
        ).status_code
        == 409
    )
    assert calls.count("/auth/v1/user") == (3 if action == "cancel" else 2)  # GET only


def test_recovery_wrong_identity_and_redirect(recovery_api):
    api, calls, db, identity = recovery_api
    assert (
        api.post(
            "/api/v1/auth/password/reset/request",
            json={
                "email": identity.email,
                "redirect_to": "https://evil.invalid/auth/password/callback",
            },
        ).status_code
        == 422
    )
    assert start(api, email="different@example.invalid").status_code == 400
    assert calls.count("/auth/v1/logout?scope=local") == 1


def test_parallel_reset_only_one_password_write(recovery_api):
    from concurrent.futures import ThreadPoolExecutor

    api, calls, db, identity = recovery_api
    grant = proof(start(api).json()["data"])

    def attempt(_):
        return api.post(
            "/api/v1/auth/password/reset/verify", json={**grant, "password": "New-Pass123!"}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 409]
    assert calls.count("/auth/v1/logout?scope=local") == 1


def test_recovery_migration_round_trip(auth_postgres_settings):
    from alembic import command
    from sqlalchemy import inspect, text

    from cashly.db.commands import alembic_config, upgrade_database
    from cashly.db.connection import build_engine

    upgrade_database(auth_postgres_settings)
    engine = build_engine(auth_postgres_settings, migration=True)
    try:
        with engine.connect() as conn:
            assert inspect(conn).has_table("password_recovery_sessions", schema="cashly")
            assert conn.scalar(
                text(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE oid='cashly.password_recovery_sessions'::regclass"
                )
            )
        command.downgrade(alembic_config(auth_postgres_settings), "0004_profile_protection")
        with engine.connect() as conn:
            assert not inspect(conn).has_table("password_recovery_sessions", schema="cashly")
        upgrade_database(auth_postgres_settings)
        with engine.connect() as conn:
            assert inspect(conn).has_table("password_recovery_sessions", schema="cashly")
    finally:
        engine.dispose()
