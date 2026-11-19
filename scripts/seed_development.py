"""Seed one dev/staging account; hosted accounts register through the Cashly form."""

import getpass
import json
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from cashly.core.auth import Identity
from cashly.core.environment import PROJECT_ROOT, ConfigurationError, resolve_environment
from cashly.core.settings import Settings
from cashly.db.commands import check_database, verify_development_target
from cashly.db.connection import Database, build_engine, database_url
from cashly.db.local_data import CATEGORIES, verify_local_target
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


class SeedError(Exception):
    """Safe, actionable operator message without credentials/provider details."""


def verify_seed_target(settings):
    if settings.environment not in {"dev", "staging"}:
        raise ConfigurationError("Seeder supports dev/staging only.")
    loopback = {"127.0.0.1", "localhost", "::1"}
    hosts = [urlsplit(settings.supabase_url or "").hostname]
    hosts += [
        database_url(secret).host
        for secret in (settings.database_url, settings.migration_database_url)
    ]
    if any(host in loopback for host in hosts):
        verify_local_target(settings)
    else:
        verify_development_target(settings)


def seed_reference(settings):
    """Add missing categories only. Never reset or modify existing categories."""
    engine = build_engine(settings, migration=True)
    try:
        with engine.begin() as conn:
            for order, (code, label) in enumerate(CATEGORIES, 1):
                conn.execute(
                    text(
                        "INSERT INTO cashly.expense_categories(code,label_id,sort_order) "
                        "VALUES (:code,:label,:order) ON CONFLICT (code) DO NOTHING"
                    ),
                    {"code": code, "label": label, "order": order},
                )
    finally:
        engine.dispose()


def seed_account(settings, credentials, *, allow_create=False):
    """Use normal password auth, preserving hosted signup/email verification."""
    with TestClient(create_app(settings)) as api:
        login = api.post("/api/v1/auth/login", json=credentials)
        if login.status_code == 401 and allow_create:
            registered = api.post(
                "/api/v1/auth/register", json={**credentials, "name": "Developer Cashly"}
            )
            if registered.status_code != 201:
                raise SeedError("Register through the Cashly form and verify email, then rerun.")
            login = api.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            if login.status_code in {401, 403}:
                raise SeedError(
                    "Login failed. Create the account through the Cashly form, verify email "
                    "and check the password, then rerun the seeder."
                )
            raise SeedError("Login unavailable. Check Auth configuration and retry later.")
        token = login.json()["data"]["session"]["access_token"]
        headers = {
            "apikey": settings.supabase_publishable_key.get_secret_value(),
            "Authorization": f"Bearer {token}",
        }
        try:
            with httpx2.Client(timeout=15, trust_env=False) as client:
                verified = client.get(settings.supabase_url + "/auth/v1/user", headers=headers)
                if verified.status_code != 200:
                    raise SeedError("Identity verification failed; no scenario was seeded.")
                user = verified.json()
            if user.get("email", "").casefold() != credentials["email"].casefold() or not user.get(
                "email_confirmed_at"
            ):
                raise SeedError("Verified identity/email mismatch; no scenario was seeded.")
            identity = Identity(
                UUID(user["id"]),
                user["email"],
                "Developer Cashly",
                datetime.fromisoformat(user["email_confirmed_at"].replace("Z", "+00:00")),
            )
            prepared = api.post("/api/v1/auth/bootstrap", headers=headers)
            if prepared.status_code != 200:
                raise SeedError(
                    "Account preparation failed. Check schema/runtime grants and retry."
                )
            seed_reference(settings)
            db = Database(settings)
            try:
                return seed_scenario(db, identity)
            finally:
                db.dispose()
        finally:
            # Retire only the script's login; existing browser sessions stay active.
            try:
                with httpx2.Client(timeout=15, trust_env=False) as client:
                    client.post(
                        settings.supabase_url + "/auth/v1/logout?scope=local", headers=headers
                    )
            except httpx2.HTTPError:
                pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "staging"], default="dev")
    parser.add_argument("--env-source", choices=["file", "system"], default="file")
    parser.add_argument("--email", help="Existing form account; password is prompted privately.")
    args = parser.parse_args()
    settings = Settings.from_selection(
        resolve_environment(override=args.env, source_override=args.env_source)
    )
    verify_seed_target(settings)
    if not check_database(settings)["schema_current"]:
        raise SeedError("Apply python -m cashly db-upgrade for the selected environment first.")
    print(f"[Cashly] seeder environment={settings.environment} source={args.env_source}")
    legacy_local = (
        not args.email
        and settings.environment == "dev"
        and (urlsplit(settings.supabase_url or "").hostname in {"127.0.0.1", "localhost", "::1"})
    )
    if legacy_local:
        if CREDENTIALS.exists():
            credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        else:
            credentials = {
                "email": os.environ.get("DEV_SEED_EMAIL", "developer@cashly.test"),
                "password": os.environ.get("DEV_SEED_PASSWORD")
                or secrets.token_urlsafe(8) + "Aa1!",
            }
            descriptor = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(credentials, file, indent=2)
    else:
        if not args.email:
            raise SeedError("Use --email for an existing verified form account on hosted Supabase.")
        credentials = {
            "email": args.email.strip(),
            "password": getpass.getpass("Cashly password: "),
        }
    result = seed_account(settings, credentials, allow_create=legacy_local)
    print(
        json.dumps(
            {
                "seeded": True,
                "environment": settings.environment,
                "already_seeded": result["already_seeded"],
                "hero_level": result["gamification"]["hero_level"],
                **({"credentials_file": CREDENTIALS.name} if legacy_local else {}),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (SeedError, ConfigurationError) as error:
        print(f"[Cashly] {error}")
        raise SystemExit(1) from None
    except Exception as error:
        print(
            f"[Cashly] Seeder failed ({type(error).__name__}); "
            "check selected services/configuration."
        )
        raise SystemExit(1) from None
