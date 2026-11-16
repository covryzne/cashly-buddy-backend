from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from cashly.core.auth import Identity
from cashly.core.settings import Settings
from cashly.db.connection import Database
from cashly.db.models import Account, AccountAuthLink, Expense, IdempotencyRecord, Profile
from cashly.repositories.accounts import (
    account_expenses,
    find_link,
    lock_identity,
    set_account_context,
)
from cashly.schemas.account import AccountData, ExpenseData, ExpensesEnvelope


def sample_mode(settings: Settings) -> bool:
    return settings.environment == "dev" and settings.dev_sample_data


def seed_samples(session: Session, account: Account) -> None:
    """Once per account, under identity lock. Never reseed deleted sample rows."""
    marker = session.scalar(
        select(IdempotencyRecord.id).where(
            IdempotencyRecord.account_id == account.id,
            IdempotencyRecord.operation == "dev_samples",
            IdempotencyRecord.key == "sample-v1",
        )
    )
    if marker:
        return
    now = datetime.now(UTC).replace(hour=5, minute=0, second=0, microsecond=0)
    samples = [
        ("Kopi Contoh", "Kopi dan roti contoh", "food_drink", 28000, "qris"),
        ("Transport Contoh", "Perjalanan contoh", "transport", 18000, "e_wallet"),
        ("Makan Contoh", "Makan siang contoh", "food_drink", 35000, "cash"),
        ("Listrik Contoh", "Tagihan contoh", "bills", 150000, "bank_transfer"),
        ("Hiburan Contoh", "Tiket contoh", "entertainment", 50000, "qris"),
        ("Belanja Contoh", "Belanja contoh", "food_drink", 65000, "debit_card"),
    ]
    for index, (merchant, description, category, amount, payment) in enumerate(samples):
        session.add(
            Expense(
                account_id=account.id,
                merchant=merchant,
                description=description,
                category_code=category,
                amount_idr=amount,
                payment_method=payment,
                occurred_at=now - timedelta(days=index),
                source_platform="dashboard",
                source_type="dashboard",
                source_message_id=f"dev-sample-v1-{index}",
                source_item_index=0,
                timestamp_basis="user_explicit",
            )
        )
    session.add(
        IdempotencyRecord(
            account_id=account.id,
            operation="dev_samples",
            key="sample-v1",
            request_fingerprint=sha256(b"cashly-dev-sample-v1").hexdigest(),
            response_status=200,
            response_body={"sample_version": 1},
        )
    )


def load_account(session: Session, identity: Identity) -> tuple[Account, Profile]:
    link = find_link(session, identity.user_id)
    if link is None:
        raise HTTPException(409)
    set_account_context(session, link.account_id)
    account = session.get(Account, link.account_id)
    if account is None or account.status != "active":
        raise HTTPException(403)
    profile = session.get(Profile, account.id)
    if profile is None:
        raise HTTPException(409)
    return account, profile


def account_data(
    account: Account, profile: Profile, identity: Identity, sample: bool
) -> AccountData:
    return AccountData(
        account_id=account.id,
        display_name=profile.display_name,
        email=identity.email,
        timezone=profile.timezone,
        theme_preference=profile.theme_preference,
        joined_at=account.created_at,
        sample_data=sample,
    )


def bootstrap(db: Database, settings: Settings, identity: Identity) -> AccountData:
    with db.transaction(auth_user_id=identity.user_id) as session:
        lock_identity(session, identity.user_id)
        link = find_link(session, identity.user_id)
        if link is None:
            account_id = uuid4()
            set_account_context(session, account_id)
            session.add(Account(id=account_id))
            session.flush()
            session.add(Profile(account_id=account_id, display_name=identity.display_name))
            session.add(
                AccountAuthLink(
                    auth_user_id=identity.user_id,
                    account_id=account_id,
                    verified_at=identity.verified_at,
                )
            )
            session.flush()
        account, profile = load_account(session, identity)
        if sample_mode(settings):
            seed_samples(session, account)
        result = account_data(account, profile, identity, sample_mode(settings))
    return result


def me(db: Database, settings: Settings, identity: Identity) -> AccountData:
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = load_account(session, identity)
        return account_data(account, profile, identity, sample_mode(settings))


def expenses(db: Database, identity: Identity) -> ExpensesEnvelope:
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        rows = account_expenses(session, account.id)
        return ExpensesEnvelope(
            data=[
                ExpenseData(
                    id=row.id,
                    merchant=row.merchant or row.description,
                    description=row.description,
                    category_code=row.category_code,
                    amount_idr=str(row.amount_idr),
                    occurred_at=row.occurred_at,
                    payment_method=row.payment_method,
                    source=row.source_type,
                )
                for row in rows[:100]
            ],
            has_more=len(rows) > 100,
        )
