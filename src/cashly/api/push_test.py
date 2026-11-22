import asyncio
import hashlib
import logging
from uuid import UUID, uuid4

import httpx2
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from cashly.api.accounts import VerifiedIdentity, database
from cashly.api.notifications import EndpointInput
from cashly.core.auth import Identity
from cashly.core.errors import ApiError
from cashly.db.connection import Database
from cashly.services import web_push
from cashly.services.accounts import load_account

router = APIRouter(prefix="/api/v1/notifications", tags=["Staging notifications"])
logger = logging.getLogger(__name__)
DELAY_SECONDS = 8


def load_subscription(db: Database, identity: Identity, subscription_id: UUID):
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        return (
            session.execute(
                text("""
              SELECT endpoint, public_key, auth_secret FROM cashly.push_subscriptions
              WHERE account_id = :account AND id = :subscription
            """),
                {"account": account.id, "subscription": subscription_id},
            )
            .mappings()
            .one_or_none()
        )


async def deliver_test(db: Database, identity: Identity, subscription_id: UUID, key, subject: str):
    await asyncio.sleep(DELAY_SECONDS)
    try:
        subscription = await run_in_threadpool(load_subscription, db, identity, subscription_id)
        if subscription is None:
            logger.info("Staging push test skipped (device no longer registered)")
            return
        async with httpx2.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            payload = dict(subscription, notification_id=uuid4())
            status = await asyncio.wait_for(
                web_push.send(client, payload, key, subject, test=True), 10
            )
        if 200 <= status < 300:
            logger.info("Staging push test accepted (status=%s)", status)
        else:
            logger.warning("Staging push test rejected (status=%s)", status)
    except (httpx2.HTTPError, TimeoutError, ValueError, SQLAlchemyError, HTTPException) as error:
        logger.warning("Staging push test failed (%s)", type(error).__name__)


@router.post("/push/test", status_code=202)
def send_test(
    body: EndpointInput, request: Request, identity: VerifiedIdentity, background: BackgroundTasks
):
    key = request.app.state.web_push_key
    if key is None:
        raise ApiError(503, "PUSH_UNAVAILABLE", "Tes notifikasi belum tersedia di staging.")
    db = database(request)
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, _ = load_account(session, identity)
        subscription = session.scalar(
            text("""
              SELECT id FROM cashly.push_subscriptions
              WHERE account_id = :account AND endpoint = :url
            """),
            {"account": account.id, "url": body.endpoint},
        )
        if subscription is None:
            raise ApiError(404, "PUSH_DEVICE_MISSING", "Aktifkan notifikasi di perangkat ini dulu.")
        # Keep one reservation per account and update it atomically across backend processes.
        reserved = session.scalar(
            text("""
              INSERT INTO cashly.idempotency_records
                (id, account_id, operation, key, request_fingerprint,
                 response_status, response_body)
              VALUES (gen_random_uuid(), :account, 'notification-push-test', 'test-device',
                      :fingerprint, 202, '{}'::jsonb)
              ON CONFLICT (account_id, operation, key) DO UPDATE
                SET created_at = now(), request_fingerprint = excluded.request_fingerprint
              WHERE idempotency_records.created_at <= now() - interval '30 seconds'
              RETURNING id
            """),
            {
                "account": account.id,
                "fingerprint": hashlib.sha256(body.endpoint.encode()).hexdigest(),
            },
        )
        if reserved is None:
            raise ApiError(429, "PUSH_TEST_LIMIT", "Tunggu 30 detik sebelum kirim tes lagi.")
    background.add_task(
        deliver_test,
        db,
        identity,
        subscription,
        key,
        request.app.state.settings.web_push_subject,
    )
    return {"data": {"delay_seconds": DELAY_SECONDS}}
