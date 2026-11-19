"""Atomic reviewed-batch persistence against an isolated PostgreSQL cluster."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from cashly.core.auth import Identity
from cashly.core.errors import ApiError
from cashly.db.commands import upgrade_database
from cashly.db.connection import Database
from cashly.db.models import Expense, ExpenseItem, GameEvent
from cashly.schemas.chat import ChatBatchInput
from cashly.services.accounts import bootstrap
from cashly.services.chat_recording import record_batch


def record(category="food_drink"):
    return {
        "id": str(uuid4()),
        "expense": {
            "merchant": "",
            "description": "Kopi",
            "category_code": category,
            "occurred_at": "2026-11-17T12:00:00+07:00",
            "payment_method": "unknown",
            "amount_idr": "30000",
            "discount_idr": "0",
            "tax_idr": "0",
            "items": [
                {"label": "Kopi", "quantity": 2, "unit_price_idr": "15000"},
            ],
        },
    }


def test_atomic_batch_replay_conflict_rollback_and_account_isolation(postgres_settings):
    upgrade_database(postgres_settings)
    db = Database(postgres_settings)
    identity = Identity(uuid4(), "test@example.invalid", "Chat Tester", datetime.now(UTC))
    second = Identity(uuid4(), "second@example.invalid", "Second Tester", datetime.now(UTC))
    try:
        with db.transaction() as session:
            session.execute(
                text("INSERT INTO auth.users(id) VALUES (:a),(:b)"),
                {
                    "a": identity.user_id,
                    "b": second.user_id,
                },
            )
        account = bootstrap(db, postgres_settings, identity)
        other = bootstrap(db, postgres_settings, second)
        body = ChatBatchInput.model_validate({"records": [record(), record("transport")]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: record_batch(db, identity, body, "chat-test-key"), [1, 2])
            )
        assert results[0] == results[1]
        assert len(results[0]["data"]) == 2
        assert all(row["source"] == "text" for row in results[0]["data"])
        assert all(row["items"][0]["unit_price_idr"] == "15000" for row in results[0]["data"])
        with db.transaction(account_id=account.account_id) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Expense)
                    .where(
                        Expense.account_id == account.account_id,
                    )
                )
                == 2
            )
        changed = body.model_copy(deep=True)
        changed.records[0].expense.merchant = "Changed"
        with pytest.raises(ApiError) as error:
            record_batch(db, identity, changed, "chat-test-key")
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
        with pytest.raises(ApiError) as error:
            record_batch(db, identity, body, "different-key")
        assert error.value.code == "CHAT_ALREADY_SAVED"
        # Same source IDs/key in a different account cannot read/replay the first account's data.
        other_result = record_batch(db, second, body, "chat-test-key")
        assert {r["id"] for r in results[0]["data"]}.isdisjoint(
            r["id"] for r in other_result["data"]
        )
        bad = ChatBatchInput.model_validate({"records": [record(), record("nonexistent")]})
        with db.transaction(account_id=other.account_id) as session:
            before_events = session.scalar(
                select(func.count())
                .select_from(GameEvent)
                .where(
                    GameEvent.account_id == other.account_id,
                )
            )
            before_items = session.scalar(
                select(func.count())
                .select_from(ExpenseItem)
                .join(
                    Expense,
                    Expense.id == ExpenseItem.expense_id,
                )
                .where(Expense.account_id == other.account_id)
            )
        with pytest.raises(ApiError):
            record_batch(db, second, bad, "rollback-key")
        with db.transaction(account_id=other.account_id) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Expense)
                    .where(
                        Expense.account_id == other.account_id,
                    )
                )
                == 2
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(GameEvent)
                    .where(
                        GameEvent.account_id == other.account_id,
                    )
                )
                == before_events
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExpenseItem)
                    .join(
                        Expense,
                        Expense.id == ExpenseItem.expense_id,
                    )
                    .where(Expense.account_id == other.account_id)
                )
                == before_items
            )
    finally:
        db.dispose()
