import json
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cashly.core.logging import configure_logging
from cashly.core.settings import Settings
from cashly.main import create_app


@pytest.fixture
def app():
    return create_app(Settings(environment="dev", cors_origins=("http://localhost:3000",)))


def test_health_is_liveness_only_and_request_ids_are_server_generated(app):
    with TestClient(app) as client:
        first = client.get("/api/v1/health", headers={"X-Request-ID": "untrusted"})
        second = client.get("/api/v1/health")
    assert first.status_code == 200
    assert first.json() == {"data": {"status": "ok", "service": "cashly-api"}}
    assert UUID(first.headers["x-request-id"])
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_missing_path_has_contract_error(app):
    response = TestClient(app).get("/missing")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert error["details"] == {}
    assert error["request_id"] == response.headers["x-request-id"]


def test_wrong_method_has_contract_error(app):
    response = TestClient(app).post("/api/v1/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == "GET"


class SampleInput(BaseModel):
    amount: int


def test_validation_does_not_echo_body_or_secret(app):
    @app.post("/test-validation")
    async def validation(body: SampleInput):
        return body

    response = TestClient(app).post("/test-validation", json={"amount": "private-secret"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "private-secret" not in response.text
    assert "input" not in response.json()["error"]["details"]["issues"][0]


def test_unexpected_error_is_sanitized_and_has_cors(app, capsys):
    configure_logging("INFO")

    @app.get("/test-failure")
    async def failure():
        raise RuntimeError("private-secret")

    response = TestClient(app).get(
        "/test-failure?token=private-secret",
        headers={
            "Origin": "http://localhost:3000",
            "Authorization": "Bearer private-secret",
        },
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "private-secret" not in response.text
    logs = capsys.readouterr().err
    assert "private-secret" not in logs
    assert "RuntimeError" in logs
    assert "request_completed" in logs
    for line in logs.splitlines():
        json.loads(line)


def test_http_exception_does_not_echo_private_detail(app):
    @app.get("/test-forbidden")
    async def forbidden():
        raise HTTPException(403, detail="private-secret")

    response = TestClient(app).get("/test-forbidden")
    assert response.status_code == 403
    assert "private-secret" not in response.text


def test_cors_allowlist_and_preflight(app):
    client = TestClient(app)
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,idempotency-key,content-type",
    }
    allowed = client.options("/api/v1/health", headers=headers)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in allowed.headers
    denied = client.options("/api/v1/health", headers={**headers, "Origin": "https://evil.test"})
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
    denied_read = client.get("/api/v1/health", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in denied_read.headers


def test_runtime_openapi_does_not_claim_unimplemented_endpoints(app):
    schema = TestClient(app).get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/api/v1/health",
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/email/verify",
        "/api/v1/auth/email/resend",
        "/api/v1/auth/configuration",
        "/api/v1/me",
        "/api/v1/expenses",
        "/api/v1/expenses/{identifier}",
        "/api/v1/expenses/{identifier}/restore",
        "/api/v1/categories",
        "/api/v1/me/preferences",
        "/api/v1/gamification/overview",
        "/api/v1/gamification/missions",
        "/api/v1/gamification/streak/calendar",
        "/api/v1/gamification/checkins/no-spend",
        "/api/v1/gamification/checkins/review",
        "/api/v1/gamification/streak/freeze",
        "/api/v1/reports/summary",
        "/api/v1/reports/trend",
        "/api/v1/reports/weekly-story",
        "/api/v1/reports/weekly-story/complete",
    }
    assert "HealthEnvelope" in schema["components"]["schemas"]


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_docs_disabled_outside_dev(environment):
    client = TestClient(create_app(Settings(environment=environment)))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/api/v1/health").status_code == 200
