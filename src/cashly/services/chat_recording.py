"""Commit a reviewed chat batch atomically; retries replay the original result."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from cashly.core.errors import ApiError
from cashly.db.models import Expense, ExpenseCategory, ExpenseItem, IdempotencyRecord
from cashly.services import finance, gamification


def record_batch(db, identity, body, key):
    # Keep fingerprints of existing chat-only requests unchanged after adding source.
    legacy_chat = all(record.source == "chat" for record in body.records)
    encoded = body.model_dump_json(
        exclude={"records": {"__all__": {"source"}}} if legacy_chat else None
    )
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = finance.locked_account(session, identity)
        previous = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.account_id == account.id,
                IdempotencyRecord.operation == "chat_batch",
                IdempotencyRecord.key == key,
            )
        )
        if previous:
            if previous.request_fingerprint != fingerprint:
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "Batch ini sudah disimpan dengan isi lain."
                )
            return previous.response_body
        rows = []
        for record in body.records:
            payload = record.expense
            category = session.get(ExpenseCategory, payload.category_code)
            if not category or not category.is_active:
                raise ApiError(409, "CATEGORY_INACTIVE", "Kategori tidak tersedia.")
            if payload.occurred_at > datetime.now(UTC):
                raise ApiError(
                    422, "FUTURE_EXPENSE", "Tanggal pengeluaran tidak boleh di masa depan."
                )
            source_id = f"{record.source}:{record.id}"
            existing = session.scalar(
                select(Expense.id).where(
                    Expense.account_id == account.id,
                    Expense.source_platform == "dashboard",
                    Expense.source_message_id == source_id,
                )
            )
            if existing:
                raise ApiError(
                    409, "CHAT_ALREADY_SAVED", "Catatan ini sudah disimpan. Muat ulang data."
                )
            values = payload.model_dump(exclude={"items", "version"})
            for field in ("amount_idr", "discount_idr", "tax_idr"):
                values[field] = int(values[field])
            row = Expense(
                id=uuid4(),
                account_id=account.id,
                source_platform="dashboard",
                source_type={"receipt": "receipt", "voice": "voice", "chat": "text"}[record.source],
                source_message_id=source_id,
                source_item_index=0,
                timestamp_basis="user_explicit",
                version=1,
                **values,
            )
            session.add(row)
            session.flush()
            for index, item in enumerate(payload.items):
                session.add(
                    ExpenseItem(
                        expense_id=row.id,
                        item_index=index,
                        label=item.label,
                        quantity=item.quantity,
                        unit_price_idr=int(item.unit_price_idr),
                        amount_idr=item.quantity * int(item.unit_price_idr),
                    )
                )
            gamification.on_expense(session, account.id, profile.timezone, row.occurred_at)
            rows.append(row)
        session.flush()
        items = finance.item_map(session, rows)
        result = {"data": [finance.serialize(row, items[row.id]) for row in rows]}
        session.add(
            IdempotencyRecord(
                account_id=account.id,
                operation="chat_batch",
                key=key,
                request_fingerprint=fingerprint,
                response_status=201,
                response_body=result,
            )
        )
        return result
