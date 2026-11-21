from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyInput(StrictInput):
    code: str = Field(pattern=r"^[0-9]{6}$")


class ChallengeData(BaseModel):
    id: UUID
    state: str
    expires_at: datetime
    resend_at: datetime
    attempts_remaining: int
    chat_url: str
    masked_number: str | None = None


class ChallengeEnvelope(BaseModel):
    data: ChallengeData


class ConnectionData(BaseModel):
    status: Literal["connected", "not_linked"]
    masked_number: str | None = None
    connected_at: datetime | None = None
    challenge: ChallengeData | None = None
    recording_available: bool = False


class ConnectionEnvelope(BaseModel):
    data: ConnectionData


class LinkHistoryData(BaseModel):
    id: UUID
    event: Literal["linked", "disconnected"]
    occurred_at: AwareDatetime
    masked_number: str
    source: Literal["action", "existing_connection"]


class LinkHistoryEnvelope(BaseModel):
    data: list[LinkHistoryData]
    next_cursor: str | None = None


class MediaHint(StrictInput):
    mime_type: str | None = Field(default=None, max_length=100)


class EventInput(StrictInput):
    contract_version: Literal["cashly-wa-v1"]
    environment: Literal["dev", "staging", "prod"]
    provider: Literal["evolution"]
    bot_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    event_type: Literal["messages.upsert"]
    message_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,255}$")
    sender_id: str = Field(pattern=r"^[0-9]+(?::[0-9]+)?@(s\.whatsapp\.net|lid)$", max_length=100)
    sender_phone_jid: str | None = Field(
        default=None, pattern=r"^[1-9][0-9]{7,14}@s\.whatsapp\.net$"
    )
    addressing_mode: str | None = Field(default=None, max_length=20)
    sent_at: AwareDatetime
    received_at: AwareDatetime
    kind: Literal["text", "receipt", "voice"]
    command_hint: str = Field(default="expense_or_correction", max_length=40)
    text: str | None = Field(default=None, max_length=4000)
    caption: str | None = Field(default=None, max_length=1000)
    quoted_message_id: str | None = Field(default=None, max_length=255)
    media: MediaHint | None = None


class ReplyData(BaseModel):
    id: UUID
    text: str
    lease_id: UUID | None = None
    report_pdf: bool = False


class AdmissionData(BaseModel):
    event_id: UUID
    bot_id: str
    message_id: str
    sender_id: str
    action: Literal["reply", "ignore", "process"]
    verified_link: bool = False
    job_id: UUID | None = None
    kind: Literal["text", "receipt", "voice"]
    reply: ReplyData | None = None


class AdmissionEnvelope(BaseModel):
    data: AdmissionData


class ClaimInput(StrictInput):
    reply_id: UUID | None = None


class ClaimEnvelope(BaseModel):
    data: AdmissionData | None


class CaptureMedia(StrictInput):
    mime_type: str = Field(min_length=1, max_length=80)
    data: str = Field(min_length=4, max_length=4 * ((10 * 1024 * 1024 + 2) // 3))


class CaptureInput(StrictInput):
    event_id: UUID
    kind: Literal["text", "receipt", "voice"]
    media: CaptureMedia | None = None
    input_error: (
        Literal[
            "MEDIA_FETCH_FAILED",
            "MEDIA_TYPE_UNSUPPORTED",
            "MEDIA_BASE64_INVALID",
            "MEDIA_TOO_LARGE",
        ]
        | None
    ) = None


class CaptureOutcome(BaseModel):
    event_id: UUID
    job_id: UUID
    status: Literal["saved", "needs_review", "failed", "queued", "ignored"]
    committed: bool = False
    reply: ReplyData | None = None


class CaptureEnvelope(BaseModel):
    data: CaptureOutcome


class DeliveryInput(StrictInput):
    contract_version: Literal["cashly-wa-v1"]
    environment: Literal["dev", "staging", "prod"]
    provider: Literal["evolution"]
    bot_id: str
    event_id: UUID
    reply_id: UUID
    lease_id: UUID
    recipient_id: str = Field(max_length=100)
    status: Literal["accepted", "unknown"]
    outbound_message_id: str | None = Field(default=None, max_length=255)
    error_code: Literal["OUTBOUND_UNCONFIRMED"] | None = None
