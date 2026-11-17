import base64
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select, tuple_

from cashly.core.errors import ApiError
from cashly.db.models import Account, Expense, ExpenseCategory, ExpenseItem, IdempotencyRecord
from cashly.services import gamification
from cashly.services.accounts import load_account


def locked_account(session, identity):
    account, profile = load_account(session, identity)
    session.scalar(select(Account).where(Account.id == account.id).with_for_update())
    return account, profile


def serialize(row, items):
    return {
        "id": str(row.id),
        "merchant": row.merchant or "",
        "description": row.description,
        "category_code": row.category_code,
        "amount_idr": str(row.amount_idr),
        "discount_idr": str(row.discount_idr),
        "tax_idr": str(row.tax_idr),
        "occurred_at": row.occurred_at.isoformat(),
        "payment_method": row.payment_method,
        "source": row.source_type,
        "version": row.version,
        "items": [
            {
                "item_index": i.item_index,
                "label": i.label,
                "quantity": i.quantity,
                "unit_price_idr": str(i.unit_price_idr),
                "amount_idr": str(i.amount_idr),
            }
            for i in sorted(items, key=lambda i: i.item_index)
        ],
    }


def item_map(session, rows):
    result = defaultdict(list)
    if rows:
        for item in session.scalars(
            select(ExpenseItem).where(ExpenseItem.expense_id.in_([row.id for row in rows]))
        ):
            result[item.expense_id].append(item)
    return result


def list_expenses(
    db, identity, limit=100, cursor=None, category=None, query=None, start=None, end=None
):
    if any(value is not None and value.tzinfo is None for value in (start, end)):
        raise ApiError(422, "INVALID_PERIOD", "Zona waktu diperlukan.")
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        stmt = select(Expense).where(Expense.account_id == account.id, Expense.deleted_at.is_(None))
        if category:
            stmt = stmt.where(Expense.category_code == category)
        if query:
            term = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(
                or_(
                    Expense.merchant.ilike(f"%{term}%", escape="\\"),
                    Expense.description.ilike(f"%{term}%", escape="\\"),
                )
            )
        if start:
            stmt = stmt.where(Expense.occurred_at >= start)
        if end:
            stmt = stmt.where(Expense.occurred_at < end)
        if cursor:
            try:
                timestamp, identifier = json.loads(base64.urlsafe_b64decode(cursor))
                stamp = datetime.fromisoformat(timestamp)
                if stamp.tzinfo is None:
                    raise ValueError
                identifier = UUID(identifier)
            except (ValueError, TypeError, json.JSONDecodeError):
                raise ApiError(422, "INVALID_CURSOR", "Cursor tidak valid.") from None
            stmt = stmt.where(tuple_(Expense.occurred_at, Expense.id) < tuple_(stamp, identifier))
        rows = list(
            session.scalars(
                stmt.order_by(Expense.occurred_at.desc(), Expense.id.desc()).limit(limit + 1)
            )
        )
        more = len(rows) > limit
        rows = rows[:limit]
        grouped = item_map(session, rows)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([rows[-1].occurred_at.isoformat(), str(rows[-1].id)]).encode()
            ).decode()
            if more
            else None
        )
        return {
            "data": [serialize(row, grouped[row.id]) for row in rows],
            "has_more": more,
            "next_cursor": next_cursor,
        }


def find_expense(session, account_id, identifier, include_deleted=False):
    stmt = select(Expense).where(Expense.id == identifier, Expense.account_id == account_id)
    if not include_deleted:
        stmt = stmt.where(Expense.deleted_at.is_(None))
    row = session.scalar(stmt)
    if not row:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Pengeluaran tidak ditemukan.")
    return row


def detail(db, identity, identifier):
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        row = find_expense(session, account.id, identifier)
        return {"data": serialize(row, item_map(session, [row])[row.id])}


def report(db, identity, start=None, end=None):
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = load_account(session, identity)
        now = datetime.now(ZoneInfo(profile.timezone))
        start = start or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = end or now + timedelta(seconds=1)
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ApiError(422, "INVALID_PERIOD", "Periode dan zona waktu tidak valid.")
        conditions = (
            Expense.account_id == account.id,
            Expense.deleted_at.is_(None),
            Expense.occurred_at >= start,
            Expense.occurred_at < end,
        )
        amount, count = session.execute(
            select(func.coalesce(func.sum(Expense.amount_idr), 0), func.count(Expense.id)).where(
                *conditions
            )
        ).one()
        categories = session.execute(
            select(Expense.category_code, func.sum(Expense.amount_idr), func.count(Expense.id))
            .where(*conditions)
            .group_by(Expense.category_code)
        ).all()
        local_day = func.date(func.timezone(profile.timezone, Expense.occurred_at))
        trend = session.execute(
            select(local_day, func.sum(Expense.amount_idr), func.count(Expense.id))
            .where(*conditions)
            .group_by(local_day)
            .order_by(local_day)
        ).all()
        return {
            "data": {
                "amount_idr": str(amount),
                "count": count,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": profile.timezone,
                "categories": [
                    {"code": code, "amount_idr": str(total), "count": n}
                    for code, total, n in categories
                ],
                "trend": [
                    {"date": str(day), "amount_idr": str(total), "count": n}
                    for day, total, n in trend
                ],
            }
        }


def mutate(db, identity, operation, key, payload=None, identifier=None, expected_version=None):
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "body": payload.model_dump(mode="json") if payload else None,
                "version": expected_version,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    scope = f"{operation}:{identifier}" if identifier else operation
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = locked_account(session, identity)
        previous = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.account_id == account.id,
                IdempotencyRecord.operation == scope,
                IdempotencyRecord.key == key,
            )
        )
        if previous:
            if previous.request_fingerprint != fingerprint:
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "Kunci digunakan untuk permintaan berbeda."
                )
            return previous.response_body
        row = None
        if operation != "create":
            row = find_expense(
                session, account.id, identifier, include_deleted=operation == "restore"
            )
            version = payload.version if payload else expected_version
            if version is not None and version != row.version:
                raise ApiError(
                    409, "VERSION_CONFLICT", "Data berubah. Muat ulang sebelum melanjutkan."
                )
        if operation in {"create", "update"}:
            category = session.get(ExpenseCategory, payload.category_code)
            if not category or (
                not category.is_active
                and (row is None or row.category_code != payload.category_code)
            ):
                raise ApiError(409, "CATEGORY_INACTIVE", "Kategori tidak tersedia.")
            if payload.occurred_at > datetime.now(UTC):
                raise ApiError(
                    422, "FUTURE_EXPENSE", "Tanggal pengeluaran tidak boleh di masa depan."
                )
            if row is None:
                row = Expense(
                    id=uuid4(),
                    account_id=account.id,
                    source_platform="dashboard",
                    source_type="dashboard",
                    source_message_id=str(uuid4()),
                    source_item_index=0,
                    timestamp_basis="user_explicit",
                    version=1,
                )
                session.add(row)
            else:
                row.version += 1
                session.execute(delete(ExpenseItem).where(ExpenseItem.expense_id == row.id))
            for field in (
                "merchant",
                "description",
                "category_code",
                "occurred_at",
                "payment_method",
            ):
                setattr(row, field, getattr(payload, field))
            for field in ("amount_idr", "discount_idr", "tax_idr"):
                setattr(row, field, int(getattr(payload, field)))
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
            if operation == "create":
                gamification.on_expense(session, account.id, profile.timezone, row.occurred_at)
        elif operation == "delete":
            row.deleted_at = datetime.now(UTC)
            row.version += 1
        elif operation == "restore":
            if row.deleted_at and datetime.now(UTC) > row.deleted_at + timedelta(seconds=30):
                raise ApiError(409, "RESTORE_EXPIRED", "Waktu Undo sudah habis.")
            row.deleted_at = None
            row.version += 1
        session.flush()
        if operation == "delete":
            result = {
                "data": {
                    "id": str(row.id),
                    "version": row.version,
                    "deleted_at": row.deleted_at.isoformat(),
                    "restore_until": (row.deleted_at + timedelta(seconds=30)).isoformat(),
                }
            }
        else:
            result = {"data": serialize(row, item_map(session, [row])[row.id])}
        session.add(
            IdempotencyRecord(
                account_id=account.id,
                operation=scope,
                key=key,
                request_fingerprint=fingerprint,
                response_status=201 if operation == "create" else 200,
                response_body=result,
            )
        )
        return result
