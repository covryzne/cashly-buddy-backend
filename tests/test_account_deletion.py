from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from test_auth import auth_authority, prepare, runtime_database
from test_auth import auth_postgres_settings as auth_postgres_settings

from cashly.core.auth import Identity
from cashly.db.connection import build_engine
from cashly.main import create_app
from cashly.services.accounts import bootstrap


@pytest.mark.parametrize("scenario", ["google", "password", "outage", "purge_failure"])
def test_account_deletion_is_owned_atomic_and_permanent(auth_postgres_settings, scenario):
    failure = scenario in {"outage", "purge_failure"}
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    other = Identity(uuid4(), "other@example.invalid", "Other", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity, other])
    engine = build_engine(auth_postgres_settings, migration=True)
    # Emulate Auth's restricted DB role: it can delete Auth users, not read
    # Cashly tables or call private lifecycle functions directly.
    role = "auth_delete_" + uuid4().hex
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA auth TO {role}")
        conn.exec_driver_sql(f"GRANT DELETE, SELECT ON auth.users TO {role}")

    def delete_user(user_id):
        with engine.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            conn.execute(text("DELETE FROM auth.users WHERE id=:id"), {"id": user_id})

    try:
        with auth_authority(
            identity.user_id, fail_delete=scenario == "outage", delete_user=delete_user
        ) as (url, calls):
            settings = auth_postgres_settings.model_copy(
                update={
                    "supabase_url": url,
                    "supabase_publishable_key": SecretStr("sb_publishable_synthetic"),
                    "supabase_secret_key": SecretStr("sb_secret_synthetic"),
                }
            )
            with TestClient(create_app(settings)) as api:
                original = api.app.state.database
                db = runtime_database(settings)
                api.app.state.database = db
                try:
                    account = bootstrap(db, settings, identity, scenario == "password")
                    other_account = bootstrap(db, settings, other)
                    headers = {
                        "Authorization": "Bearer google-valid",
                        "Idempotency-Key": "account-delete-expense",
                    }
                    expense = api.post(
                        "/api/v1/expenses",
                        headers=headers,
                        json={
                            "description": "Test expense",
                            "merchant": "Test",
                            "category_code": "food_drink",
                            "amount_idr": "20000",
                            "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                            "items": [{"label": "Item", "quantity": 1, "unit_price_idr": "20000"}],
                        },
                    )
                    assert expense.status_code == 201, expense.text
                    checked = api.post(
                        "/api/v1/gamification/checkins/review",
                        headers={
                            "Authorization": "Bearer google-valid",
                            "Idempotency-Key": "account-delete-checkin",
                        },
                        json={},
                    )
                    assert checked.status_code == 200, checked.text
                    account_id = account.account_id
                    # Existing profile must not leak its admin key through public config.
                    assert "sb_secret_synthetic" not in api.get("/api/v1/auth/configuration").text
                    path = "/api/v1/me/account"
                    assert (
                        api.request("DELETE", path, json={"confirmation": "HAPUS AKUN"}).status_code
                        == 401
                    )
                    assert (
                        api.request(
                            "DELETE",
                            path,
                            headers=headers,
                            json={
                                "confirmation": "HAPUS AKUN",
                                "account_id": str(other_account.account_id),
                            },
                        ).status_code
                        == 422
                    )
                    assert (
                        api.request(
                            "DELETE", path, headers=headers, json={"confirmation": "hapus"}
                        ).status_code
                        == 422
                    )
                    body = {"confirmation": "HAPUS AKUN"}
                    if scenario == "password":
                        assert (
                            api.request("DELETE", path, headers=headers, json=body).status_code
                            == 422
                        )
                        assert (
                            api.request(
                                "DELETE",
                                path,
                                headers=headers,
                                json={**body, "current_password": "Wrong-Pass999!"},
                            ).status_code
                            == 400
                        )
                        assert not any("/admin/users/" in call for call in calls)
                        body["current_password"] = "Existing-Pass123!"
                    tables = (
                        ("accounts", "id"),
                        ("profiles", "account_id"),
                        ("expenses", "account_id"),
                        ("idempotency_records", "account_id"),
                        ("game_states", "account_id"),
                        ("game_events", "account_id"),
                        ("streak_protections", "account_id"),
                        ("account_security_challenges", "account_id"),
                    )
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "INSERT INTO "
                                "cashly.streak_protections(account_id,protected_date,quota_month) "
                                "VALUES "
                                "(:id,current_date,date_trunc('month',current_date)::date)"
                            ),
                            {"id": account_id},
                        )
                        conn.execute(
                            text(
                                "INSERT INTO "
                                "cashly.account_security_challenges(id, account_id, "
                                "auth_user_id, purpose, current_email, expires_at)"
                                "VALUES "
                                "(:challenge, :id, :auth, 'legacy', "
                                "'tester@example.invalid', now()+interval"
                                "'5 minutes')"
                            ),
                            {"challenge": uuid4(), "id": account_id, "auth": identity.user_id},
                        )
                        conn.execute(
                            text(
                                "INSERT INTO "
                                "cashly.password_recovery_sessions(id, auth_user_id, email, "
                                "token_digest, expires_at)"
                                "VALUES "
                                "(:challenge,:auth,'tester@example.invalid',:digest,now()+interval "
                                "'5 minutes')"
                            ),
                            {
                                "challenge": uuid4(),
                                "auth": identity.user_id,
                                "digest": uuid4().hex + uuid4().hex,
                            },
                        )
                        before = {
                            table: conn.scalar(
                                text(f"SELECT count(*) FROM cashly.{table} WHERE {column}=:id"),
                                {"id": account_id},
                            )
                            for table, column in tables
                        }
                    if scenario == "purge_failure":
                        with engine.begin() as conn:
                            conn.exec_driver_sql(
                                "CREATE FUNCTION cashly.reject_test_delete() RETURNS trigger "
                                "LANGUAGE plpgsql AS $$ BEGIN "
                                "RAISE EXCEPTION 'test purge failed'; END $$"
                            )
                            conn.exec_driver_sql(
                                "CREATE TRIGGER reject_test_delete BEFORE DELETE "
                                "ON cashly.expenses FOR EACH ROW "
                                "EXECUTE FUNCTION cashly.reject_test_delete()"
                            )
                    response = api.request("DELETE", path, headers=headers, json=body)
                    assert response.status_code == (503 if failure else 200), response.text
                    assert (
                        "sb_secret_synthetic" not in response.text
                        and "private admin" not in response.text
                    )
                    with engine.connect() as conn:
                        expected = 1 if failure else 0
                        assert (
                            conn.scalar(
                                text("SELECT count(*) FROM auth.users WHERE id=:id"),
                                {"id": identity.user_id},
                            )
                            == expected
                        )
                        for table, column in tables:
                            assert conn.scalar(
                                text(f"SELECT count(*) FROM cashly.{table} WHERE {column}=:id"),
                                {"id": account_id},
                            ) == (before[table] if failure else 0), table
                        assert (
                            conn.scalar(
                                text(
                                    "SELECT count(*) FROM cashly.password_recovery_sessions "
                                    "WHERE auth_user_id=:id"
                                ),
                                {"id": identity.user_id},
                            )
                            == expected
                        )
                        assert conn.scalar(text("SELECT count(*) FROM cashly.expense_items")) == (
                            1 if expected else 0
                        )
                        assert (
                            conn.scalar(
                                text("SELECT count(*) FROM cashly.accounts WHERE id=:id"),
                                {"id": other_account.account_id},
                            )
                            == 1
                        )
                        assert (
                            conn.scalar(text("SELECT count(*) FROM cashly.expense_categories")) == 9
                        )
                        assert (
                            conn.scalar(
                                text(
                                    "SELECT has_function_privilege('anon', "
                                    "'cashly.purge_unlinked_account()', 'EXECUTE')"
                                )
                            )
                            is False
                        )
                    if not failure:
                        assert response.json() == {"data": {"status": "account_deleted"}}
                        assert api.get("/api/v1/me", headers=headers).status_code == 401
                finally:
                    db.dispose()
                    api.app.state.database = original
    finally:
        engine.dispose()


def test_deletion_requires_backend_secret(auth_postgres_settings):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    with auth_authority(identity.user_id) as (url, calls):
        settings = auth_postgres_settings.model_copy(
            update={"supabase_url": url, "supabase_publishable_key": SecretStr("synthetic-key")}
        )
        with TestClient(create_app(settings)) as api:
            bootstrap(api.app.state.database, settings, identity)
            response = api.request(
                "DELETE",
                "/api/v1/me/account",
                headers={"Authorization": "Bearer google-valid"},
                json={"confirmation": "HAPUS AKUN"},
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "ACCOUNT_DELETION_NOT_CONFIGURED"
            assert not any("/admin/users/" in call for call in calls)
