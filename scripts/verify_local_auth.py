"""Explicit live-local smoke test; creates two fictional QA accounts. No env file reads."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cashly.core.settings import Settings
from cashly.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def main():
    status = subprocess.run(
        ["node", "node_modules/supabase/dist/supabase.js", "status", "-o", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if status.returncode:
        raise RuntimeError("Local Supabase status unavailable; run npm run supabase:start in BE")
    values = json.loads(status.stdout)
    if values.get("API_URL") != "http://127.0.0.1:54321":
        raise RuntimeError("Expected the approved local Supabase endpoint")
    key = values.get("PUBLISHABLE_KEY")
    if not key:
        raise RuntimeError("Local publishable key unavailable")
    settings = Settings(
        environment="dev",
        supabase_url=values["API_URL"],
        supabase_publishable_key=SecretStr(key),
        database_url=SecretStr(
            "postgresql+psycopg://cashly_app:cashly-local-only@127.0.0.1:54322/postgres?sslmode=disable"
        ),
        dev_sample_data=True,
    )
    with (
        httpx2.Client(timeout=15, trust_env=False) as auth,
        TestClient(create_app(settings)) as api,
    ):
        accounts, expenses = [], []
        for index in range(2):
            email = f"cashly-qa-{uuid4().hex[:12]}@example.com"
            password = "Local-QA123!"
            response = auth.post(
                settings.supabase_url + "/auth/v1/signup",
                headers={"apikey": key},
                json={
                    "email": email,
                    "password": password,
                    "data": {"full_name": f"Local QA {index + 1}"},
                },
            )
            assert response.status_code == 200, "Local signup failed"
            signup = response.json()
            assert signup.get("access_token"), (
                "Local signup must return an active session without email confirmation"
            )
            assert signup["user"].get("email_confirmed_at"), (
                "Provider must report the local identity confirmed"
            )
            login = auth.post(
                settings.supabase_url + "/auth/v1/token?grant_type=password",
                headers={"apikey": key},
                json={"email": email, "password": password},
            )
            assert login.status_code == 200, "Local password login failed"
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            ready = api.post("/api/v1/auth/bootstrap", headers=headers)
            assert ready.status_code == 200, (
                "Account bootstrap failed against the real local provider/database"
            )
            account = ready.json()["data"]
            assert (
                api.post("/api/v1/auth/bootstrap", headers=headers).json()["data"]["account_id"]
                == account["account_id"]
            )
            assert api.get("/api/v1/me", headers=headers).json()["data"]["email"] == email
            rows = api.get("/api/v1/expenses", headers=headers).json()["data"]
            assert rows == []
            accounts.append(account["account_id"])
            expenses.append({row["id"] for row in rows})
        assert accounts[0] != accounts[1]
        assert expenses[0].isdisjoint(expenses[1])
    print(
        "PASS: real local signup/password login, immediate activation, bootstrap retry, "
        "empty accounts (no automatic seeding) and distinct account identities"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not print tokens, local status, passwords, request bodies or DB exceptions.
        print(
            f"Local verification failed ({type(error).__name__}); inspect local service readiness.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
