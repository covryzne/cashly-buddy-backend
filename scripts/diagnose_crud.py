"""Opt-in staging CRUD smoke/performance check; credentials only in memory/env."""

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx2  # noqa: E402

from cashly.core.environment import resolve_environment  # noqa: E402
from cashly.core.settings import Settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8017")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assert urlparse(args.base_url).hostname in {"localhost", "127.0.0.1"}
    settings = Settings.from_selection(
        resolve_environment(root=Path(__file__).resolve().parents[1], override="staging")
    )
    email = os.environ.get("CASHLY_TEST_EMAIL") or input("Staging email: ")
    password = os.environ.get("CASHLY_TEST_PASSWORD") or getpass.getpass("Staging password: ")
    metrics = []
    with httpx2.Client(timeout=45, trust_env=False) as client:
        login = client.post(
            settings.supabase_url.rstrip("/") + "/auth/v1/token?grant_type=password",
            headers={"apikey": settings.supabase_publishable_key.get_secret_value()},
            json={"email": email, "password": password},
        )
        if login.status_code != 200:
            raise SystemExit(f"Staging login HTTP {login.status_code}")
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}

        def call(method, path, body=None, key=None):
            started = perf_counter()
            response = client.request(
                method,
                args.base_url + "/api/v1/" + path,
                headers={**headers, "Idempotency-Key": key or str(uuid4())},
                json=body,
            )
            route = "/".join(
                ":id" if len(part) == 36 else part for part in path.split("?")[0].split("/")
            )
            entry = {
                "method": method,
                "route": route,
                "status": response.status_code,
                "duration_ms": round((perf_counter() - started) * 1000, 1),
                "server_timing": response.headers.get("server-timing"),
                "request_id": response.headers.get("x-request-id"),
            }
            metrics.append(entry)
            print(json.dumps(entry), flush=True)
            assert response.status_code < 400, f"{method} {route}: HTTP {response.status_code}"
            return response.json()["data"]

        account = call("GET", "me")
        assert account["runtime"] == {"environment": "staging", "database_kind": "hosted"}
        original = call("GET", "reports/summary")
        streak = call("GET", "gamification/overview")
        marker = "Performance QA " + uuid4().hex[:12]
        body = {
            "merchant": marker,
            "description": marker,
            "category_code": "food_drink",
            "occurred_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "payment_method": "qris",
            "amount_idr": "1000",
            "items": [{"label": "QA", "quantity": 1, "unit_price_idr": "1000"}],
        }
        row = None
        try:
            row = call("POST", "expenses", body)
            path = "expenses/" + row["id"]
            assert call("GET", path)["version"] == 1
            edited = {
                **body,
                "version": 1,
                "amount_idr": "2000",
                "items": [{"label": "QA", "quantity": 1, "unit_price_idr": "2000"}],
            }
            edit_key = str(uuid4())
            row = call("PATCH", path, edited, edit_key)
            assert row["version"] == 2 and row["amount_idr"] == "2000"
            assert call("PATCH", path, edited, edit_key) == row
            call("DELETE", path + "?version=2")
            row = call("POST", path + "/restore")
            assert row["version"] == 4
        finally:
            if row is not None:
                call("DELETE", "expenses/" + row["id"] + "?version=" + str(row["version"]))
        assert call("GET", "expenses?q=" + marker) == []
        after = call("GET", "reports/summary")
        assert (after["amount_idr"], after["count"]) == (original["amount_idr"], original["count"])
        after_streak = call("GET", "gamification/overview")
        assert after_streak == streak
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "environment": "staging",
                    "passed": True,
                    "test_record_deleted": True,
                    "summary_and_streak_unchanged": True,
                    "requests": metrics,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    print("PASS CRUD, replay, undo, cleanup, unchanged summary and streak")


if __name__ == "__main__":
    main()
