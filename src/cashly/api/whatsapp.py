import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.schemas.whatsapp import (
    AdmissionEnvelope,
    ChallengeEnvelope,
    ClaimEnvelope,
    ClaimInput,
    ConnectionEnvelope,
    DeliveryInput,
    EventInput,
    VerifyInput,
)
from cashly.services import whatsapp

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


@router.post("/integrations/whatsapp/outbox/claim", response_model=ClaimEnvelope)
def claim(request: Request, body: ClaimInput, _: Integration):
    return whatsapp.claim_outbox(database(request), request.app.state.settings, body.reply_id)


@router.post("/integrations/whatsapp/deliveries")
def deliveries(request: Request, body: DeliveryInput, _: Integration):
    return whatsapp.report_delivery(database(request), request.app.state.settings, body)
