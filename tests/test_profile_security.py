from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_auth import auth_authority, prepare, runtime_database
from test_auth import auth_postgres_settings as auth_postgres_settings

from cashly.core.auth import Identity, require_identity
from cashly.main import create_app
from cashly.services.accounts import bootstrap


def test_profile_ownership_unique_username_and_readonly_email(auth_postgres_settings):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    other = Identity(uuid4(), "other@example.invalid", "Other", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity, other])
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
                bootstrap(db, settings, identity)
                bootstrap(db, settings, other)
                headers = {"Authorization": "Bearer google-valid"}
                setup = api.post(
                    "/api/v1/me/security/password/setup/request",
                    headers=headers,
                    json={"redirect_to": "http://localhost:3000/auth/password/callback"},
                )
                assert setup.status_code == 202, setup.text
                assert setup.json()["data"]["status"] == "link_sent"
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
            finally:
                db.dispose()
                api.app.state.database = original


def test_email_change_routes_are_removed():
    from cashly.core.settings import Settings

    with TestClient(create_app(Settings(environment="dev"))) as api:
        for path in (
            "/api/v1/me/security/email/request",
            "/api/v1/me/security/email/verify",
            "/api/v1/auth/email/change/verify",
        ):
            assert api.post(path, json={}).status_code == 404
            assert path not in api.get("/openapi.json").json()["paths"]
