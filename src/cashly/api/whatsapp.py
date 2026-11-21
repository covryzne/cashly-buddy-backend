import hmac
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.schemas.receipt import MAX_REQUEST_BYTES
from cashly.schemas.whatsapp import (
    AdmissionEnvelope,
    CaptureEnvelope,
    CaptureInput,
    ChallengeEnvelope,
    ClaimEnvelope,
    ClaimInput,
    ConnectionEnvelope,
    DeliveryInput,
    EventInput,
    LinkHistoryEnvelope,
    VerifyInput,
)
from cashly.services import whatsapp, whatsapp_capture
from cashly.services.whatsapp_pdf import render_report

router = APIRouter(tags=["WhatsApp linking"])


def require_integration(
    request: Request,
    key: Annotated[str | None, Header(alias="X-Cashly-Integration-Key")] = None,
):
    settings = request.app.state.settings
    whatsapp.configured(settings)
    expected = settings.whatsapp_integration_key.get_secret_value()
    if key is None or not hmac.compare_digest(key.encode(), expected.encode()):
        raise ApiError(401, "WHATSAPP_INTEGRATION_UNAUTHORIZED", "Integrasi tidak diizinkan.")


Integration = Annotated[None, Depends(require_integration)]
ReportLease = Annotated[UUID, Header(alias="X-Cashly-Report-Lease")]


@router.get("/integrations/whatsapp/outbox/{reply_id}/report-data")
def outbox_report_data(request: Request, reply_id: UUID, lease: ReportLease):
    data = whatsapp.report_data(database(request), request.app.state.settings, reply_id, lease)
    return JSONResponse({"data": data}, headers={"Cache-Control": "private, no-store"})


@router.get("/integrations/whatsapp/outbox/{reply_id}/report-pdf")
def outbox_report_pdf(request: Request, reply_id: UUID, lease: ReportLease, _: Integration):
    settings = request.app.state.settings
    db = database(request)
    _, report = whatsapp.claimed_report(db, settings, reply_id, lease)
    origin = settings.whatsapp_report_frontend_url.rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ApiError(503, "WHATSAPP_PDF_UNAVAILABLE", "PDF belum tersedia di chat ini.")
    query = urlencode({"reply": str(reply_id), "lease": str(lease)})
    try:
        encoded = render_report(f"{origin}/internal/whatsapp-report?{query}")
    except ApiError as error:
        return {"data": {"media": None, "text": error.public_message}}
    whatsapp.claimed_report(db, settings, reply_id, lease)
    return {
        "data": {"media": encoded, "file_name": f"cashly-{report['start']}-{report['end']}.pdf"}
    }


@router.post("/me/whatsapp/challenges", response_model=ChallengeEnvelope)
def create_challenge(
    request: Request,
    identity: VerifiedIdentity,
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return whatsapp.create_challenge(database(request), request.app.state.settings, identity, key)


@router.get("/me/whatsapp", response_model=ConnectionEnvelope)
def status(request: Request, identity: VerifiedIdentity):
    return whatsapp.status(database(request), request.app.state.settings, identity)


@router.delete("/me/whatsapp", response_model=ConnectionEnvelope)
def disconnect(request: Request, identity: VerifiedIdentity):
    return whatsapp.disconnect(database(request), request.app.state.settings, identity)


@router.get("/me/whatsapp/history", response_model=LinkHistoryEnvelope)
def history(
    request: Request,
    identity: VerifiedIdentity,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    event: Literal["linked", "disconnected"] | None = None,
):
    return whatsapp.link_history(
        database(request), request.app.state.settings, identity, limit, cursor, event=event
    )


@router.post("/me/whatsapp/challenges/{challenge_id}/verify", response_model=ConnectionEnvelope)
def verify(request: Request, identity: VerifiedIdentity, challenge_id: UUID, body: VerifyInput):
    return whatsapp.verify(
        database(request),
        request.app.state.settings,
        identity,
        challenge_id,
        body.code,
    )


@router.post("/integrations/whatsapp/events", response_model=AdmissionEnvelope)
def events(request: Request, body: EventInput, _: Integration):
    return whatsapp.receive_event(database(request), request.app.state.settings, body)


@router.post("/integrations/whatsapp/jobs/{job_id}/process", response_model=CaptureEnvelope)
async def process_capture(
    request: Request,
    job_id: UUID,
    _: Integration,
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if key != f"wa-process:{job_id}":
        raise ApiError(400, "WHATSAPP_JOB_KEY", "Kunci pemrosesan tidak sesuai.")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError(415, "INVALID_MEDIA_TYPE", "Kirim pesan dalam format JSON.")
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_REQUEST_BYTES:
            raise ApiError(413, "MEDIA_TOO_LARGE", "Ukuran media maksimal 10 MB.")
        payload.extend(chunk)
    try:
        body = CaptureInput.model_validate_json(payload)
    except ValidationError:
        raise ApiError(422, "INVALID_WHATSAPP_CAPTURE", "Data pesan tidak valid.") from None
    del payload
    return await whatsapp_capture.process(
        database(request),
        request.app.state.settings,
        request.app.state.ai_http,
        job_id,
        body,
        request.state.request_id,
    )


@router.post("/integrations/whatsapp/outbox/claim", response_model=ClaimEnvelope)
def claim(request: Request, body: ClaimInput, _: Integration):
    return whatsapp.claim_outbox(database(request), request.app.state.settings, body.reply_id)


@router.post("/integrations/whatsapp/deliveries")
def deliveries(request: Request, body: DeliveryInput, _: Integration):
    return whatsapp.report_delivery(database(request), request.app.state.settings, body)
