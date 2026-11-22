from datetime import datetime, time, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.services.accounts import load_account
from cashly.services.notification_worker import WIB
from cashly.services.web_push import (
    BODY,
    TITLE,
    decode,
    encode,
    public_bytes,
    receiver_key,
    validate_endpoint,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class ReminderInput(BaseModel):
    daily_enabled: bool
    reminder_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class EndpointInput(BaseModel):
    endpoint: str = Field(max_length=2048)

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, value: str) -> str:
        validate_endpoint(value)
        return value


class SubscriptionInput(EndpointInput):
    p256dh: str = Field(min_length=87, max_length=87)
    auth: str = Field(min_length=22, max_length=22)

    @field_validator("p256dh")
    @classmethod
    def check_key(cls, value: str) -> str:
        receiver_key(value)
        return value

    @field_validator("auth")
    @classmethod
    def check_auth(cls, value: str) -> str:
        if len(decode(value)) != 16:
            raise ValueError("Invalid subscription auth secret")
        return value


@router.get("")
def inbox(request: Request, identity: VerifiedIdentity, before: UUID | None = None):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        params = {"account": account.id, "before": before}
        cursor = (
            """
          AND (created_at, id) < (
            SELECT created_at, id FROM cashly.notifications
            WHERE account_id = :account AND id = :before
          )
        """
            if before
            else ""
        )
        result = (
            session.execute(
                text(f"""
          WITH page AS (
            SELECT id, created_at, read_at FROM cashly.notifications
            WHERE account_id = :account AND cleared_at IS NULL {cursor}
            ORDER BY created_at DESC, id DESC LIMIT 31
          ), totals AS (
            SELECT count(*) AS unread_count FROM cashly.notifications
            WHERE account_id = :account AND read_at IS NULL AND cleared_at IS NULL
          )
          SELECT page.id, page.created_at, page.read_at, totals.unread_count
          FROM totals LEFT JOIN page ON true
          ORDER BY page.created_at DESC, page.id DESC
        """),
                params,
            )
            .mappings()
            .all()
        )
        rows = [row for row in result if row["id"] is not None]
        return {
            "data": {
                "items": [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "read_at": row["read_at"],
                        "title": TITLE,
                        "body": BODY,
                    }
                    for row in rows[:30]
                ],
                "next_cursor": rows[29]["id"] if len(rows) > 30 else None,
                "unread_count": result[0]["unread_count"],
            }
        }


@router.delete("")
def clear_inbox(request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        result = session.execute(
            text("""
              UPDATE cashly.notifications SET cleared_at = now()
              WHERE account_id = :account AND cleared_at IS NULL
            """),
            {"account": account.id},
        )
        return {"data": {"cleared": result.rowcount}}


@router.post("/read-all")
def read_all(request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        session.execute(
            text("""
          UPDATE cashly.notifications SET read_at = now()
          WHERE account_id = :account AND read_at IS NULL AND cleared_at IS NULL
        """),
            {"account": account.id},
        )
    return {"data": {"read": True}}


@router.post("/{notification_id:uuid}/read")
def read_one(notification_id: UUID, request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        found = session.scalar(
            text("""
          UPDATE cashly.notifications SET read_at = coalesce(read_at, now())
          WHERE account_id = :account AND id = :id RETURNING id
        """),
            {"account": account.id, "id": notification_id},
        )
        if found is None:
            raise ApiError(404, "NOTIFICATION_MISSING", "Notifikasi ini sudah tidak tersedia.")
    return {"data": {"read": True}}


def preference_response(
    request: Request, enabled: bool, reminder_time: time, push_active: bool = False
):
    key = request.app.state.web_push_key
    settings = request.app.state.settings
    production_times = settings.production_reminder_times if settings.environment == "prod" else ()
    return {
        "data": {
            "daily_enabled": enabled,
            "reminder_time": (production_times[0] if production_times else reminder_time).strftime(
                "%H:%M"
            ),
            "production_reminder_times": (
                [slot.strftime("%H:%M") for slot in production_times] if production_times else None
            ),
            "push_public_key": encode(public_bytes(key)) if key else None,
            "push_active": push_active,
            "test_push_available": key is not None and settings.environment == "staging",
        }
    }


def read_preferences(request: Request, identity, endpoint: str | None = None):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        row = session.execute(
            text("""
          SELECT coalesce(p.daily_enabled, false), coalesce(p.reminder_time, time '20:00'),
                 EXISTS (
                   SELECT 1 FROM cashly.push_subscriptions s
                   WHERE s.account_id = :account AND s.endpoint = :endpoint
                 )
          FROM (SELECT 1) AS defaults
          LEFT JOIN cashly.notification_preferences p ON p.account_id = :account
        """),
            {"account": account.id, "endpoint": endpoint},
        ).one()
        return preference_response(request, *row)


@router.get("/preferences")
def preferences(request: Request, identity: VerifiedIdentity):
    return read_preferences(request, identity)


@router.post("/preferences/device")
def device_preferences(body: EndpointInput, request: Request, identity: VerifiedIdentity):
    return read_preferences(request, identity, body.endpoint)


def set_reminder(
    session, account_id, enabled, reminder_time, *, production, staging_start_day=None
):
    session.execute(
        text("""
          INSERT INTO cashly.notification_preferences AS current
            (account_id, daily_enabled, reminder_time, staging_start_day, production_enabled_at)
          VALUES (:account, :enabled, :time, :staging_start_day,
                  CASE WHEN :production AND :enabled THEN now() END)
          ON CONFLICT (account_id) DO UPDATE
          SET daily_enabled = excluded.daily_enabled, reminder_time = excluded.reminder_time,
              staging_start_day = CASE
                WHEN current.reminder_time <> excluded.reminder_time
                  OR (excluded.daily_enabled AND NOT current.daily_enabled)
                THEN excluded.staging_start_day
                ELSE current.staging_start_day
              END,
              production_enabled_at = CASE
                WHEN NOT current.daily_enabled AND excluded.production_enabled_at IS NOT NULL
                THEN excluded.production_enabled_at
                ELSE current.production_enabled_at
              END
        """),
        {
            "account": account_id,
            "enabled": enabled,
            "time": reminder_time,
            "staging_start_day": staging_start_day,
            "production": production,
        },
    )


@router.patch("/preferences")
def save_preferences(body: ReminderInput, request: Request, identity: VerifiedIdentity):
    settings = request.app.state.settings
    production = settings.environment == "prod"
    if production:
        reminder_time = settings.production_reminder_times[0]
    elif body.reminder_time is None:
        raise ApiError(422, "REMINDER_TIME_REQUIRED", "Pilih jam pengingat dulu.")
    else:
        reminder_time = time.fromisoformat(body.reminder_time)
    staging_start_day = None
    if settings.environment == "staging":
        local_now = datetime.now(WIB)
        staging_start_day = local_now.date()
        if reminder_time <= local_now.time():
            staging_start_day += timedelta(days=1)
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        set_reminder(
            session,
            account.id,
            body.daily_enabled,
            reminder_time,
            production=production,
            staging_start_day=staging_start_day,
        )
    return preference_response(request, body.daily_enabled, reminder_time)


@router.post("/push/status")
def push_status(body: EndpointInput, request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        found = session.scalar(
            text("""
          SELECT id FROM cashly.push_subscriptions WHERE account_id = :account AND endpoint = :url
        """),
            {"account": account.id, "url": body.endpoint},
        )
    return {"data": {"active": found is not None}}


@router.post("/push")
def register_push(body: SubscriptionInput, request: Request, identity: VerifiedIdentity):
    if request.app.state.web_push_key is None:
        raise ApiError(503, "PUSH_UNAVAILABLE", "Notifikasi perangkat belum tersedia saat ini.")
    settings = request.app.state.settings
    production = settings.environment == "prod"
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        params = {
            "account": account.id,
            "url": body.endpoint,
            "key": body.p256dh,
            "auth": body.auth,
        }
        inserted = session.scalar(
            text("""
          INSERT INTO cashly.push_subscriptions (account_id, endpoint, public_key, auth_secret)
          VALUES (:account, :url, :key, :auth)
          ON CONFLICT (endpoint) DO NOTHING RETURNING id
        """),
            params,
        )
        if inserted is None:
            updated = session.scalar(
                text("""
              UPDATE cashly.push_subscriptions SET public_key = :key, auth_secret = :auth
              WHERE account_id = :account AND endpoint = :url RETURNING id
            """),
                params,
            )
            if updated is None:
                raise ApiError(
                    409, "PUSH_ALREADY_REGISTERED", "Aktifkan ulang notifikasi di perangkat ini."
                )
        if production:
            set_reminder(
                session, account.id, True, settings.production_reminder_times[0], production=True
            )
    return {"data": {"active": True, "daily_enabled": True} if production else {"active": True}}


@router.delete("/push")
def remove_push(body: EndpointInput, request: Request, identity: VerifiedIdentity):
    with database(request).transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        session.execute(
            text("""
          DELETE FROM cashly.push_subscriptions WHERE account_id = :account AND endpoint = :url
        """),
            {"account": account.id, "url": body.endpoint},
        )
    return {"data": {"active": False}}
