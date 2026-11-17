from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from test_auth import auth_authority, prepare, runtime_database
from test_auth import auth_postgres_settings as auth_postgres_settings

from cashly.core.auth import Identity, require_identity
from cashly.db.connection import build_engine
from cashly.main import create_app
from cashly.services.accounts import bootstrap


def test_security_challenge_attempt_limit_expiry_and_identity_validation(auth_postgres_settings):
    from cashly.services.account_security import matches_identity

    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    assert not matches_identity({"id": "invalid"}, identity.user_id, identity.email)
    assert not matches_identity({}, identity.user_id, identity.email)
    assert not matches_identity(
        {"id": str(uuid4()), "email": identity.email}, identity.user_id, identity.email
    )
    prepare(auth_postgres_settings, [identity])
    with auth_authority(identity.user_id) as (url, _):
        settings = auth_postgres_settings.model_copy(
            update={"supabase_url": url, "supabase_publishable_key": SecretStr("synthetic-key")}
        )
        with TestClient(create_app(settings)) as api:
            original = api.app.state.database
            db = runtime_database(settings)
            api.app.state.database = db
            try:
                bootstrap(db, settings, identity)
                headers = {"Authorization": "Bearer google-valid"}
                duplicate = api.post(
                    "/api/v1/me/security/email/request",
                    headers=headers,
                    json={"email": "taken@example.invalid"},
                )
                assert duplicate.status_code == 409
                assert duplicate.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
                assert "private provider" not in duplicate.text
                engine = build_engine(settings, migration=True)
                with engine.connect() as connection:
                    assert (
                        connection.scalar(
                            text("SELECT count(*) FROM cashly.account_security_challenges")
                        )
                        == 0
                    )
                engine.dispose()
                path = "/api/v1/me/security/password/"
                challenge = api.post(path + "request", headers=headers, json={}).json()["data"][
                    "challenge_id"
                ]
                body = {"challenge_id": challenge, "token": "000000", "password": "New-Pass123!"}
                for _ in range(8):
                    assert api.post(path + "verify", headers=headers, json=body).status_code == 400
                assert (
                    api.post(
                        path + "verify", headers=headers, json={**body, "token": "123456"}
                    ).status_code
                    == 429
                )
                engine = build_engine(settings, migration=True)
                with engine.begin() as connection:
                    assert (
                        connection.scalar(
                            text("SELECT attempts FROM cashly.account_security_challenges")
                        )
                        == 8
                    )
                    connection.execute(
                        text("UPDATE cashly.account_security_challenges SET expires_at=:expiry"),
                        {"expiry": datetime.now(UTC) - timedelta(seconds=1)},
                    )
                engine.dispose()
                assert (
                    api.post(
                        path + "verify", headers=headers, json={**body, "token": "123456"}
                    ).status_code
                    == 409
                )
                replacement = api.post(path + "request", headers=headers, json={})
                assert replacement.status_code == 202
                assert replacement.json()["data"]["challenge_id"] != challenge
                assert not api.get("/api/v1/me", headers=headers).json()["data"][
                    "password_login_enabled"
                ]
            finally:
                db.dispose()
                api.app.state.database = original


def test_profile_ownership_unique_username_and_security_codes(auth_postgres_settings):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    other = Identity(uuid4(), "other@example.invalid", "Other", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity, other])
    with auth_authority(identity.user_id) as (url, calls):
        settings = auth_postgres_settings.model_copy(
            update={"supabase_url": url, "supabase_publishable_key": SecretStr("synthetic-key")}
        )
        with TestClient(create_app(settings)) as api:
            original = api.app.state.database
            db = runtime_database(settings)
            api.app.state.database = db
            try:
                account = bootstrap(db, settings, identity)
                bootstrap(db, settings, other)
                headers = {"Authorization": "Bearer google-valid"}
                changed = api.patch(
                    "/api/v1/me/profile",
                    headers=headers,
                    json={"display_name": "  Full Name  ", "username": " Demo.Name "},
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["data"]["username"] == "demo.name"
                assert changed.json()["data"]["email"] == identity.email
                assert (
                    api.get("/api/v1/me", headers=headers).json()["data"]["display_name"]
                    == "Full Name"
                )
                assert (
                    api.patch(
                        "/api/v1/me/profile",
                        headers=headers,
                        json={
                            "display_name": "Tester",
                            "username": "valid.name",
                            "email": "stolen@example.invalid",
                        },
                    ).status_code
                    == 422
                )
                api.app.dependency_overrides[require_identity] = lambda: other
                conflict = api.patch(
                    "/api/v1/me/profile",
                    headers=headers,
                    json={"display_name": "Other", "username": "DEMO.NAME"},
                )
                assert conflict.status_code == 409
                assert "uq_profiles" not in conflict.text
                api.app.dependency_overrides.clear()
                requested = api.post(
                    "/api/v1/me/security/password/request", headers=headers, json={}
                )
                assert requested.status_code == 202, requested.text
                challenge = requested.json()["data"]["challenge_id"]
                assert "/auth/v1/recover" in calls
                assert (
                    api.post(
                        "/api/v1/me/security/password/request", headers=headers, json={}
                    ).status_code
                    == 429
                )
                body = {"challenge_id": challenge, "token": "000000", "password": "New-Pass123!"}
                assert (
                    api.post(
                        "/api/v1/me/security/password/verify", headers=headers, json=body
                    ).status_code
                    == 400
                )
                assert not api.get("/api/v1/me", headers=headers).json()["data"][
                    "password_login_enabled"
                ]
                api.app.dependency_overrides[require_identity] = lambda: other
                assert (
                    api.post(
                        "/api/v1/me/security/password/verify",
                        headers=headers,
                        json={**body, "token": "123456"},
                    ).status_code
                    == 409
                )
                api.app.dependency_overrides.clear()
                completed = api.post(
                    "/api/v1/me/security/password/verify",
                    headers=headers,
                    json={**body, "token": "123456"},
                )
                assert completed.status_code == 200, completed.text
                assert completed.json()["data"]["status"] == "password_changed"
                assert "email-only" not in completed.text and "New-Pass123!" not in completed.text
                assert api.get("/api/v1/me", headers=headers).json()["data"][
                    "password_login_enabled"
                ]
                assert (
                    api.post(
                        "/api/v1/me/security/password/verify",
                        headers=headers,
                        json={**body, "token": "123456"},
                    ).status_code
                    == 409
                )
                engine = build_engine(settings, migration=True)
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE cashly.account_security_challenges SET expires_at=:expiry"),
                        {"expiry": datetime.now(UTC) + timedelta(minutes=3)},
                    )
                engine.dispose()
                email = api.post(
                    "/api/v1/me/security/email/request",
                    headers=headers,
                    json={"email": "new@example.invalid"},
                )
                assert email.status_code == 202, email.text
                email_id = email.json()["data"]["challenge_id"]
                current = api.post(
                    "/api/v1/me/security/email/verify",
                    headers=headers,
                    json={"challenge_id": email_id, "address": "current", "token": "111111"},
                )
                assert current.status_code == 200, current.text
                assert current.json()["data"]["status"] == "confirmation_pending"
                assert (
                    api.get("/api/v1/me", headers=headers).json()["data"]["email"] == identity.email
                )
                assert (
                    api.post(
                        "/api/v1/me/security/email/verify",
                        headers=headers,
                        json={"challenge_id": email_id, "address": "new", "token": "000000"},
                    ).status_code
                    == 400
                )
                final = api.post(
                    "/api/v1/me/security/email/verify",
                    headers=headers,
                    json={"challenge_id": email_id, "address": "new", "token": "222222"},
                )
                assert final.status_code == 200, final.text
                assert final.json()["data"]["status"] == "email_changed"
                me = api.get("/api/v1/me", headers=headers).json()["data"]
                assert me["email"] == "new@example.invalid"
                assert me["account_id"] == str(account.account_id)
                assert me["username"] == "demo.name"
            finally:
                db.dispose()
                api.app.state.database = original
