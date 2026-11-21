from contextlib import contextmanager
from time import monotonic, perf_counter

from fastapi import APIRouter, Request
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.db.models import ExpenseCategory
from cashly.schemas.chat import ChatExtractEnvelope, ChatExtractInput
from cashly.schemas.receipt import MAX_REQUEST_BYTES, ReceiptExtractInput
from cashly.schemas.voice import (
    MAX_AUDIO_REQUEST,
    TranscriptData,
    TranscriptEnvelope,
    VoiceExtractInput,
    VoiceTranscribeInput,
)
from cashly.services.accounts import load_account
from cashly.services.chat_extraction import extract_chat, logger
from cashly.services.receipt_extraction import (
    extract_receipt,
    validate_image,
)
from cashly.services.voice_audio import normalize_audio

router = APIRouter(tags=["Chat recording"])

CHAT_UNAVAILABLE = "Mochi belum bisa memproses pesanmu. Tunggu sebentar, lalu coba kirim lagi, ya."
VOICE_UNAVAILABLE = "Mochi belum bisa memproses catatan suaramu. Coba lagi sebentar, ya."
RECEIPT_UNAVAILABLE = "Mochi belum bisa membaca strukmu. Coba lagi sebentar, ya."


@contextmanager
def extraction_errors(message: str):
    try:
        yield
    except ApiError as error:
        if error.status_code < 500 and error.code != "AI_RATE_LIMITED":
            raise
        raise ApiError(error.status_code, error.code, message) from None


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
    with extraction_slot(request, identity), extraction_errors(CHAT_UNAVAILABLE):
        data = await extract_chat(
            request.app.state.settings,
            request.app.state.ai_http,
            body,
            categories,
            timezone,
            request.state.request_id,
        )
        return ChatExtractEnvelope(data=data)


def ensure_extraction_account(db, identity):
    with db.transaction(auth_user_id=identity.user_id) as session:
        load_account(session, identity)


def voice_request_schema():
    schema = VoiceTranscribeInput.model_json_schema()
    definitions = schema.pop("$defs")
    schema["properties"]["audio"] = definitions["VoiceAudio"]
    return schema


@router.post(
    "/voice/transcribe",
    response_model=TranscriptEnvelope,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": voice_request_schema()}},
        }
    },
)
async def transcribe_voice(request: Request, identity: VerifiedIdentity):
    """Transcribe for an active account; never writes expenses."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError(415, "INVALID_MEDIA_TYPE", "Kirim audio dalam format request JSON.")
    with extraction_slot(request, identity), extraction_errors(VOICE_UNAVAILABLE):
        await run_in_threadpool(ensure_extraction_account, database(request), identity)
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_AUDIO_REQUEST:
                raise ApiError(413, "AUDIO_TOO_LARGE", "Ukuran audio maksimal 10 MB.")
            payload.extend(chunk)
        try:
            body = VoiceTranscribeInput.model_validate_json(payload)
        except ValidationError:
            raise ApiError(
                422, "INVALID_AUDIO_INPUT", "Audio tidak valid atau melebihi 10 MB."
            ) from None
        del payload
        started = perf_counter()
        media = await run_in_threadpool(normalize_audio, body.audio)
        logger.info(
            "ai_extraction_audio_validated",
            extra={
                "request_id": request.state.request_id,
                "input_kind": "voice",
                "phase": "decode",
                "audio_duration_seconds": media.duration_seconds,
                "decode_ms": round((perf_counter() - started) * 1000, 2),
                "outcome": "validated",
            },
        )
        result = await extract_chat(
            request.app.state.settings,
            request.app.state.ai_http,
            ChatExtractInput(message="Transkripsikan ucapan dalam audio ini secara harfiah."),
            {},
            "UTC",
            request.state.request_id,
            media=media,
        )
        return TranscriptEnvelope(
            data=TranscriptData(
                **result.model_dump(),
                duration_seconds=media.duration_seconds,
            )
        )


@router.post("/voice/extract", response_model=ChatExtractEnvelope)
async def extract_voice(request: Request, identity: VerifiedIdentity, body: VoiceExtractInput):
    """The user-reviewed transcript uses the shared extraction/validation contract."""
    with extraction_slot(request, identity), extraction_errors(VOICE_UNAVAILABLE):
        categories, timezone = await run_in_threadpool(
            extraction_context, database(request), identity
        )
        data = await extract_chat(
            request.app.state.settings,
            request.app.state.ai_http,
            body,
            categories,
            timezone,
            request.state.request_id,
            input_kind="voice",
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
    with extraction_slot(request, identity), extraction_errors(RECEIPT_UNAVAILABLE):
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
