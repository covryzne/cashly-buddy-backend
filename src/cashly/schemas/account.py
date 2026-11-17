from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class RuntimeContext(BaseModel):
    environment: Literal["dev", "staging", "prod"]
    database_kind: Literal["local", "hosted"]


class AccountData(BaseModel):
    account_id: UUID
    display_name: str
    email: str
    timezone: str
    theme_preference: str
    joined_at: datetime
    sample_data: bool
    runtime: RuntimeContext | None = None


class AccountEnvelope(BaseModel):
    data: AccountData


class ExpenseData(BaseModel):
    id: UUID
    merchant: str
    description: str
    category_code: str
    amount_idr: str
    occurred_at: datetime
    payment_method: str
    source: str


class ExpensesEnvelope(BaseModel):
    data: list[ExpenseData]
    # Bounded first slice, not a claim that all history was fetched.
    has_more: bool
