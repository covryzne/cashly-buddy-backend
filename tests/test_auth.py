"""Loopback Auth stand-in + disposable PostgreSQL; no Supabase/SMTP calls."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from cashly.core.auth import Identity
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

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

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
                        "email": "tester@example.invalid",
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
                assert first.json()["data"]["sample_data"] is True
                assert client.post("/api/v1/auth/bootstrap", headers=headers).json() == first.json()
                assert (
                    client.post(
                        "/api/v1/auth/bootstrap", headers={"Authorization": "Bearer email-only"}
                    ).json()
                    == first.json()
                )
                assert client.get("/api/v1/me", headers=headers).json() == first.json()
                records = client.get("/api/v1/expenses", headers=headers).json()
                assert len(records["data"]) == 6
                assert not records["has_more"]
                assert all(isinstance(row["amount_idr"], str) for row in records["data"])
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
        assert len(expenses(db, identities[0]).data) == 6
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
