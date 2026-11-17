from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from cashly.core.auth import Identity
from cashly.core.settings import Settings
from cashly.db.connection import Database, database_url
from cashly.db.models import Account, AccountAuthLink, Profile
from cashly.repositories.accounts import (
    account_expenses,
    find_link,
    lock_identity,
    set_account_context,
)
from cashly.schemas.account import AccountData, ExpenseData, ExpensesEnvelope, RuntimeContext


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


def runtime_context(settings: Settings) -> RuntimeContext | None:
    if settings.database_url is None:
        return None
    local = database_url(settings.database_url).host in {"127.0.0.1", "localhost", "::1"}
    return RuntimeContext(
        environment=settings.environment,
        database_kind="local" if local else "hosted",
    )


def account_data(
    account: Account,
    profile: Profile,
    identity: Identity,
    sample: bool,
    settings: Settings | None = None,
) -> AccountData:
    return AccountData(
        account_id=account.id,
        display_name=profile.display_name,
        username=profile.username,
        password_login_enabled=profile.password_login_enabled,
        google_connected="google" in identity.providers,
        email=identity.email,
        timezone=profile.timezone,
        theme_preference=profile.theme_preference,
        joined_at=account.created_at,
        sample_data=sample,
        runtime=runtime_context(settings) if settings else None,
    )


def bootstrap(
    db: Database, settings: Settings, identity: Identity, password_enabled=False
) -> AccountData:
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
        if password_enabled:
            profile.password_login_enabled = True
            session.flush()
        result = account_data(account, profile, identity, False, settings)
    return result


def me(db: Database, settings: Settings, identity: Identity) -> AccountData:
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = load_account(session, identity)
        return account_data(account, profile, identity, False, settings)


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
