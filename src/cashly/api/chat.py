from contextlib import contextmanager
from time import monotonic

from fastapi import APIRouter, Request
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.db.models import ExpenseCategory
from cashly.schemas.chat import ChatExtractEnvelope, ChatExtractInput
from cashly.schemas.receipt import MAX_REQUEST_BYTES, ReceiptExtractInput
from cashly.services.accounts import load_account
from cashly.services.chat_extraction import extract_chat
from cashly.services.receipt_extraction import (
    extract_receipt,
    validate_image,
)

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
    with extraction_slot(request, identity):
        data = await extract_chat(
            request.app.state.settings,
            request.app.state.ai_http,
            body,
            categories,
            timezone,
            request.state.request_id,
        )
        return ChatExtractEnvelope(data=data)


@contextmanager
def extraction_slot(request, identity):
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
        yield
    finally:
        limits[identity.user_id] = (times + [now], False)


def receipt_request_schema():
    schema = ReceiptExtractInput.model_json_schema()
    definitions = schema.pop("$defs")
    schema["properties"]["image"] = definitions["ReceiptImage"]
    return schema


@router.post(
    "/receipts/extract",
    response_model=ChatExtractEnvelope,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": receipt_request_schema()}},
        }
    },
)
async def receipt_extract(request: Request, identity: VerifiedIdentity):
    """Read a bounded image request and return an editable draft, never an expense."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError(415, "INVALID_MEDIA_TYPE", "Kirim foto dalam format request JSON.")
    with extraction_slot(request, identity):
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_REQUEST_BYTES:
                raise ApiError(413, "IMAGE_TOO_LARGE", "Ukuran foto maksimal 10 MB.")
            payload.extend(chunk)
        try:
            body = ReceiptExtractInput.model_validate_json(payload)
        except ValidationError:
            # Pydantic error inputs can contain the complete base64 image; never return them.
            raise ApiError(
                422, "INVALID_RECEIPT_INPUT", "Foto atau keterangan tidak valid."
            ) from None
        del payload
        await run_in_threadpool(validate_image, body.image)
        categories, timezone = await run_in_threadpool(
            extraction_context, database(request), identity
        )
        data = await extract_receipt(
            request.app.state.settings,
            request.app.state.ai_http,
            body,
            categories,
            timezone,
            request.state.request_id,
        )
        return ChatExtractEnvelope(data=data)
