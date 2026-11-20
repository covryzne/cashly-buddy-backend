"""Account-scoped linking; no AI, media fetch or financial mutations."""

import hashlib
import hmac
import json
import re
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select, text, update

from cashly.core.errors import ApiError
from cashly.db.models import Account
from cashly.db.whatsapp_models import (
    WhatsAppAlias as Alias,
)
from cashly.db.whatsapp_models import (
    WhatsAppChallenge as Challenge,
)
from cashly.db.whatsapp_models import (
    WhatsAppChannel as Channel,
)
from cashly.db.whatsapp_models import (
    WhatsAppEvent as Event,
)
from cashly.db.whatsapp_models import (
    WhatsAppOutbox as Outbox,
)
from cashly.repositories.accounts import set_account_context
from cashly.services.accounts import load_account

PENDING = ("awaiting_message", "otp_sent")
TTL = timedelta(minutes=5)
COOLDOWN = timedelta(seconds=60)
LEASE = timedelta(minutes=3)
LINK_MESSAGE = re.compile(
    r"^Halo Cashly, hubungkan WhatsApp saya\. Kode koneksi:\s*([A-Za-z0-9_-]{32,64})\s*$",
    re.IGNORECASE,
)


def clock():
    return datetime.now(UTC)


def configured(settings):
    keys = (settings.whatsapp_integration_key, settings.whatsapp_hash_secret)
    if (
        not settings.whatsapp_enabled
        or not settings.whatsapp_bot_number
        or any(key is None or len(key.get_secret_value()) < 32 for key in keys)
    ):
        raise ApiError(503, "WHATSAPP_NOT_CONFIGURED", "Koneksi WhatsApp belum tersedia.")
    try:
        return Fernet(settings.whatsapp_encryption_key.get_secret_value().encode())
    except (ValueError, TypeError, AttributeError):
        raise ApiError(503, "WHATSAPP_NOT_CONFIGURED", "Koneksi WhatsApp belum tersedia.") from None


def digest(settings, purpose, value):
    return hmac.new(
        settings.whatsapp_hash_secret.get_secret_value().encode(),
        (purpose + ":" + value).encode(),
        hashlib.sha256,
    ).hexdigest()


def encrypt(settings, value):
    return configured(settings).encrypt(json.dumps(value).encode()).decode()


def decrypt(settings, value):
    try:
        return json.loads(configured(settings).decrypt(value.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise ApiError(
            503, "WHATSAPP_KEY_UNAVAILABLE", "Koneksi WhatsApp perlu diperiksa."
        ) from None


@contextmanager
def transaction(db, settings, identity=None):
    configured(settings)
    with db.transaction(auth_user_id=identity.user_id if identity else None) as session:
        # Low-volume linking only: one short DB-only critical section. No network
        # calls while locked; the integration capability applies to new WA tables only.
        session.execute(text("SELECT pg_advisory_xact_lock(714001)"))
        session.execute(text("SELECT set_config('cashly.whatsapp_integration','on',true)"))
        now = clock()
        cleanup(session, now)
        account = load_account(session, identity)[0] if identity else None
        yield session, now, account


def cleanup(session, now):
    session.execute(
        update(Challenge)
        .where(
            Challenge.state.in_(PENDING),
            func.coalesce(Challenge.otp_expires_at, Challenge.expires_at) <= now,
        )
        .values(state="expired", otp_digest=None)
    )
    session.execute(
        update(Outbox)
        .where(
            Outbox.state.in_(("ready", "claimed")),
            Outbox.expires_at <= now,
        )
        .values(state="expired", payload_cipher=None)
    )
    session.execute(
        update(Outbox)
        .where(
            Outbox.state == "claimed",
            Outbox.lease_until <= now,
        )
        .values(state="unknown", payload_cipher=None)
    )
    session.execute(delete(Event).where(Event.created_at < now - timedelta(days=1)))


def cancel_pending(session, account_id):
    session.execute(
        update(Challenge)
        .where(
            Challenge.account_id == account_id,
            Challenge.state.in_(PENDING),
        )
        .values(state="cancelled", otp_digest=None)
    )
    session.execute(
        update(Outbox)
        .where(
            Outbox.account_id == account_id,
            Outbox.state.in_(("ready", "claimed")),
        )
        .values(state="cancelled", payload_cipher=None)
    )


def challenge_data(settings, row):
    reference = decrypt(settings, row.reference_cipher)
    message = "Halo Cashly, hubungkan WhatsApp saya. Kode koneksi: " + reference
    return {
        "id": row.id,
        "state": row.state,
        "expires_at": row.otp_expires_at or row.expires_at,
        "resend_at": row.resend_at,
        "attempts_remaining": max(0, 5 - row.attempts),
        "chat_url": "https://wa.me/"
        + settings.whatsapp_bot_number
        + "?"
        + urlencode({"text": message}),
        "masked_number": "•••• " + row.phone_last4 if row.phone_last4 else None,
    }


def create_challenge(db, settings, identity, request_key):
    if not request_key or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_key):
        raise ApiError(422, "IDEMPOTENCY_KEY_REQUIRED", "Identitas permintaan diperlukan.")
    with transaction(db, settings, identity) as (session, now, account):
        if session.scalar(select(Channel).where(Channel.account_id == account.id)):
            raise ApiError(409, "WHATSAPP_ALREADY_LINKED", "Putuskan koneksi lama terlebih dahulu.")
        request_hash = digest(settings, "request", request_key)
        previous = session.scalar(
            select(Challenge).where(
                Challenge.account_id == account.id,
                Challenge.request_hash == request_hash,
            )
        )
        if previous:
            return {"data": challenge_data(settings, previous)}
        latest = session.scalar(
            select(Challenge)
            .where(Challenge.account_id == account.id)
            .order_by(Challenge.created_at.desc())
            .limit(1)
        )
        if latest and latest.resend_at > now:
            raise ApiError(
                429, "WHATSAPP_RESEND_COOLDOWN", "Tunggu 60 detik sebelum meminta kode baru."
            )
        cancel_pending(session, account.id)
        reference = secrets.token_urlsafe(32)
        row = Challenge(
            account_id=account.id,
            request_hash=request_hash,
            reference_hash=digest(settings, "reference", reference),
            reference_cipher=encrypt(settings, reference),
            created_at=now,
            expires_at=now + TTL,
            resend_at=now + COOLDOWN,
            state="awaiting_message",
            attempts=0,
            aliases=[],
        )
        session.add(row)
        session.flush()
        return {"data": challenge_data(settings, row)}


def status(db, settings, identity):
    with transaction(db, settings, identity) as (session, _, account):
        channel = session.scalar(select(Channel).where(Channel.account_id == account.id))
        pending = session.scalar(
            select(Challenge)
            .where(
                Challenge.account_id == account.id,
                Challenge.state.in_((*PENDING, "locked", "expired")),
            )
            .order_by(Challenge.created_at.desc())
            .limit(1)
        )
        return {
            "data": {
                "status": "connected" if channel else "not_linked",
                "masked_number": "•••• " + channel.phone_last4 if channel else None,
                "connected_at": channel.verified_at if channel else None,
                "challenge": challenge_data(settings, pending) if pending and not channel else None,
                "recording_available": False,
            }
        }


def disconnect(db, settings, identity):
    with transaction(db, settings, identity) as (session, _, account):
        cancel_pending(session, account.id)
        session.execute(delete(Channel).where(Channel.account_id == account.id))
    return {"data": {"status": "not_linked", "recording_available": False}}


def verify(db, settings, identity, challenge_id, code):
    error = None
    with transaction(db, settings, identity) as (session, now, account):
        row = session.scalar(
            select(Challenge)
            .where(
                Challenge.id == challenge_id,
                Challenge.account_id == account.id,
            )
            .with_for_update()
        )
        if row is None:
            raise ApiError(
                404, "WHATSAPP_CHALLENGE_NOT_FOUND", "Permintaan koneksi tidak ditemukan."
            )
        if row.state != "otp_sent" or not row.otp_digest or row.otp_expires_at <= now:
            error = ApiError(
                409,
                "WHATSAPP_CHALLENGE_INACTIVE",
                "Kode belum tersedia atau sudah berakhir. Minta kode baru.",
            )
        elif not hmac.compare_digest(row.otp_digest, digest(settings, "otp", f"{row.id}:{code}")):
            row.attempts += 1
            if row.attempts >= 5:
                row.state, row.otp_digest = "locked", None
                session.execute(
                    update(Outbox)
                    .where(Outbox.challenge_id == row.id)
                    .values(state="cancelled", payload_cipher=None)
                )
            error = ApiError(
                400 if row.attempts < 5 else 429,
                "WHATSAPP_OTP_INVALID",
                "Kode belum cocok."
                if row.attempts < 5
                else "Batas percobaan tercapai. Minta kode baru.",
            )
        else:
            conflicts = session.scalar(
                select(Channel).where(
                    (Channel.phone_hash == row.phone_hash) | (Channel.account_id == account.id),
                )
            )
            alias_conflict = session.scalar(
                select(Alias).where(Alias.identity_hash.in_(row.aliases))
            )
            if conflicts or alias_conflict:
                row.state, row.otp_digest = "cancelled", None
                session.execute(
                    update(Outbox)
                    .where(Outbox.challenge_id == row.id)
                    .values(state="cancelled", payload_cipher=None)
                )
                error = ApiError(
                    409,
                    "WHATSAPP_LINK_CONFLICT",
                    "Nomor ini tidak dapat dihubungkan. Periksa koneksi akunmu.",
                )
            else:
                channel = Channel(
                    account_id=account.id,
                    phone_hash=row.phone_hash,
                    phone_last4=row.phone_last4,
                    verified_at=now,
                )
                session.add(channel)
                session.flush()
                session.add_all(
                    [Alias(identity_hash=value, channel_id=channel.id) for value in row.aliases]
                )
                row.state, row.otp_digest = "verified", None
                session.execute(
                    update(Outbox)
                    .where(Outbox.account_id == account.id)
                    .values(payload_cipher=None)
                )
    if error:
        raise error
    return status(db, settings, identity)


def identity_keys(settings, body):
    candidates = [body.sender_id, body.sender_phone_jid]
    phones = {
        value
        for value in candidates
        if value and re.fullmatch(r"[1-9][0-9]{7,14}@s\.whatsapp\.net", value)
    }
    if len(phones) > 1:
        return None, []
    aliases = list({digest(settings, "sender", value) for value in candidates if value})
    return next(iter(phones), None), aliases


def admission(settings, row, outbox=None, payload=None, lease_id=None):
    if payload:
        data = payload.copy()
        data["reply"] = {**payload["reply"], "lease_id": lease_id}
        return {"data": data}
    return {
        "data": {
            "event_id": str(row.id),
            "bot_id": row.bot_id,
            "message_id": row.message_id,
            "sender_id": "",
            "kind": "text",
            "action": "ignore",
            "verified_link": False,
        }
    }


def receive_event(db, settings, body):
    if body.environment != settings.environment or body.bot_id != settings.whatsapp_instance:
        raise ApiError(403, "WHATSAPP_INTEGRATION_SCOPE", "Integrasi tidak diizinkan.")
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json", exclude={"received_at"}), sort_keys=True).encode()
    ).hexdigest()
    sender_hash = digest(settings, "sender", body.sender_id)
    with transaction(db, settings) as (session, now, _):
        if body.sent_at > now + TTL or body.sent_at < now - timedelta(days=1):
            raise ApiError(422, "WHATSAPP_EVENT_TIME", "Waktu pesan tidak valid.")
        existing = session.scalar(
            select(Event).where(Event.bot_id == body.bot_id, Event.message_id == body.message_id)
        )
        base = {
            "bot_id": body.bot_id,
            "message_id": body.message_id,
            "sender_id": body.sender_id,
            "kind": body.kind,
            "verified_link": False,
        }
        if existing:
            if existing.fingerprint != fingerprint:
                raise ApiError(409, "WHATSAPP_EVENT_CONFLICT", "Identitas pesan sudah digunakan.")
            out = session.scalar(select(Outbox).where(Outbox.event_id == existing.id))
            if out and out.state == "ready" and out.payload_cipher:
                return {"data": decrypt(settings, out.payload_cipher)}
            return {"data": {**base, "event_id": str(existing.id), "action": "ignore"}}
        recent = session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.sender_hash == sender_hash, Event.created_at > now - COOLDOWN)
        )
        if recent >= 10:
            raise ApiError(429, "WHATSAPP_RATE_LIMITED", "Terlalu banyak pesan. Tunggu sebentar.")
        row = Event(
            bot_id=body.bot_id,
            message_id=body.message_id,
            sender_hash=sender_hash,
            fingerprint=fingerprint,
            created_at=now,
        )
        session.add(row)
        session.flush()
        phone, aliases = identity_keys(settings, body)
        phone_hash = digest(settings, "phone", phone) if phone else None
        channel = session.scalar(
            select(Channel)
            .join(Alias, Alias.channel_id == Channel.id)
            .where(Alias.identity_hash == sender_hash)
        )
        if channel and phone_hash and channel.phone_hash != phone_hash:
            channel = None
            phone = None
        if channel:
            row.account_id = channel.account_id
            base["verified_link"] = True
        message = (body.text or "").strip()
        match = LINK_MESSAGE.fullmatch(message) if body.kind == "text" else None
        challenge = None
        reply = (
            "Nomor ini belum terhubung ke Cashly. Buka Cashly → Profil → Hubungkan WhatsApp, "
            "lalu verifikasi OTP dulu ya."
        )
        if match:
            challenge = session.scalar(
                select(Challenge)
                .where(
                    Challenge.reference_hash == digest(settings, "reference", match[1]),
                )
                .with_for_update()
            )
            if not challenge or challenge.state not in PENDING or challenge.expires_at <= now:
                challenge = None
                reply = "Permintaan koneksi sudah berakhir. Mulai lagi dari Profil Cashly ya."
            elif challenge.sender_hash:
                reply = (
                    "Kode sudah diminta. Periksa balasan sebelumnya "
                    "atau minta kode baru dari Profil Cashly."
                )
                challenge = None
            elif not phone or body.sent_at < challenge.created_at - timedelta(seconds=10):
                challenge = None
                reply = (
                    "Identitas nomor belum dapat diverifikasi. Mulai ulang dari Profil Cashly ya."
                )
            else:
                set_account_context(session, challenge.account_id)
                owner = session.get(Account, challenge.account_id)
                conflict = session.scalar(select(Channel).where(Channel.phone_hash == phone_hash))
                alias_conflict = session.scalar(
                    select(Alias).where(Alias.identity_hash.in_(aliases))
                )
                if not owner or owner.status != "active" or conflict or alias_conflict:
                    challenge = None
                    reply = "Nomor ini tidak dapat dihubungkan. Periksa koneksi akunmu di Cashly."
                else:
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    challenge.sender_hash = sender_hash
                    challenge.sender_cipher = encrypt(settings, body.sender_id)
                    challenge.phone_hash, challenge.phone_last4 = (
                        phone_hash,
                        phone.split("@")[0][-4:],
                    )
                    challenge.aliases = aliases
                    challenge.otp_digest = digest(settings, "otp", f"{challenge.id}:{code}")
                    challenge.otp_expires_at = now + TTL
                    challenge.resend_at = max(challenge.resend_at, now + COOLDOWN)
                    challenge.state = "otp_sent"
                    row.account_id = challenge.account_id
                    reply = (
                        f"Kode OTP Cashly kamu: {code}. Berlaku 5 menit. "
                        "Masukkan di aplikasi Cashly dan jangan bagikan kode ini."
                    )
        elif re.match(r"^/(help|info|start)(?:\s|$)", message, re.IGNORECASE):
            reply = (
                "Hai cuy! Hubungkan nomor lewat Profil Cashly dan verifikasi OTP di aplikasi. "
                "Pencatatan chat, foto struk dan VN WhatsApp masih disiapkan."
            )
        elif channel:
            reply = (
                "Nomormu sudah terhubung. Pencatatan WhatsApp masih disiapkan; "
                "sementara catat lewat aplikasi Cashly ya."
            )
        outbox = Outbox(
            event_id=row.id,
            account_id=row.account_id,
            challenge_id=challenge.id if challenge else None,
            created_at=now,
            expires_at=challenge.otp_expires_at if challenge else now + TTL,
            state="ready",
        )
        session.add(outbox)
        session.flush()
        payload = {
            **base,
            "event_id": str(row.id),
            "action": "reply",
            "reply": {"id": str(outbox.id), "text": reply},
        }
        outbox.payload_cipher = encrypt(settings, payload)
        return {"data": payload}


def claim_outbox(db, settings, reply_id=None):
    with transaction(db, settings) as (session, now, _):
        query = select(Outbox).where(
            Outbox.state == "ready", Outbox.expires_at > now, Outbox.payload_cipher.is_not(None)
        )
        if reply_id:
            query = query.where(Outbox.id == reply_id)
        row = session.scalar(query.order_by(Outbox.created_at).limit(1).with_for_update())
        if row is None:
            return {"data": None}
        payload = decrypt(settings, row.payload_cipher)
        row.state, row.lease_id, row.lease_until = "claimed", uuid4(), now + LEASE
        return admission(settings, None, row, payload, str(row.lease_id))


def report_delivery(db, settings, body):
    if body.environment != settings.environment or body.bot_id != settings.whatsapp_instance:
        raise ApiError(403, "WHATSAPP_INTEGRATION_SCOPE", "Integrasi tidak diizinkan.")
    with transaction(db, settings) as (session, _, __):
        row = session.scalar(select(Outbox).where(Outbox.id == body.reply_id).with_for_update())
        event = session.get(Event, row.event_id) if row else None
        if (
            not row
            or row.lease_id != body.lease_id
            or row.event_id != body.event_id
            or not event
            or event.bot_id != body.bot_id
            or event.sender_hash != digest(settings, "sender", body.recipient_id)
        ):
            raise ApiError(409, "WHATSAPP_DELIVERY_SCOPE", "Laporan pengiriman tidak cocok.")
        if row.state in ("cancelled", "expired"):
            return {"data": {"status": "ignored"}}
        if body.status == "accepted" and not body.outbound_message_id:
            raise ApiError(422, "WHATSAPP_MESSAGE_ID_REQUIRED", "Identitas pesan kirim diperlukan.")
        if row.state == "accepted":
            if body.status != "accepted" or row.outbound_message_id != body.outbound_message_id:
                raise ApiError(
                    409, "WHATSAPP_DELIVERY_CONFLICT", "Status pengiriman sudah tercatat."
                )
        elif row.state in ("claimed", "unknown"):
            row.state = body.status
            row.outbound_message_id = body.outbound_message_id
            row.payload_cipher = None
        return {"data": {"status": "recorded"}}
