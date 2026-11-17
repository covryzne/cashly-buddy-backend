from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,11})$")]


class ItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=99)
    unit_price_idr: Money


class ExpenseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merchant: str = Field(default="", max_length=100)
    description: str = Field(min_length=1, max_length=500)
    category_code: str = Field(max_length=32)
    occurred_at: datetime
    payment_method: Literal[
        "cash", "qris", "bank_transfer", "debit_card", "credit_card", "e_wallet", "unknown"
    ] = "unknown"
    amount_idr: Money
    discount_idr: Money = "0"
    tax_idr: Money = "0"
    items: list[ItemInput] = Field(default_factory=list, max_length=50)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_total(self):
        if self.occurred_at.tzinfo is None or not self.description.strip():
            raise ValueError("Timezone and description required")
        if any(not item.label.strip() for item in self.items):
            raise ValueError("Item label required")
        if int(self.amount_idr) <= 0:
            raise ValueError("Total must be positive")
        if self.items:
            subtotal = sum(item.quantity * int(item.unit_price_idr) for item in self.items)
            if int(self.discount_idr) > subtotal:
                raise ValueError("Discount exceeds subtotal")
            if subtotal - int(self.discount_idr) + int(self.tax_idr) != int(self.amount_idr):
                raise ValueError("Total mismatch")
        elif int(self.discount_idr) or int(self.tax_idr):
            raise ValueError("Adjustments require items")
        return self


class FreezeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protected_local_date: date


class PreferencesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=2, max_length=40)
    theme_preference: Literal["system", "light", "dark"] = "system"

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class ItemRead(ItemInput):
    item_index: int
    amount_idr: str


class ExpenseRead(BaseModel):
    id: str
    merchant: str
    description: str
    category_code: str
    occurred_at: datetime
    payment_method: str
    source: str
    amount_idr: str
    discount_idr: str
    tax_idr: str
    version: int
    items: list[ItemRead]


class ExpenseEnvelope(BaseModel):
    data: ExpenseRead


class ExpenseList(BaseModel):
    data: list[ExpenseRead]
    has_more: bool
    next_cursor: str | None


class DeleteData(BaseModel):
    id: str
    version: int
    deleted_at: datetime
    restore_until: datetime


class DeleteEnvelope(BaseModel):
    data: DeleteData


class GameMission(BaseModel):
    code: str
    title: str
    completed: bool
    xp: int
    can_complete: bool = False
    available_from: date | None = None
    test_override: bool = False


class CalendarDay(BaseModel):
    date: date
    status: Literal["active", "protected", "missed", "upcoming"]


class Recovery(BaseModel):
    protected_local_date: date


class GameOverview(BaseModel):
    level: int
    total_xp: int
    current_streak: int
    longest_streak: int
    freeze_balance: int
    max_mascot_stage_unlocked: int
    today_checked_in: bool
    today_local_date: date
    timezone: str
    recovery: Recovery | None
    calendar: list[CalendarDay]
    missions: list[GameMission]


class GameEnvelope(BaseModel):
    data: GameOverview
