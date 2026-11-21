from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from cashly.db.models import AccountAuthLink, Expense


def lock_identity(session: Session, user_id: UUID) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": str(user_id)},
    )


def find_link(session: Session, user_id: UUID) -> AccountAuthLink | None:
    return session.scalar(select(AccountAuthLink).where(AccountAuthLink.auth_user_id == user_id))


def set_account_context(session: Session, account_id: UUID) -> None:
    session.execute(
        text("SELECT set_config('cashly.account_id', :account_id, true)"),
        {"account_id": str(account_id)},
    )


def account_expenses(session: Session, account_id: UUID) -> list[Expense]:
    return list(
        session.scalars(
            select(Expense)
            .where(Expense.account_id == account_id, Expense.deleted_at.is_(None))
            .order_by(Expense.occurred_at.desc(), Expense.id.desc())
            .limit(101)
        )
    )
