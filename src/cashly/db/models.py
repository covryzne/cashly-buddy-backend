"""Core storage only. Signup, restore, adjustment and reward behavior remains pending."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "cashly"


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# Reference only: Supabase owns this table. Never create/drop/alter it via Alembic.
Table(
    "users",
    Base.metadata,
    Column("id", Uuid, primary_key=True),
    schema="auth",
    info={"external": True},
)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Timestamps, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="status"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(16), server_default="active")


class Profile(Timestamps, Base):
    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint("char_length(btrim(display_name)) BETWEEN 2 AND 40", name="display_name"),
        CheckConstraint("username ~ '^[a-z0-9._]{3,24}$'", name="username_format"),
        Index("uq_profiles_username", text("lower(username)"), unique=True),
        CheckConstraint("theme_preference IN ('system','light','dark')", name="theme"),
        {"schema": SCHEMA},
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(40))
    username: Mapped[str] = mapped_column(String(24), default=lambda: "cashly_" + uuid4().hex[:16])
    password_login_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Jakarta")
    theme_preference: Mapped[str] = mapped_column(String(8), server_default="system")


class AccountAuthLink(Base):
    __tablename__ = "account_auth_links"
    __table_args__ = (Index("ix_auth_links_account", "account_id"), {"schema": SCHEMA})
    auth_user_id: Mapped[UUID] = mapped_column(ForeignKey("auth.users.id"), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"
    __table_args__ = {"schema": SCHEMA}
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    label_id: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class Expense(Timestamps, Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount_idr > 0", name="positive_amount"),
        CheckConstraint("discount_idr >= 0", name="discount_amount"),
        CheckConstraint("tax_idr >= 0", name="tax_amount"),
        CheckConstraint("char_length(btrim(description)) BETWEEN 1 AND 500", name="description"),
        CheckConstraint("source_item_index >= 0", name="source_item_index"),
        CheckConstraint("char_length(source_message_id) BETWEEN 1 AND 255", name="message_id"),
        CheckConstraint(
            "payment_method IN ('cash','qris','bank_transfer','debit_card',"
            "'credit_card','e_wallet','unknown')",
            name="payment_method",
        ),
        CheckConstraint(
            "source_type IN ('text','voice','receipt','dashboard')", name="source_type"
        ),
        CheckConstraint("source_platform IN ('whatsapp','dashboard')", name="source_platform"),
        CheckConstraint(
            "timestamp_basis IN ('receipt','user_explicit','message_time')", name="timestamp_basis"
        ),
        UniqueConstraint(
            "account_id",
            "source_platform",
            "source_message_id",
            "source_item_index",
            name="uq_expenses_source",
        ),
        Index(
            "ix_expenses_account_time",
            "account_id",
            text("occurred_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_expenses_account_category_time",
            "account_id",
            "category_code",
            text("occurred_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_expenses_category", "category_code"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"))
    amount_idr: Mapped[int] = mapped_column(BigInteger)
    discount_idr: Mapped[int] = mapped_column(BigInteger, server_default="0")
    tax_idr: Mapped[int] = mapped_column(BigInteger, server_default="0")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    category_code: Mapped[str] = mapped_column(ForeignKey("cashly.expense_categories.code"))
    description: Mapped[str] = mapped_column(String(500))
    merchant: Mapped[str | None] = mapped_column(String(200))
    payment_method: Mapped[str] = mapped_column(String(20), server_default="unknown")
    source_platform: Mapped[str] = mapped_column(String(16))
    source_type: Mapped[str] = mapped_column(String(16))
    source_message_id: Mapped[str] = mapped_column(String(255))
    source_item_index: Mapped[int] = mapped_column(Integer, server_default="0")
    timestamp_basis: Mapped[str] = mapped_column(String(20))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExpenseItem(Base):
    __tablename__ = "expense_items"
    __table_args__ = (
        UniqueConstraint("expense_id", "item_index", name="uq_expense_items_position"),
        CheckConstraint("item_index >= 0", name="item_index"),
        CheckConstraint("amount_idr >= 0", name="line_amount"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("unit_price_idr >= 0", name="unit_price"),
        CheckConstraint(
            "amount_idr = quantity::numeric * unit_price_idr::numeric", name="line_total"
        ),
        CheckConstraint("char_length(btrim(label)) BETWEEN 1 AND 200", name="label"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    expense_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.expenses.id"))
    item_index: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, server_default="1")
    unit_price_idr: Mapped[int] = mapped_column(BigInteger)
    amount_idr: Mapped[int] = mapped_column(BigInteger)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("account_id", "operation", "key", name="uq_idempotency_scope"),
        CheckConstraint("response_status BETWEEN 100 AND 599", name="response_status"),
        CheckConstraint("char_length(key) BETWEEN 8 AND 128", name="key_length"),
        CheckConstraint("request_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"))
    operation: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (
        UniqueConstraint("account_id", "event_key", name="uq_game_event_key"),
        Index("ix_game_events_account_date", "account_id", "local_date"),
        Index(
            "uq_game_daily_checkin",
            "account_id",
            "local_date",
            unique=True,
            postgresql_where=text("kind = 'checkin'"),
        ),
        CheckConstraint("xp >= 0", name="xp"),
        {"schema": SCHEMA},
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"))
    event_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(24))
    local_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    xp: Mapped[int] = mapped_column(Integer, server_default="0")
    freeze_delta: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GameState(Base):
    __tablename__ = "game_states"
    __table_args__ = (
        CheckConstraint("max_stage BETWEEN 0 AND 5", name="stage"),
        {"schema": SCHEMA},
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"), primary_key=True)
    max_stage: Mapped[int] = mapped_column(Integer, server_default="0")


class StreakProtection(Base):
    __tablename__ = "streak_protections"
    __table_args__ = (Index("ix_protection_month", "account_id", "quota_month"), {"schema": SCHEMA})
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"), primary_key=True)
    protected_date: Mapped[date] = mapped_column(Date, primary_key=True)
    quota_month: Mapped[date] = mapped_column(Date)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountSecurityChallenge(Base):
    __tablename__ = "account_security_challenges"
    __table_args__ = ({"schema": SCHEMA},)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id"))
    auth_user_id: Mapped[UUID] = mapped_column(Uuid)
    purpose: Mapped[str] = mapped_column(String(24))
    current_email: Mapped[str] = mapped_column(String(254))
    new_email: Mapped[str | None] = mapped_column(String(254))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_current: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    confirmed_new: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
