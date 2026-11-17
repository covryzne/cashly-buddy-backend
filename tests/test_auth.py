"""Loopback Auth stand-in + disposable PostgreSQL; no Supabase/SMTP calls."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from cashly.core.auth import Identity
from cashly.core.settings import Settings
from cashly.db.commands import upgrade_database
from cashly.db.connection import Database, build_engine
from cashly.main import create_app
from cashly.services.accounts import bootstrap, expenses


@pytest.fixture
def auth_postgres_settings(postgres_settings):
    """Each auth test owns its database; never pollute migration/RLS fixtures."""
    name = "oauth_" + uuid4().hex
    admin = build_engine(postgres_settings, migration=True)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = make_url(postgres_settings.database_url.get_secret_value()).set(database=name)
    secret = SecretStr(url.render_as_string(hide_password=False))
    settings = postgres_settings.model_copy(
        update={"database_url": secret, "migration_database_url": secret}
    )
    engine = build_engine(settings, migration=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA auth")
        conn.exec_driver_sql("CREATE TABLE auth.users (id uuid PRIMARY KEY)")
    engine.dispose()
    try:
        yield settings
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin.dispose()


@contextmanager
def auth_authority(user_id):
    calls = []
    state = {"email": "tester@example.invalid", "new_email": None, "current": False, "new": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            calls.append(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if body.get("email") == "taken@example.invalid":
                self.send_response(422)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"code":422,"error_code":"email_exists","msg":"private provider details"}'
                )
                return
            if body.get("email"):
                state.update(new_email=body["email"], current=False, new=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            calls.append(self.path)
            if self.path == "/auth/v1/logout?scope=local":
                self.send_response(204)
                self.end_headers()
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if body.get("email") == "outage@example.invalid":
                status, payload = 503, {}
            elif body.get("email") == "limited@example.invalid":
                status, payload = 429, {}
            elif self.path == "/auth/v1/recover":
                status, payload = 200, {}
            elif self.path == "/auth/v1/verify" and body.get("type") == "email_change":
                if body.get("email") == state["email"] and body.get("token") == "111111":
                    state["current"] = True
                elif body.get("email") == state["new_email"] and body.get("token") == "222222":
                    state["new"] = True
                else:
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"msg":"private OTP error"}')
                    return
                if state["current"] and state["new"]:
                    state["email"] = state["new_email"]
                    status, payload = 200, {"access_token": "email-only"}
                else:
                    status, payload = 200, {"msg": "Confirm the other email"}
            elif self.path == "/auth/v1/verify" and body.get("token") != "123456":
                status, payload = 403, {"msg": "private OTP details"}
            elif self.path == "/auth/v1/resend":
                assert body["type"] == "signup"
                status, payload = 200, {}
            elif body.get("email") == "unconfirmed@example.invalid":
                status, payload = 400, {"code": "email_not_confirmed"}
            elif body.get("password") == "Wrong-Pass999!":
                status, payload = 400, {"msg": "private provider details"}
            elif body.get("email") == "confirm@example.invalid":
                status, payload = 200, {"user": {"id": str(user_id)}}
            else:
                status, payload = (
                    200,
                    {
                        "access_token": "email-only",
                        "refresh_token": "synthetic-refresh",
                        "expires_in": 3600,
                    },
                )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            calls.append(self.path)
            token = self.headers.get("Authorization")
            if token == "Bearer upstream-down":
                status, payload = 503, {}
            elif token in {"Bearer google-valid", "Bearer unverified", "Bearer email-only"}:
                status, payload = (
                    200,
                    {
                        "id": str(user_id),
                        "email": state["email"],
                        "email_confirmed_at": None
                        if token == "Bearer unverified"
                        else "2026-11-16T00:00:00Z",
                        "identities": [
                            {"provider": "email" if token == "Bearer email-only" else "google"}
                        ],
                        "user_metadata": {"full_name": "Google Tester", "account_id": str(uuid4())},
                    },
                )
            else:
                status, payload = 401, {"message": "private upstream details"}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_email_otp_errors_and_no_implicit_session():
    with auth_authority(uuid4()) as (url, calls):
        settings = Settings(
            environment="dev", supabase_url=url, supabase_publishable_key=SecretStr("synthetic-key")
        )
        with TestClient(create_app(settings)) as client:
            for path in ("verify", "resend"):
                response = client.post(f"/api/v1/auth/email/{path}", json={"email": "bad"})
                assert response.status_code == 422
            assert not calls
            for token in ("12345", "abcdef", "１２３４５６"):
                response = client.post(
                    "/api/v1/auth/email/verify",
                    json={
                        "email": "tester@example.invalid",
                        "token": token,
                    },
                )
                assert response.status_code == 422
                assert token not in response.text
            wrong = client.post(
                "/api/v1/auth/email/verify",
                json={
                    "email": "tester@example.invalid",
                    "token": "000000",
                },
            )
            assert wrong.status_code == 400
            assert wrong.json()["error"]["code"] == "EMAIL_OTP_INVALID"
            assert "private" not in wrong.text
            verified = client.post(
                "/api/v1/auth/email/verify",
                json={
                    "email": "tester@example.invalid",
                    "token": "123456",
                },
            )
            assert verified.status_code == 200
            assert verified.json()["data"]["status"] == "registered"
            assert verified.json()["data"]["session"] is None
            assert "synthetic-refresh" not in verified.text
            assert "/auth/v1/user" in calls
            assert "/auth/v1/logout?scope=local" in calls
            assert verified.headers["cache-control"] == "private, no-store"
            assert not verified.headers.get("set-cookie")
            assert (
                client.post(
                    "/api/v1/auth/email/resend",
                    json={
                        "email": "unknown@example.invalid",
                    },
                ).status_code
                == 202
            )
            for path in ("verify", "resend"):
                for email, expected in (
                    ("limited@example.invalid", 429),
                    ("outage@example.invalid", 503),
                ):
                    body = {"email": email}
                    if path == "verify":
                        body["token"] = "123456"
                    assert (
                        client.post(f"/api/v1/auth/email/{path}", json=body).status_code == expected
                    )
            unconfirmed = client.post(
                "/api/v1/auth/login",
                json={
                    "email": "unconfirmed@example.invalid",
                    "password": "Test-Pass123!",
                },
            )
            assert unconfirmed.status_code == 403
            assert unconfirmed.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def prepare(settings, identities):
    upgrade_database(settings)
    engine = build_engine(settings, migration=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(Path("sql/runtime_role.sql").read_text(encoding="utf-8"))
        for identity in identities:
            conn.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": identity.user_id})
    engine.dispose()


def runtime_database(settings):
    db = Database(settings)
    # All application transactions run as the real restricted runtime role.
    event.listen(
        db.engine, "begin", lambda conn: conn.exec_driver_sql("SET LOCAL ROLE cashly_runtime")
    )
    return db


def test_http_auth_and_account_reads(auth_postgres_settings):
    postgres_settings = auth_postgres_settings
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    prepare(postgres_settings, [identity])
    with auth_authority(identity.user_id) as (authority, calls):
        settings = postgres_settings.model_copy(
            update={
                "supabase_url": authority,
                "supabase_publishable_key": SecretStr("synthetic-key"),
                "dev_sample_data": True,
            }
        )
        with TestClient(create_app(settings)) as client:
            original = client.app.state.database
            db = runtime_database(settings)
            client.app.state.database = db
            try:
                assert client.get("/api/v1/me").status_code == 401
                assert not calls
                for token, expected in [
                    ("forged", 401),
                    ("unverified", 403),
                    ("upstream-down", 503),
                ]:
                    response = client.post(
                        "/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {token}"}
                    )
                    assert response.status_code == expected
                    assert "private upstream" not in response.text
                    assert response.headers["cache-control"] == "private, no-store"
                headers = {"Authorization": "Bearer google-valid"}
                assert client.get("/api/v1/me", headers=headers).status_code == 409
                first = client.post("/api/v1/auth/bootstrap", headers=headers)
                assert first.status_code == 200, first.text
                assert first.json()["data"]["display_name"] == "Google Tester"
                assert first.json()["data"]["sample_data"] is False
                assert client.post("/api/v1/auth/bootstrap", headers=headers).json() == first.json()
                email_account = client.post(
                    "/api/v1/auth/bootstrap", headers={"Authorization": "Bearer email-only"}
                ).json()["data"]
                assert email_account["account_id"] == first.json()["data"]["account_id"]
                assert email_account["username"] == first.json()["data"]["username"]
                assert client.get("/api/v1/me", headers=headers).json() == first.json()
                records = client.get("/api/v1/expenses", headers=headers).json()
                assert len(records["data"]) == 0
                assert not records["has_more"]
                assert all(isinstance(row["amount_idr"], str) for row in records["data"])
                daily_headers = {**headers, "Idempotency-Key": str(uuid4())}
                checked = client.post("/api/v1/gamification/checkins/review", headers=daily_headers)
                assert checked.status_code == 200, checked.text
                day_data = checked.json()["data"]
                assert day_data["today_checked_in"] and day_data["hero_level"] == 1
                assert (
                    next(
                        day
                        for day in day_data["calendar"]
                        if day["date"] == day_data["today_local_date"]
                    )["status"]
                    == "active"
                )
                assert (
                    client.post(
                        "/api/v1/gamification/checkins/review", headers=daily_headers
                    ).json()
                    == checked.json()
                )
                duplicate = client.post(
                    "/api/v1/gamification/checkins/review",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                )
                assert duplicate.json()["data"]["hero_level"] == 1
                assert set(calls) == {"/auth/v1/user"}
            finally:
                db.dispose()
                client.app.state.database = original


def test_verified_email_can_prepare_account_and_unverified_email_is_rejected():
    from cashly.core.settings import Settings

    with auth_authority(uuid4()) as (authority, _):
        settings = Settings(
            environment="staging",
            supabase_url=authority,
            supabase_publishable_key=SecretStr("synthetic-key"),
        )
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/v1/auth/bootstrap", headers={"Authorization": "Bearer email-only"}
            )
            # Verified email is accepted; no database is configured in this fixture.
            assert response.status_code == 503
            assert (
                client.post(
                    "/api/v1/auth/bootstrap", headers={"Authorization": "Bearer unverified"}
                ).status_code
                == 403
            )


def test_bootstrap_concurrency_isolation_and_production_seed_guard(auth_postgres_settings):
    postgres_settings = auth_postgres_settings
    identities = [
        Identity(uuid4(), f"test{i}@example.invalid", f"Tester {i}", datetime.now(UTC))
        for i in range(3)
    ]
    settings = postgres_settings.model_copy(update={"db_pool_size": 4, "dev_sample_data": True})
    prepare(settings, identities)
    db = runtime_database(settings)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            accounts = list(pool.map(lambda _: bootstrap(db, settings, identities[0]), range(4)))
        assert len({account.account_id for account in accounts}) == 1
        assert len(expenses(db, identities[0]).data) == 0
        second = bootstrap(db, settings, identities[1])
        assert second.account_id != accounts[0].account_id
        first_ids = {row.id for row in expenses(db, identities[0]).data}
        assert not first_ids.intersection(row.id for row in expenses(db, identities[1]).data)
        production = settings.model_copy(update={"environment": "prod"})
        assert not bootstrap(db, production, identities[2]).sample_data
        assert not expenses(db, identities[2]).data
        with db.transaction(
            auth_user_id=identities[0].user_id, account_id=accounts[0].account_id
        ) as session:
            session.execute(
                text("UPDATE cashly.accounts SET status='disabled' WHERE id=:id"),
                {"id": accounts[0].account_id},
            )
        with pytest.raises(HTTPException) as error:
            bootstrap(db, settings, identities[0])
        assert error.value.status_code == 403
    finally:
        db.dispose()


def test_register_and_login_prepare_account_with_documented_password_body(auth_postgres_settings):
    identity = Identity(uuid4(), "tester@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    with auth_authority(identity.user_id) as (authority, calls):
        settings = auth_postgres_settings.model_copy(
            update={
                "supabase_url": authority,
                "supabase_publishable_key": SecretStr("sb_publishable_synthetic"),
                "dev_sample_data": True,
            }
        )
        with TestClient(create_app(settings)) as client:
            body = {
                "name": "New Tester",
                "email": "tester@example.invalid",
                "password": "Cashly-Test123!",
            }
            created = client.post("/api/v1/auth/register", json=body)
            assert created.status_code == 201, created.text
            data = created.json()["data"]
            assert data["status"] == "registered"
            assert data["session"] is None
            credentials = {"email": body["email"], "password": body["password"]}
            login = client.post("/api/v1/auth/login", json=credentials)
            assert login.status_code == 200
            assert login.json()["data"]["account"]["account_id"] == data["account"]["account_id"]
            assert (
                client.get("/api/v1/me", headers={"Authorization": "Bearer email-only"}).status_code
                == 200
            )
            assert "/auth/v1/signup" in calls
            assert "/auth/v1/token?grant_type=password" in calls
            assert (
                client.post(
                    "/api/v1/auth/login", json={**credentials, "password": "Wrong-Pass999!"}
                ).status_code
                == 401
            )
            confirm = client.post(
                "/api/v1/auth/register", json={**body, "email": "confirm@example.invalid"}
            )
            assert confirm.status_code == 202
            assert confirm.json()["data"]["session"] is None
            assert confirm.json()["data"]["account"] is None
            for email, status in [
                ("outage@example.invalid", 503),
                ("limited@example.invalid", 429),
            ]:
                failed = client.post("/api/v1/auth/login", json={**credentials, "email": email})
                assert failed.status_code == status
                assert body["password"] not in failed.text
            assert (
                client.post(
                    "/api/v1/auth/register", json={**body, "account_id": str(uuid4())}
                ).status_code
                == 422
            )
            config = client.get("/api/v1/auth/configuration")
            assert config.status_code == 200
            assert config.json()["local"] is True
            schema = client.get("/openapi.json").json()
            assert "requestBody" in schema["paths"]["/api/v1/auth/register"]["post"]
            assert "security" not in schema["paths"]["/api/v1/auth/register"]["post"]
            assert "security" in schema["paths"]["/api/v1/me"]["get"]


def test_auth_configuration_never_returns_secret_or_service_role_keys():
    from cashly.core.settings import Settings

    for key in ["sb_secret_private-test", "not-a-publishable-key"]:
        settings = Settings(
            environment="dev",
            supabase_url="http://127.0.0.1:54321",
            supabase_publishable_key=SecretStr(key),
        )
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/auth/configuration")
            assert response.status_code == 503
            assert key not in response.text


def test_details_upgrade_and_transactional_local_cleanup(auth_postgres_settings):
    from alembic import command
    from sqlalchemy.exc import IntegrityError

    from cashly.db.commands import alembic_config
    from cashly.db.local_data import local_data

    settings = auth_postgres_settings
    command.upgrade(alembic_config(settings), "0001_core")
    engine = build_engine(settings, migration=True)
    account_id, auth_id, expense_id = (uuid4() for _ in range(3))
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": auth_id})
            conn.execute(text("INSERT INTO cashly.accounts(id) VALUES (:id)"), {"id": account_id})
            conn.execute(
                text(
                    "INSERT INTO cashly.profiles(account_id,display_name) "
                    "VALUES (:id,'Local Tester')"
                ),
                {"id": account_id},
            )
            conn.execute(
                text(
                    "INSERT INTO cashly.account_auth_links(auth_user_id,account_id,verified_at) "
                    "VALUES (:auth,:account,now())"
                ),
                {"auth": auth_id, "account": account_id},
            )
            conn.execute(
                text(
                    "INSERT INTO cashly.expenses(id,account_id,amount_idr,"
                    "occurred_at,category_code,"
                    "description,source_platform,source_type,source_message_id,timestamp_basis) "
                    "VALUES (:id,:account,25000,now(),'food_drink','Fixture','dashboard',"
                    "'dashboard','fixture-source','user_explicit')"
                ),
                {"id": expense_id, "account": account_id},
            )
            conn.execute(
                text(
                    "INSERT INTO cashly.expense_items(id,expense_id,item_index,label,amount_idr) "
                    "VALUES (:id,:expense,0,'Legacy line',25000)"
                ),
                {"id": uuid4(), "expense": expense_id},
            )
            conn.execute(
                text(
                    "INSERT INTO cashly.idempotency_records(id,account_id,operation,key,"
                    "request_fingerprint,response_status,response_body) "
                    "VALUES (:id,:account,'dev_samples','sample-v1',:hash,200,'{}')"
                ),
                {"id": uuid4(), "account": account_id, "hash": "a" * 64},
            )
        upgrade_database(settings)
        with engine.connect() as conn:
            assert tuple(
                conn.execute(
                    text("SELECT quantity,unit_price_idr,amount_idr FROM cashly.expense_items")
                ).one()
            ) == (1, 25000, 25000)
            assert tuple(
                conn.execute(
                    text("SELECT discount_idr,tax_idr,amount_idr FROM cashly.expenses")
                ).one()
            ) == (0, 0, 25000)
        # Real constraints reject invalid item arithmetic, quantity and adjustments.
        for query in (
            "UPDATE cashly.expense_items SET quantity=0",
            "UPDATE cashly.expense_items SET unit_price_idr=-1",
            "UPDATE cashly.expense_items SET quantity=2",
            "UPDATE cashly.expenses SET discount_idr=-1",
            "UPDATE cashly.expenses SET tax_idr=-1",
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text(query))
        with engine.begin() as conn:
            conn.execute(text("UPDATE cashly.expense_items SET quantity=5,unit_price_idr=5000"))
            conn.execute(text("UPDATE cashly.expenses SET discount_idr=2000,tax_idr=1000"))
        # Round-trip downgrade preserves totals and upgrade backfills legacy details.
        command.downgrade(alembic_config(settings), "0001_core")
        upgrade_database(settings)
        # Only this test bypasses the fixed port guard: its database is disposable
        # on a random port. Production script always enforces loopback:54322.
        with patch("cashly.db.local_data.verify_local_target"):
            first = local_data(settings, "seed-reference")
            assert first["after"]["expense_categories"] == 9
            assert local_data(settings, "seed-reference")["after"] == first["after"]
            # Failure after item deletion must roll back every deletion.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE FUNCTION cashly.block_cleanup() RETURNS trigger LANGUAGE plpgsql "
                        "AS $$ BEGIN RAISE EXCEPTION 'fixture failure'; END $$"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TRIGGER fixture_block BEFORE DELETE ON cashly.expenses "
                        "FOR EACH ROW EXECUTE FUNCTION cashly.block_cleanup()"
                    )
                )
            from cashly.core.environment import ConfigurationError

            with pytest.raises(ConfigurationError, match="rolled back"):
                local_data(settings, "reset", confirm="cashly-local-transactions")
            assert local_data(settings, "inspect")["after"] == first["after"]
            with engine.begin() as conn:
                conn.execute(text("DROP TRIGGER fixture_block ON cashly.expenses"))
                conn.execute(text("DROP FUNCTION cashly.block_cleanup()"))
            result = local_data(settings, "reset", confirm="cashly-local-transactions")
            for table in ("expenses", "expense_items", "idempotency_records"):
                assert result["after"][table] == 0
            for table in ("accounts", "profiles", "account_auth_links", "auth_users"):
                assert result["after"][table] == result["before"][table] == 1
            assert result["after"]["expense_categories"] == 9
            assert (
                local_data(settings, "reset", confirm="cashly-local-transactions")["after"]
                == result["after"]
            )
    finally:
        engine.dispose()


def test_finance_api_crud_rewards_replay_and_ownership(auth_postgres_settings):
    from cashly.core.errors import ApiError
    from cashly.services import finance

    identities = [
        Identity(uuid4(), f"crud{i}@example.invalid", "CRUD Tester", datetime.now(UTC))
        for i in range(2)
    ]
    prepare(auth_postgres_settings, identities)
    with auth_authority(identities[0].user_id) as (authority, _):
        settings = auth_postgres_settings.model_copy(
            update={
                "weekly_review_test_mode": True,
                "supabase_url": authority,
                "supabase_publishable_key": SecretStr("synthetic-key"),
            }
        )
        db = runtime_database(settings)
        bootstrap(db, settings, identities[0])
        bootstrap(db, settings, identities[1])
        try:
            with TestClient(create_app(settings)) as api:
                original = api.app.state.database
                api.app.state.database = db
                try:
                    headers = {
                        "Authorization": "Bearer google-valid",
                        "Idempotency-Key": str(uuid4()),
                    }
                    body = {
                        "merchant": "CRUD QA",
                        "description": "Two coffees",
                        "category_code": "food_drink",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "payment_method": "qris",
                        "amount_idr": "29000",
                        "discount_idr": "2000",
                        "tax_idr": "1000",
                        "items": [{"label": "Coffee", "quantity": 2, "unit_price_idr": "15000"}],
                    }
                    assert api.post("/api/v1/expenses", json=body).status_code == 401
                    created = api.post("/api/v1/expenses", json=body, headers=headers)
                    assert created.status_code == 201, created.text
                    record = created.json()["data"]
                    identifier = record["id"]
                    assert record["items"][0]["quantity"] == 2
                    assert (
                        api.post("/api/v1/expenses", json=body, headers=headers).json()
                        == created.json()
                    )
                    changed = {**body, "merchant": "Different"}
                    assert (
                        api.post("/api/v1/expenses", json=changed, headers=headers).status_code
                        == 409
                    )
                    game = api.get("/api/v1/gamification/overview", headers=headers).json()["data"]
                    assert game["hero_level"] == 1 and game["current_streak"] == 1
                    assert game["hero_level"] == 1
                    assert (
                        api.get(f"/api/v1/expenses/{identifier}", headers=headers).status_code
                        == 200
                    )
                    other = finance.list_expenses(db, identities[1])
                    assert other["data"] == []
                    with pytest.raises(ApiError) as denied:
                        finance.detail(db, identities[1], UUID(identifier))
                    assert denied.value.status_code == 404
                    edits = {**body, "merchant": "Edited QA", "version": 1}
                    edit_headers = {**headers, "Idempotency-Key": str(uuid4())}
                    updated = api.patch(
                        f"/api/v1/expenses/{identifier}", json=edits, headers=edit_headers
                    )
                    assert updated.status_code == 200, updated.text
                    assert updated.json()["data"]["version"] == 2
                    assert (
                        api.patch(
                            f"/api/v1/expenses/{identifier}",
                            json=edits,
                            headers={**headers, "Idempotency-Key": str(uuid4())},
                        ).status_code
                        == 409
                    )
                    summary = api.get("/api/v1/reports/summary", headers=headers).json()["data"]
                    assert summary["amount_idr"] == "29000" and summary["count"] == 1
                    assert (
                        api.post(
                            "/api/v1/gamification/checkins/no-spend", headers=headers
                        ).status_code
                        == 409
                    )
                    review = api.post("/api/v1/reports/weekly-story/complete", headers=headers)
                    assert review.status_code == 200, review.text
                    assert review.json()["data"]["hero_level"] == 1
                    assert (
                        api.post("/api/v1/reports/weekly-story/complete", headers=headers).json()
                        == review.json()
                    )
                    deleted = api.delete(
                        f"/api/v1/expenses/{identifier}?version=2", headers=headers
                    )
                    assert deleted.status_code == 200, deleted.text
                    assert api.get("/api/v1/expenses", headers=headers).json()["data"] == []
                    assert (
                        api.get("/api/v1/reports/summary", headers=headers).json()["data"][
                            "amount_idr"
                        ]
                        == "0"
                    )
                    restored = api.post(f"/api/v1/expenses/{identifier}/restore", headers=headers)
                    assert restored.status_code == 200, restored.text
                    assert restored.json()["data"]["items"][0]["unit_price_idr"] == "15000"
                    assert (
                        api.get("/api/v1/gamification/overview", headers=headers).json()["data"][
                            "hero_level"
                        ]
                        == 1
                    )
                    invalid = {**body, "amount_idr": "1"}
                    assert (
                        api.post("/api/v1/expenses", json=invalid, headers=headers).status_code
                        == 422
                    )
                    assert (
                        api.get("/api/v1/expenses?cursor=malformed", headers=headers).status_code
                        == 422
                    )
                finally:
                    api.app.state.database = original
        finally:
            db.dispose()


def test_seed_scenario_repeat_and_manual_freeze(auth_postgres_settings):
    import runpy
    from datetime import timedelta

    from cashly.services import finance, gamification

    identity = Identity(uuid4(), "seed@example.invalid", "Seed Tester", datetime.now(UTC))
    settings = auth_postgres_settings
    prepare(settings, [identity])
    db = runtime_database(settings)
    try:
        account = bootstrap(db, settings, identity)
        seed = runpy.run_path(str(Path("scripts/seed_development.py")))["seed_scenario"]
        result = seed(db, identity)
        assert result["gamification"]["hero_level"] == 1
        assert result["gamification"]["current_streak"] == 12
        assert result["gamification"]["hero_level"] == 1
        assert result["gamification"]["hero_level"] == 1
        assert result["gamification"]["protection_remaining"] == 5
        assert seed(db, identity)["already_seeded"]
        # Exercise the explicit dev-only review reset on this disposable account.
        from cashly.services.accounts import load_account
        from cashly.services.gamification import record, today

        day = today("Asia/Jakarta")
        with db.transaction(auth_user_id=identity.user_id) as session:
            load_account(session, identity)
            record(
                session,
                account.account_id,
                "Asia/Jakarta",
                f"weekly:{day - timedelta(days=day.weekday())}",
                "weekly_review",
                day,
                xp=40,
            )
        admin = build_engine(settings, migration=True)
        try:
            with admin.begin() as conn:
                conn.execute(text("ALTER TABLE auth.users ADD COLUMN email text"))
                conn.execute(
                    text("UPDATE auth.users SET email=:email WHERE id=:id"),
                    {"email": identity.email, "id": identity.user_id},
                )
            reset = runpy.run_path(str(Path("scripts/reset_weekly_review.py")))["reset_weekly"]
            # Fixed-port guard is bypassed only for this disposable random-port DB.
            reset.__globals__["verify_local_target"] = lambda _: None
            assert reset(settings, identity.email)["weekly_reviews_reset"] == 1
            assert reset(settings, identity.email)["weekly_reviews_reset"] == 0
            with db.transaction(auth_user_id=identity.user_id) as session:
                load_account(session, identity)
                assert (
                    gamification.overview(session, account.account_id, "Asia/Jakarta")["hero_level"]
                    == 1
                )
        finally:
            admin.dispose()
        reset_daily = runpy.run_path(str(Path("scripts/reset_daily_checkin.py")))["reset_daily"]
        reset_daily.__globals__["verify_local_target"] = lambda _: None
        from cashly.core.environment import ConfigurationError

        with pytest.raises(ConfigurationError, match="DAILY_CHECKIN_TEST_MODE"):
            reset_daily(settings, identity.email)
        daily_settings = settings.model_copy(update={"daily_checkin_test_mode": True})
        before_rows = len(finance.list_expenses(db, identity)["data"])
        assert reset_daily(daily_settings, identity.email)["daily_checkins_reset"] == 1
        assert reset_daily(daily_settings, identity.email)["daily_checkins_reset"] == 0
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            after = gamification.overview(session, account.account_id, "Asia/Jakarta")
            assert not after["today_checked_in"] and after["hero_level"] == 1
            assert after["hero_level"] == 1
            gamification.action(session, account.account_id, "Asia/Jakarta", "review")
            assert (
                gamification.overview(session, account.account_id, "Asia/Jakarta")["hero_level"]
                == 1
            )
        assert len(finance.list_expenses(db, identity)["data"]) == before_rows
        rows = finance.list_expenses(db, identity, limit=5)
        assert len(rows["data"]) == 5 and rows["has_more"]
        second = finance.list_expenses(db, identity, limit=5, cursor=rows["next_cursor"])
        assert not {r["id"] for r in rows["data"]}.intersection(r["id"] for r in second["data"])
        first = UUID(rows["data"][0]["id"])
        finance.mutate(db, identity, "delete", str(uuid4()), identifier=first)
        seed(db, identity)
        assert len(finance.list_expenses(db, identity)["data"]) == 15
        # Create a recoverable gap through yesterday in this disposable DB only.
        engine = build_engine(settings, migration=True)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM cashly.game_events WHERE account_id=:id"),
                    {"id": account.account_id},
                )
            day = gamification.today("Asia/Jakarta")
            with db.transaction(auth_user_id=identity.user_id) as session:
                finance.locked_account(session, identity)
                for offset in reversed(range(2, 16)):
                    gamification.checkin(
                        session, account.account_id, "Asia/Jakarta", day - timedelta(days=offset)
                    )
                before = gamification.overview(session, account.account_id, "Asia/Jakarta")
                assert before["protection_remaining"] == 5 and before["recovery"]
                restored = gamification.action(
                    session, account.account_id, "Asia/Jakarta", "freeze", [day - timedelta(days=1)]
                )
                assert restored["protection_remaining"] == 4 and restored["hero_level"] == 1
                assert restored["current_streak"] == 15
                again = gamification.action(
                    session, account.account_id, "Asia/Jakarta", "freeze", [day - timedelta(days=1)]
                )
                assert again == restored
                assert restored["hero_level"] == 1
        finally:
            engine.dispose()
    finally:
        db.dispose()


def test_calendar_is_current_local_week_monday_to_sunday(auth_postgres_settings):
    from datetime import date

    from cashly.services import finance, gamification

    identity = Identity(uuid4(), "week@example.invalid", "Week Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    db = runtime_database(auth_postgres_settings)
    try:
        account = bootstrap(db, auth_postgres_settings, identity)
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            wednesday = date(2026, 11, 18)
            before = gamification.overview(session, account.account_id, "Asia/Jakarta", wednesday)
            assert before["calendar"][0]["date"] == "2026-11-16"
            assert before["calendar"][-1]["date"] == "2026-11-22"
            assert [d["status"] for d in before["calendar"]] == ["missed"] * 3 + ["upcoming"] * 4
            assert not before["today_checked_in"]
            gamification.checkin(session, account.account_id, "Asia/Jakarta", wednesday)
            after = gamification.overview(session, account.account_id, "Asia/Jakarta", wednesday)
            assert after["today_checked_in"] and after["calendar"][2]["status"] == "active"
            assert after["today_local_date"] == "2026-11-18"
            assert after["timezone"] == "Asia/Jakarta"
            gamification.checkin(session, account.account_id, "Asia/Jakarta", wednesday)
            assert (
                gamification.overview(session, account.account_id, "Asia/Jakarta", wednesday)[
                    "hero_level"
                ]
                == 1
            )
            next_week = gamification.overview(
                session, account.account_id, "Asia/Jakarta", date(2026, 11, 23)
            )
            assert next_week["calendar"][0]["date"] == "2026-11-23"
            assert next_week["calendar"][-1]["date"] == "2026-11-29"
    finally:
        db.dispose()


def test_weekend_review_policy_and_local_test_override(auth_postgres_settings):
    from datetime import date

    from cashly.core.errors import ApiError
    from cashly.services import finance, gamification

    identity = Identity(uuid4(), "weekend@example.invalid", "Weekend Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    db = runtime_database(auth_postgres_settings)
    try:
        account = bootstrap(db, auth_postgres_settings, identity)
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            for day, allowed in [
                (date(2026, 11, 20), False),
                (date(2026, 11, 21), True),
                (date(2026, 11, 22), True),
                (date(2026, 11, 23), False),
            ]:
                result = gamification.overview(session, account.account_id, "Asia/Jakarta", day)
                weekly = result["missions"][1]
                assert weekly["can_complete"] is allowed
                assert weekly["test_override"] is False
            monday = date(2026, 11, 16)
            with patch("cashly.services.gamification.today", return_value=monday):
                with pytest.raises(ApiError) as blocked:
                    gamification.action(session, account.account_id, "Asia/Jakarta", "weekly")
                assert blocked.value.code == "WEEKLY_REVIEW_NOT_AVAILABLE"
                settings = auth_postgres_settings.model_copy(
                    update={"weekly_review_test_mode": True}
                )
                enabled = gamification.overview(
                    session, account.account_id, "Asia/Jakarta", monday, settings
                )
                assert (
                    enabled["missions"][1]["can_complete"]
                    and enabled["missions"][1]["test_override"]
                )
                with pytest.raises(ApiError) as empty:
                    gamification.action(
                        session, account.account_id, "Asia/Jakarta", "weekly", settings=settings
                    )
                assert empty.value.code == "REVIEW_EMPTY"
    finally:
        db.dispose()
