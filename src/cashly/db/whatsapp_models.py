from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cashly.db.models import Base


class WhatsAppChallenge(Base):
    __tablename__ = "whatsapp_challenges"
    __table_args__ = {"schema": "cashly"}
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("cashly.accounts.id", ondelete="CASCADE"))
    request_hash: Mapped[str] = mapped_column(String(64))
    reference_hash: Mapped[str] = mapped_column(String(64), unique=True)
    reference_cipher: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resend_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="awaiting_message")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sender_hash: Mapped[str | None] = mapped_column(String(64))
    sender_cipher: Mapped[str | None] = mapped_column(Text)
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    phone_last4: Mapped[str | None] = mapped_column(String(4))
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    otp_digest: Mapped[str | None] = mapped_column(String(64))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppChannel(Base):
    __tablename__ = "whatsapp_channels"
    __table_args__ = {"schema": "cashly"}
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("cashly.accounts.id", ondelete="CASCADE"), unique=True
    )
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True)
    phone_last4: Mapped[str] = mapped_column(String(4))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WhatsAppAlias(Base):
    __tablename__ = "whatsapp_aliases"
    __table_args__ = {"schema": "cashly"}
    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("cashly.whatsapp_channels.id", ondelete="CASCADE")
    )


class WhatsAppEvent(Base):
    __tablename__ = "whatsapp_events"
    __table_args__ = {"schema": "cashly"}
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    bot_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(255))
    sender_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cashly.accounts.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WhatsAppOutbox(Base):
    __tablename__ = "whatsapp_outbox"
    __table_args__ = {"schema": "cashly"}
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("cashly.whatsapp_events.id", ondelete="CASCADE"), unique=True
    )
    account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cashly.accounts.id", ondelete="CASCADE")
    )
    challenge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cashly.whatsapp_challenges.id", ondelete="CASCADE")
    )
    payload_cipher: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbound_message_id: Mapped[str | None] = mapped_column(String(255))
