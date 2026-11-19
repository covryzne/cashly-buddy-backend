from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from cashly.db.commands import alembic_config, check_database, upgrade_database
from cashly.db.connection import Database, build_engine
from cashly.db.models import (
    Account,
    AccountAuthLink,
    Expense,
    ExpenseItem,
    IdempotencyRecord,
    Profile,
)


def test_real_postgres_migration_constraints_rls_and_rollback(postgres_settings):
    settings = postgres_settings
    upgrade_database(settings)
    upgrade_database(settings)  # Repeated upgrade must preserve nine reference rows.
    status = check_database(settings)
    assert status == {
        "connected": True,
        "tls": False,
        "privileged_role": True,
        "schema_current": True,
    }
    engine = build_engine(settings, migration=True)
    account_a, account_b, auth_a, auth_b = [uuid4() for _ in range(4)]
    try:
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT count(*) FROM cashly.expense_categories")) == 9
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname='cashly' AND rowsecurity")
                )
                == 13
            )
            assert conn.scalar(text("SELECT to_regclass('public.unrelated_sentinel') IS NOT NULL"))
            # Only this disposable cluster has test auth rows and a custom runtime group.
            conn.execute(
                text("INSERT INTO auth.users(id) VALUES (:a),(:b)"), {"a": auth_a, "b": auth_b}
            )
            conn.exec_driver_sql(Path("sql/runtime_role.sql").read_text(encoding="utf-8"))
        db = Database(settings)
        try:
            with db.transaction() as session:
                session.add_all([Account(id=account_a), Account(id=account_b)])
                session.flush()
                session.add(Profile(account_id=account_a, display_name="Tester A"))
                session.add_all(
                    [
                        AccountAuthLink(
                            auth_user_id=auth_a, account_id=account_a, verified_at=datetime.now(UTC)
                        ),
                        AccountAuthLink(
                            auth_user_id=auth_b, account_id=account_b, verified_at=datetime.now(UTC)
                        ),
                    ]
                )
            for auth_user, expected in ((auth_a, 1), (auth_b, 1), (None, 0)):
                with db.transaction(auth_user_id=auth_user) as session:
                    session.execute(text("SET LOCAL ROLE cashly_runtime"))
                    assert (
                        session.scalar(select(func.count()).select_from(AccountAuthLink))
                        == expected
                    )
            with pytest.raises(IntegrityError):
                with db.transaction() as session:
                    session.add(
                        AccountAuthLink(
                            auth_user_id=uuid4(),
                            account_id=account_a,
                            verified_at=datetime.now(UTC),
                        )
                    )

            def replay_record(account):
                return IdempotencyRecord(
                    account_id=account,
                    operation="create_expense",
                    key="fixture-key",
                    request_fingerprint="a" * 64,
                    response_status=201,
                    response_body={"data": {}},
                )

            with db.transaction() as session:
                session.add_all([replay_record(account_a), replay_record(account_b)])
            with pytest.raises(IntegrityError):
                with db.transaction() as session:
                    session.add(replay_record(account_a))
            expense_id = uuid4()

            def expense(**overrides):
                return Expense(
                    id=uuid4(),
                    account_id=account_a,
                    amount_idr=25000,
                    occurred_at=datetime.now(UTC),
                    category_code="food_drink",
                    description="Fictional receipt",
                    source_platform="dashboard",
                    source_type="receipt",
                    source_message_id=str(uuid4()),
                    timestamp_basis="user_explicit",
                    **overrides,
                )

            with db.transaction() as session:
                row = expense()
                row.id = expense_id
                row.source_message_id = "fixture-receipt-1"
                session.add(row)
                session.flush()
                session.add_all(
                    [
                        ExpenseItem(
                            expense_id=expense_id,
                            item_index=0,
                            label="Kopi",
                            amount_idr=15000,
                            unit_price_idr=15000,
                        ),
                        ExpenseItem(
                            expense_id=expense_id,
                            item_index=1,
                            label="Roti",
                            amount_idr=10000,
                            unit_price_idr=10000,
                        ),
                    ]
                )
            with db.transaction() as session:
                assert session.scalar(select(func.sum(Expense.amount_idr))) == 25000
                assert session.scalar(select(func.count()).select_from(Expense)) == 1
            with pytest.raises(IntegrityError):
                with db.transaction() as session:
                    duplicate = expense()
                    duplicate.source_message_id = "fixture-receipt-1"
                    session.add(duplicate)
            with pytest.raises(IntegrityError):
                with db.transaction() as session:
                    invalid = expense()
                    invalid.amount_idr = 0
                    session.add(invalid)
            with pytest.raises(IntegrityError):
                with db.transaction() as session:
                    session.add(
                        ExpenseItem(expense_id=expense_id, item_index=2, label="Bad", amount_idr=-1)
                    )
            # A failed item rolls back its freshly inserted parent too.
            rolled_back_id = uuid4()
            with pytest.raises(IntegrityError):
                with db.transaction() as session:
                    row = expense()
                    row.id = rolled_back_id
                    session.add(row)
                    session.flush()
                    session.add(
                        ExpenseItem(
                            expense_id=rolled_back_id, item_index=-1, label="Bad", amount_idr=1
                        )
                    )
            with db.transaction() as session:
                assert session.get(Expense, rolled_back_id) is None
            # RLS: same unfiltered query returns only context-owned rows under a non-owner role.
            for account, expected in ((account_a, 1), (account_b, 0), (None, 0)):
                with db.transaction(account_id=account) as session:
                    session.execute(text("SET LOCAL ROLE cashly_runtime"))
                    assert session.scalar(select(func.count()).select_from(Expense)) == expected
                    assert (
                        session.scalar(select(func.count()).select_from(ExpenseItem))
                        == expected * 2
                    )
            with pytest.raises(DBAPIError):
                with db.transaction(account_id=account_b) as session:
                    session.execute(text("SET LOCAL ROLE cashly_runtime"))
                    session.add(expense())
            # Owner reassignment must fail WITH CHECK; foreign-owner child insertion too.
            with pytest.raises(DBAPIError):
                with db.transaction(account_id=account_a) as session:
                    session.execute(text("SET LOCAL ROLE cashly_runtime"))
                    session.execute(
                        text("UPDATE cashly.expenses SET account_id=:b WHERE id=:id"),
                        {"b": account_b, "id": expense_id},
                    )
            with pytest.raises(DBAPIError):
                with db.transaction(account_id=account_b) as session:
                    session.execute(text("SET LOCAL ROLE cashly_runtime"))
                    session.add(
                        ExpenseItem(
                            expense_id=expense_id, item_index=5, label="Other", amount_idr=1
                        )
                    )
            for role in ("anon", "authenticated"):
                with pytest.raises(DBAPIError):
                    with db.transaction(account_id=account_a) as session:
                        session.execute(text(f"SET LOCAL ROLE {role}"))
                        session.execute(text("SELECT * FROM cashly.expenses"))
            with db.transaction() as session:
                before = session.get(Expense, expense_id).updated_at
            with db.transaction() as session:
                row = session.get(Expense, expense_id)
                row.description = "Changed fixture"
            with db.transaction() as session:
                assert session.get(Expense, expense_id).updated_at > before
                row = session.get(Expense, expense_id)
                row.deleted_at = datetime.now(UTC)
            with db.transaction(account_id=account_a) as session:
                session.execute(text("SET LOCAL ROLE cashly_runtime"))
                assert session.scalar(select(func.count()).select_from(ExpenseItem)) == 0
            # Real reflection/autogenerate must not propose changing auth/public tables.
            command.check(alembic_config(settings))
        finally:
            db.dispose()
        command.downgrade(alembic_config(settings), "base")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('cashly.expenses') IS NULL"))
            assert conn.scalar(text("SELECT to_regclass('auth.users') IS NOT NULL"))
            assert conn.scalar(text("SELECT to_regclass('public.unrelated_sentinel') IS NOT NULL"))
        upgrade_database(settings)
        assert check_database(settings)["schema_current"]
    finally:
        engine.dispose()
