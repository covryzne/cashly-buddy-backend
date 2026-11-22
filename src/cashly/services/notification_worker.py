import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta, timezone

import httpx2
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from cashly.db.connection import Database
from cashly.services import web_push

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))


@contextmanager
def worker_transaction(database: Database) -> Iterator[Session]:
    with database.transaction() as session:
        session.execute(text("SELECT set_config('cashly.notification_worker', 'on', true)"))
        yield session


def create_reminders(
    database: Database,
    now: datetime,
    push_available: bool,
    *,
    staging: bool = False,
    production_times: tuple[time, time, time] | None = None,
) -> tuple[int, int]:
    local = now.astimezone(WIB)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest = max(start, local - timedelta(minutes=30))
    with worker_transaction(database) as session:
        counts = session.execute(
            text("""
          WITH due AS (
            SELECT p.account_id,
                   CASE WHEN :staging THEN slots.reminder_at END AS staging_time,
                   CASE WHEN :production THEN slots.reminder_at END AS production_time
            FROM cashly.notification_preferences p
            JOIN cashly.accounts a ON a.id = p.account_id AND a.status = 'active'
            CROSS JOIN LATERAL (
              SELECT p.reminder_time AS reminder_at WHERE NOT :production
              UNION ALL
              SELECT configured.reminder_at
              FROM unnest(CAST(:production_times AS time[])) AS configured(reminder_at)
            ) AS slots
            CROSS JOIN LATERAL (
              SELECT CAST(:start AS timestamptz)
                + (slots.reminder_at - time '00:00') AS scheduled_at
            ) AS slot
            WHERE p.daily_enabled AND slots.reminder_at BETWEEN :earliest AND :current_time
              AND (NOT :staging OR p.staging_start_day IS NULL OR p.staging_start_day <= :day)
              AND (NOT :production OR p.production_enabled_at IS NULL
                   OR p.production_enabled_at <= slot.scheduled_at)
              AND NOT EXISTS (
                SELECT 1 FROM cashly.notifications n
                WHERE n.account_id = p.account_id AND n.local_day = :day
                  AND (
                    (NOT :staging AND NOT :production)
                    OR (:staging AND n.staging_reminder_time = slots.reminder_at)
                    OR (:production AND n.production_reminder_time = slots.reminder_at)
                    OR (
                      n.staging_reminder_time IS NULL AND n.production_reminder_time IS NULL
                      AND n.created_at >= slot.scheduled_at
                      AND NOT EXISTS (
                        SELECT 1
                        FROM unnest(CAST(:production_times AS time[])) AS later(reminder_at)
                        WHERE later.reminder_at > slots.reminder_at
                          AND n.created_at >= CAST(:start AS timestamptz)
                            + (later.reminder_at - time '00:00')
                      )
                    )
                  )
              )
              AND (:staging OR :production OR (
                NOT EXISTS (
                  SELECT 1 FROM cashly.expenses e
                  WHERE e.account_id = p.account_id AND e.deleted_at IS NULL
                    AND e.occurred_at >= :start AND e.occurred_at < :end
                )
                AND NOT EXISTS (
                  SELECT 1 FROM cashly.game_events g
                  WHERE g.account_id = p.account_id AND g.local_date = :day AND g.kind = 'checkin'
                )
              ))
          ), new_notifications AS (
            INSERT INTO cashly.notifications
              (account_id, local_day, created_at, staging_reminder_time, production_reminder_time)
              SELECT account_id, :day, :now, staging_time, production_time FROM due
              ON CONFLICT DO NOTHING
              RETURNING id, account_id
          ), queued_pushes AS (
            INSERT INTO cashly.push_deliveries (notification_id, subscription_id, available_at)
              SELECT n.id, s.id, :now
              FROM new_notifications n
              JOIN cashly.push_subscriptions s ON s.account_id = n.account_id
              WHERE :push_available
              RETURNING 1
          )
          SELECT (SELECT count(*) FROM new_notifications), (SELECT count(*) FROM queued_pushes)
        """),
            {
                "earliest": earliest.time(),
                "current_time": local.time(),
                "day": local.date(),
                "start": start.astimezone(UTC),
                "end": (start + timedelta(days=1)).astimezone(UTC),
                "now": now,
                "push_available": push_available,
                "staging": staging,
                "production": production_times is not None,
                "production_times": list(production_times or ()),
            },
        ).one()
        session.execute(
            text("""
          DELETE FROM cashly.push_deliveries d USING cashly.notifications n
          WHERE n.id = d.notification_id AND n.created_at < :expires
        """),
            {"expires": now - timedelta(minutes=15)},
        )
        return counts[0], counts[1]


def claim_delivery(database: Database):
    with worker_transaction(database) as session:
        return (
            session.execute(
                text("""
          WITH candidate AS (
            SELECT d.notification_id, d.subscription_id, s.endpoint, s.public_key, s.auth_secret
            FROM cashly.push_deliveries d
            JOIN cashly.push_subscriptions s ON s.id = d.subscription_id
            JOIN cashly.notifications n ON n.id = d.notification_id
            JOIN cashly.accounts a ON a.id = n.account_id AND a.status = 'active'
            JOIN cashly.notification_preferences p ON p.account_id = n.account_id
            WHERE d.available_at <= now() AND d.attempts < 3 AND p.daily_enabled
              AND n.cleared_at IS NULL
              AND (n.read_at IS NULL OR n.production_reminder_time IS NOT NULL)
              AND (n.staging_reminder_time IS NULL OR n.staging_reminder_time = p.reminder_time)
            ORDER BY d.available_at
            LIMIT 1 FOR UPDATE OF d SKIP LOCKED
          )
          UPDATE cashly.push_deliveries d
          SET attempts = d.attempts + 1, available_at = now() + interval '1 minute'
          FROM candidate c
          WHERE d.notification_id = c.notification_id AND d.subscription_id = c.subscription_id
          RETURNING d.notification_id, d.subscription_id, d.attempts,
                    c.endpoint, c.public_key, c.auth_secret
        """)
            )
            .mappings()
            .one_or_none()
        )


def finish_delivery(database: Database, row, status: int | None) -> None:
    with worker_transaction(database) as session:
        if status in {404, 410}:
            session.execute(
                text("""
              DELETE FROM cashly.push_subscriptions WHERE id = :subscription_id
            """),
                row,
            )
        elif row["attempts"] >= 3 or (status is not None and status < 500 and status != 429):
            session.execute(
                text("""
              DELETE FROM cashly.push_deliveries
              WHERE notification_id = :notification_id AND subscription_id = :subscription_id
            """),
                row,
            )


async def run_notification_worker(
    database: Database,
    key,
    subject: str,
    *,
    staging: bool = False,
    production_times: tuple[time, time, time] | None = None,
) -> None:
    logger.info(
        "Notification worker started (push=%s, staging=%s, production_times=%s)",
        "enabled" if key else "disabled",
        staging,
        ",".join(slot.strftime("%H:%M") for slot in production_times or ()) or "-",
    )
    async with httpx2.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                created, queued = await run_in_threadpool(
                    create_reminders,
                    database,
                    datetime.now(UTC),
                    key is not None,
                    staging=staging,
                    production_times=production_times,
                )
                if created:
                    logger.info(
                        "Daily reminders created (inbox=%s, push_queued=%s)", created, queued
                    )
                if key is not None:
                    for _ in range(25):
                        row = await run_in_threadpool(claim_delivery, database)
                        if row is None:
                            break
                        try:
                            status = await asyncio.wait_for(
                                web_push.send(client, row, key, subject), 10
                            )
                        except (httpx2.HTTPError, TimeoutError, ValueError):
                            status = None
                        if status is None or not 200 <= status < 300:
                            logger.warning("Web Push delivery attempt failed (status=%s)", status)
                        else:
                            logger.info("Web Push accepted (status=%s)", status)
                        await run_in_threadpool(finish_delivery, database, row, status)
            except SQLAlchemyError as error:
                state = getattr(getattr(error, "orig", None), "sqlstate", None)
                logger.error(
                    "Notification worker DB error (%s, sqlstate=%s)", type(error).__name__, state
                )
            except Exception as error:
                logger.error("Notification worker tick failed (%s)", type(error).__name__)
            await asyncio.sleep(20)
