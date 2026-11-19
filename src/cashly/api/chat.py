from time import monotonic

from fastapi import APIRouter, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.db.models import ExpenseCategory
from cashly.schemas.chat import ChatExtractEnvelope, ChatExtractInput
from cashly.services.accounts import load_account
from cashly.services.chat_extraction import extract_chat

router = APIRouter(tags=["Chat recording"])


def extraction_context(db, identity):
    with db.transaction(auth_user_id=identity.user_id) as session:
        _, profile = load_account(session, identity)
        categories = {
            c.code: c.label_id
            for c in session.scalars(select(ExpenseCategory).where(ExpenseCategory.is_active))
        }
        return categories, profile.timezone


@router.post("/chat/extract", response_model=ChatExtractEnvelope)
async def extract(request: Request, identity: VerifiedIdentity, body: ChatExtractInput):
    """Extract drafts only; never writes expenses or awards a streak."""
    categories, timezone = await run_in_threadpool(extraction_context, database(request), identity)
    # Per-process protection for the initial single-instance deployment. No raw content stored.
    limits = request.app.state.chat_limits
    now = monotonic()
    for user, (times, busy) in list(limits.items()):
        if not busy and (not times or now - times[-1] >= 60):
            del limits[user]
    times, busy = limits.get(identity.user_id, ([], False))
    times = [t for t in times if now - t < 60]
    if busy or len(times) >= 10 or len(limits) >= 10000:
        raise ApiError(429, "CHAT_RATE_LIMITED", "Tunggu sebentar sebelum mengirim lagi, ya.")
    limits[identity.user_id] = (times + [now], True)
    try:
        data = await extract_chat(
            request.app.state.settings,
            request.app.state.ai_http,
            body,
            categories,
            timezone,
            request.state.request_id,
        )
        return ChatExtractEnvelope(data=data)
    finally:
        limits[identity.user_id] = (times + [now], False)
