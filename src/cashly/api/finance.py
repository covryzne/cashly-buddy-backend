import hashlib
import json
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import select

from cashly.api.accounts import VerifiedIdentity, database
from cashly.db.models import ExpenseCategory, IdempotencyRecord
from cashly.schemas.chat import ChatBatchEnvelope, ChatBatchInput
from cashly.schemas.finance import (
    DeleteEnvelope,
    ExpenseEnvelope,
    ExpenseInput,
    ExpenseList,
    FreezeInput,
    GameEnvelope,
    PreferencesInput,
)
from cashly.services import finance, gamification
from cashly.services.accounts import account_data, load_account
from cashly.services.chat_recording import record_batch

router = APIRouter(tags=["Finance"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


@router.post("/expenses/chat-batch", status_code=201, response_model=ChatBatchEnvelope)
def create_chat_batch(request: Request, identity: VerifiedIdentity, body: ChatBatchInput, key: Key):
    return record_batch(database(request), identity, body, key)


@router.get("/expenses", response_model=ExpenseList)
def expenses(
    request: Request,
    response: Response,
    identity: VerifiedIdentity,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    category: str | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    start: datetime | None = None,
    end: datetime | None = None,
):
    response.headers["Cache-Control"] = "private, no-store"
    return finance.list_expenses(
        database(request), identity, limit, cursor, category, q, start, end
    )


@router.get("/expenses/{identifier}", response_model=ExpenseEnvelope)
def detail(request: Request, identity: VerifiedIdentity, identifier: UUID):
    return finance.detail(database(request), identity, identifier)


@router.post("/expenses", status_code=201, response_model=ExpenseEnvelope)
def create(request: Request, identity: VerifiedIdentity, body: ExpenseInput, key: Key):
    return finance.mutate(database(request), identity, "create", key, body)


@router.patch("/expenses/{identifier}", response_model=ExpenseEnvelope)
def update(
    request: Request, identity: VerifiedIdentity, identifier: UUID, body: ExpenseInput, key: Key
):
    return finance.mutate(database(request), identity, "update", key, body, identifier)


@router.delete("/expenses/{identifier}", response_model=DeleteEnvelope)
def remove(
    request: Request,
    identity: VerifiedIdentity,
    identifier: UUID,
    key: Key,
    version: Annotated[int | None, Query(ge=1)] = None,
):
    return finance.mutate(
        database(request), identity, "delete", key, identifier=identifier, expected_version=version
    )


@router.post("/expenses/{identifier}/restore", response_model=ExpenseEnvelope)
def restore(request: Request, identity: VerifiedIdentity, identifier: UUID, key: Key):
    return finance.mutate(database(request), identity, "restore", key, identifier=identifier)


@router.get("/categories")
def categories(request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        load_account(session, identity)
        return {
            "data": [
                {"code": c.code, "label": c.label_id}
                for c in session.scalars(
                    select(ExpenseCategory)
                    .where(ExpenseCategory.is_active)
                    .order_by(ExpenseCategory.sort_order)
                )
            ]
        }


@router.patch("/me/preferences")
def preferences(request: Request, identity: VerifiedIdentity, body: PreferencesInput):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, profile = finance.locked_account(session, identity)
        profile.display_name = body.display_name.strip()
        profile.theme_preference = body.theme_preference
        session.flush()
        return {"data": account_data(account, profile, identity, False, request.app.state.settings)}


@router.get("/gamification/overview", response_model=GameEnvelope)
@router.get("/gamification/streak/calendar", response_model=GameEnvelope)
def game(request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, profile = load_account(session, identity)
        return {
            "data": gamification.overview(
                session, account.id, profile.timezone, settings=request.app.state.settings
            )
        }


@router.get("/reports/summary")
@router.get("/reports/trend")
def report(
    request: Request,
    identity: VerifiedIdentity,
    start: datetime | None = None,
    end: datetime | None = None,
):
    return finance.report(database(request), identity, start, end)


def game_action(request, identity, kind, key, day=None):
    fingerprint = hashlib.sha256(json.dumps({"day": str(day)}).encode()).hexdigest()
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, profile = finance.locked_account(session, identity)
        previous = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.account_id == account.id,
                IdempotencyRecord.operation == f"game:{kind}",
                IdempotencyRecord.key == key,
            )
        )
        if previous:
            if previous.request_fingerprint != fingerprint:
                from cashly.core.errors import ApiError

                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "Kunci digunakan untuk permintaan berbeda."
                )
            # A cached pre-0004 response has XP fields. Preserve the original operation
            # without replaying its side effects, but serve the current game contract.
            if "hero_level" not in previous.response_body.get("data", {}):
                previous.response_body = {
                    "data": gamification.overview(
                        session, account.id, profile.timezone, settings=request.app.state.settings
                    )
                }
            return previous.response_body
        result = {
            "data": gamification.action(
                session, account.id, profile.timezone, kind, day, request.app.state.settings
            )
        }
        session.add(
            IdempotencyRecord(
                account_id=account.id,
                operation=f"game:{kind}",
                key=key,
                request_fingerprint=fingerprint,
                response_status=200,
                response_body=result,
            )
        )
        return result


@router.post("/gamification/checkins/no-spend", response_model=GameEnvelope)
def no_spend(request: Request, identity: VerifiedIdentity, key: Key):
    return game_action(request, identity, "no-spend", key)


@router.post("/gamification/checkins/review", response_model=GameEnvelope)
def daily_review(request: Request, identity: VerifiedIdentity, key: Key):
    return game_action(request, identity, "review", key)


@router.post("/gamification/streak/freeze", response_model=GameEnvelope)
def freeze(request: Request, identity: VerifiedIdentity, body: FreezeInput, key: Key):
    return game_action(request, identity, "freeze", key, body.protected_local_dates)
