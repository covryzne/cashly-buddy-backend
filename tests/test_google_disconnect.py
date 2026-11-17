from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_auth import auth_authority, prepare, runtime_database
from test_auth import auth_postgres_settings as auth_postgres_settings

from cashly.core.auth import Identity, require_identity
from cashly.main import create_app
from cashly.services.accounts import bootstrap


@pytest.mark.parametrize("scenario", ["success", "outage", "missing_email_identity"])
def test_disconnect_requires_password_and_preserves_account(auth_postgres_settings, scenario):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    other = Identity(uuid4(), "other@example.invalid", "Other", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity, other])
    with auth_authority(
        identity.user_id,
        fail_delete=scenario == "outage",
        email_identity=scenario != "missing_email_identity",
    ) as (url, calls):
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
                path = "/api/v1/me/security/google"
                assert api.get(path).status_code == 401
                before = api.get(path, headers=headers).json()["data"]
                assert before["connected"] and before["google_email"] == identity.email
                assert not before["can_disconnect"]
                blocked = api.post(path + "/disconnect", headers=headers, json={})
                assert blocked.status_code == 409
                assert blocked.json()["error"]["code"] == "GOOGLE_PASSWORD_REQUIRED"
                assert not any("/identities/" in call for call in calls)
                proof = api.post(
                    "/api/v1/auth/password/reset/start",
                    json={"email": identity.email, "token_hash": "a" * 64},
                ).json()["data"]
                password = api.post(
                    "/api/v1/auth/password/reset/verify",
                    json={
                        "recovery_id": proof["recovery_id"],
                        "recovery_token": proof["recovery_token"],
                        "password": "New-Pass123!",
                    },
                )
                assert password.status_code == 200, password.text
                # Recovery marks the existing profile even before the next login.
                allowed = api.get(path, headers=headers).json()["data"]
                assert allowed["password_login_enabled"]
                assert allowed["can_disconnect"] == (scenario != "missing_email_identity")
                assert (
                    api.post(
                        path + "/disconnect", headers=headers, json={"identity_id": str(uuid4())}
                    ).status_code
                    == 422
                )
                api.app.dependency_overrides[require_identity] = lambda: other
                assert api.post(path + "/disconnect", headers=headers, json={}).status_code == 403
                api.app.dependency_overrides.clear()
                response = api.post(path + "/disconnect", headers=headers, json={})
                if scenario == "success":
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["connected"] is False
                    assert api.get(path, headers=headers).json()["data"]["google_email"] is None
                    assert (
                        api.post(path + "/disconnect", headers=headers, json={}).status_code == 200
                    )
                    assert sum("/identities/" in call for call in calls) == 1
                elif scenario == "outage":
                    assert response.status_code == 503
                    assert "private provider" not in response.text
                    assert api.get(path, headers=headers).json()["data"]["connected"]
                else:
                    assert response.status_code == 409
                    assert response.json()["error"]["code"] == "GOOGLE_EMAIL_LOGIN_REQUIRED"
                    assert not any("/identities/" in call for call in calls)
                me = api.get("/api/v1/me", headers=headers).json()["data"]
                assert me["account_id"] == str(account.account_id)
                assert me["email"] == identity.email and me["password_login_enabled"]
                assert api.get("/api/v1/expenses", headers=headers).json()["data"] == []
            finally:
                db.dispose()
                api.app.state.database = original
