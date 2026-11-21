"""Chat API compatibility exports and explicit-save batch contract."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from cashly.schemas.extraction import ChatChange as ChatChange
from cashly.schemas.extraction import ChatDraft as ChatDraft
from cashly.schemas.extraction import ChatEntry as ChatEntry
from cashly.schemas.extraction import ChatExtractData as ChatExtractData
from cashly.schemas.extraction import ChatExtractEnvelope as ChatExtractEnvelope
from cashly.schemas.extraction import ChatExtractInput as ChatExtractInput
from cashly.schemas.extraction import ChatMessage as ChatMessage
from cashly.schemas.extraction import DraftItem as DraftItem
from cashly.schemas.extraction import ModelExtraction as ModelExtraction
from cashly.schemas.extraction import StrictModel as StrictModel
from cashly.schemas.finance import ExpenseInput, ExpenseRead


class ChatBatchRecord(StrictModel):
    id: UUID
    expense: ExpenseInput
    source: Literal["chat", "receipt", "voice"] = "chat"


class ChatBatchInput(StrictModel):
    records: list[ChatBatchRecord] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_records(self):
        if len({r.id for r in self.records}) != len(self.records):
            raise ValueError("Duplicate entries")
        if any(not r.expense.items or r.expense.version is not None for r in self.records):
            raise ValueError("Chat records require items and no version")
        return self


class ChatBatchEnvelope(StrictModel):
    data: list[ExpenseRead]
