"""Extraction drafts are separate from committed expense inputs."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cashly.schemas.finance import Money


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DraftItem(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    quantity: str = Field(pattern=r"^[1-9][0-9]?$")
    priceIdr: Money


class ChatDraft(StrictModel):
    merchant: str = Field(max_length=100)
    categoryCode: str = Field(min_length=1, max_length=32)
    paymentMethod: Literal[
        "",
        "QRIS",
        "Tunai",
        "Kartu debit",
        "Transfer bank",
        "Dompet digital",
        "Kartu kredit",
        "Lainnya",
    ]
    occurredDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    occurredTime: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?$")
    items: list[DraftItem] = Field(min_length=1, max_length=50)
    discountIdr: Money
    taxIdr: Money

    @field_validator("occurredDate")
    @classmethod
    def valid_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid date")
        return value

    @model_validator(mode="after")
    def valid_total(self):
        subtotal = sum(int(i.quantity) * int(i.priceIdr) for i in self.items)
        total = subtotal + int(self.taxIdr) - int(self.discountIdr)
        if int(self.discountIdr) > subtotal or not 0 < total <= 999999999999:
            raise ValueError("Invalid total")
        return self


class ChatEntry(StrictModel):
    id: UUID
    text: str = Field(min_length=1, max_length=4000)
    createdAt: datetime
    draft: ChatDraft | None
    issue: str | None = Field(max_length=240)
    reason: Literal["name", "amount", "ambiguous"] | None

    @field_validator("createdAt")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Timezone required")
        return value


class ChatMessage(StrictModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=1000)


class ChatExtractInput(StrictModel):
    message: str = Field(min_length=1, max_length=1000)
    entries: list[ChatEntry] = Field(default_factory=list, max_length=50)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)
    edit_entry_id: UUID | None = None

    @model_validator(mode="after")
    def valid_context(self):
        ids = [entry.id for entry in self.entries]
        if len(ids) != len(set(ids)) or (
            self.edit_entry_id is not None and self.edit_entry_id not in ids
        ):
            raise ValueError("Invalid entry IDs")
        if len(self.model_dump_json().encode()) > 60000:
            raise ValueError("Chat context too large")
        return self


class ChatChange(StrictModel):
    target_id: UUID | None
    draft: ChatDraft | None
    question: str | None = Field(max_length=240)
    reason: Literal["name", "amount", "ambiguous"] | None
    source_total_idr: Money | None = None


class ModelExtraction(StrictModel):
    intent: Literal["record", "clarify", "out_of_scope", "help"]
    question: str | None = Field(max_length=240)
    changes: list[ChatChange] = Field(max_length=50)


class ChatExtractData(StrictModel):
    entries: list[ChatEntry]
    reply: str


class ChatExtractEnvelope(StrictModel):
    data: ChatExtractData
