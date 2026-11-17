"""Create one local development login and populate its expense/game scenario."""

import json
import os
import secrets
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import select

from cashly.core.auth import Identity
from cashly.core.environment import PROJECT_ROOT, resolve_environment
from cashly.core.settings import Settings
from cashly.db.commands import check_database
from cashly.db.connection import Database
from cashly.db.local_data import local_data, verify_local_target
from cashly.db.models import IdempotencyRecord
from cashly.main import create_app
from cashly.schemas.finance import ExpenseInput
from cashly.services import finance, gamification

CREDENTIALS = PROJECT_ROOT / ".cashly-dev-account.json"


def seed_scenario(db, identity):
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = finance.locked_account(session, identity)
        marker = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.account_id == account.id,
                IdempotencyRecord.operation == "seed-development",
                IdempotencyRecord.key == "scenario-v1",
            )
        )
        if marker and marker.response_body.get("completed"):
            return {
                "already_seeded": True,
                "gamification": gamification.overview(session, account.id, profile.timezone),
            }
        if marker:
            anchor = datetime.fromisoformat(marker.response_body["anchor"])
        else:
            anchor = datetime.now(ZoneInfo("Asia/Jakarta")).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            marker = IdempotencyRecord(
                account_id=account.id,
                operation="seed-development",
                key="scenario-v1",
                request_fingerprint="0" * 64,
                response_status=200,
                response_body={"anchor": anchor.isoformat(), "completed": False},
            )
            session.add(marker)
    categories = ["food_drink", "transport", "bills", "entertainment"]
    for index in range(16):
        price = 10000 + index * 2500
        count = 1 + index % 3
        discount = 1000 if index % 2 else 0
        tax = 500 if index % 3 == 0 else 0
        body = ExpenseInput(
            merchant=f"Toko Contoh {index + 1}",
            description=f"Transaksi development {index + 1}",
            category_code=categories[index % len(categories)],
            occurred_at=anchor - timedelta(days=index),
            payment_method="qris",
            amount_idr=str(count * price - discount + tax),
            discount_idr=str(discount),
            tax_idr=str(tax),
            items=[
                {
                    "label": f"Item contoh {index + 1}",
                    "quantity": count,
                    "unit_price_idr": str(price),
                }
            ],
        )
        finance.mutate(db, identity, "create", f"seed-development-v1-{index:02}", body)
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = finance.locked_account(session, identity)
        for offset in reversed(range(12)):
            gamification.checkin(
                session, account.id, profile.timezone, anchor.date() - timedelta(days=offset)
            )
        marker = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.account_id == account.id,
                IdempotencyRecord.operation == "seed-development",
                IdempotencyRecord.key == "scenario-v1",
            )
        )
        marker.response_body = {"anchor": anchor.isoformat(), "completed": True}
        session.flush()
        return {
            "already_seeded": False,
            "gamification": gamification.overview(session, account.id, profile.timezone),
        }


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-source", choices=["file", "system"], default="file")
    args = parser.parse_args()
    settings = Settings.from_selection(
        resolve_environment(override="dev", source_override=args.env_source)
    )
    verify_local_target(settings)
    if not check_database(settings)["schema_current"]:
        raise RuntimeError("Apply python -m cashly db-upgrade first")
    local_data(settings, "seed-reference")
    if CREDENTIALS.exists():
        credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    else:
        credentials = {
            "email": os.environ.get("DEV_SEED_EMAIL", "developer@cashly.test"),
            "password": os.environ.get("DEV_SEED_PASSWORD") or secrets.token_urlsafe(8) + "Aa1!",
        }
        # Save before provisioning so a failed operation can resume with the same login.
        descriptor = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(credentials, file, indent=2)
    with TestClient(create_app(settings)) as api:
        login = api.post("/api/v1/auth/login", json=credentials)
        if login.status_code == 401:
            registered = api.post(
                "/api/v1/auth/register", json={**credentials, "name": "Developer Cashly"}
            )
            if registered.status_code != 201:
                raise RuntimeError(
                    "Cannot provision local account; check existing login/configuration"
                )
            login = api.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            raise RuntimeError("Local development login failed")
        token = login.json()["data"]["session"]["access_token"]
        with httpx2.Client(timeout=15, trust_env=False) as client:
            verified = client.get(
                settings.supabase_url + "/auth/v1/user",
                headers={
                    "apikey": settings.supabase_publishable_key.get_secret_value(),
                    "Authorization": f"Bearer {token}",
                },
            )
            if verified.status_code != 200:
                raise RuntimeError("Local identity verification failed")
            user = verified.json()
        identity = Identity(
            UUID(user["id"]),
            user["email"],
            "Developer Cashly",
            datetime.fromisoformat(user["email_confirmed_at"].replace("Z", "+00:00")),
        )
        db = Database(settings)
        try:
            result = seed_scenario(db, identity)
        finally:
            db.dispose()
        api.post("/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {token}"})
        # Close the CLI login session; frontend will establish its own normal session.
        with httpx2.Client(timeout=15, trust_env=False) as client:
            client.post(
                settings.supabase_url + "/auth/v1/logout?scope=local",
                headers={
                    "apikey": settings.supabase_publishable_key.get_secret_value(),
                    "Authorization": f"Bearer {token}",
                },
            )
    print(
        json.dumps(
            {
                "seeded": True,
                "already_seeded": result["already_seeded"],
                "credentials_file": CREDENTIALS.name,
                "level": result["gamification"]["level"],
                "xp": result["gamification"]["total_xp"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never expose DB/HTTP exceptions or credential bodies on the terminal.
        print(
            f"[Cashly] Seeder failed ({type(error).__name__}); check local services/configuration."
        )
        raise SystemExit(1) from None
