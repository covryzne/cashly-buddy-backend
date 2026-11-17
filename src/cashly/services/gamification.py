"""Streak-only heroes and monthly manual protections (ADR-0083)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashly.core.errors import ApiError
from cashly.core.settings import Settings
from cashly.db.connection import database_url
from cashly.db.models import Expense, GameEvent, StreakProtection


def events(session, account_id):
    return list(session.scalars(select(GameEvent).where(GameEvent.account_id == account_id)))


def record(session: Session, account_id, timezone, key, kind, day, xp=0, freeze=0):
    exists = session.scalar(
        select(GameEvent.id).where(GameEvent.account_id == account_id, GameEvent.event_key == key)
    )
    if exists:
        return False
    session.add(
        GameEvent(
            account_id=account_id,
            event_key=key,
            kind=kind,
            local_date=day,
            timezone=timezone,
            xp=xp,
            freeze_delta=freeze,
        )
    )
    session.flush()
    return True


def today(timezone):
    return datetime.now(ZoneInfo(timezone)).date()


def streaks(active: set[date], protected: set[date], day: date):
    longest = run = 0
    previous = None
    for value in sorted(active | protected):
        if previous is None or value != previous + timedelta(days=1):
            run = 0
        run += int(value in active)
        longest = max(longest, run)
        previous = value
    cursor = day if day in active | protected else day - timedelta(days=1)
    current = 0
    while cursor in active | protected:
        current += int(cursor in active)
        cursor -= timedelta(days=1)
    return current, longest


def weekly_test_override(settings: Settings | None) -> bool:
    return bool(
        settings
        and settings.environment == "dev"
        and settings.weekly_review_test_mode
        and settings.database_url
        and database_url(settings.database_url).host in {"127.0.0.1", "localhost", "::1"}
        and not any(
            name in database_url(settings.database_url).query
            for name in ("host", "hostaddr", "service")
        )
    )


def recovery_dates(active, protected, day):
    covered = active | protected
    cursor = day - timedelta(days=1)
    earliest = day - timedelta(days=4)
    while cursor in covered and cursor >= earliest:
        cursor -= timedelta(days=1)
    if cursor < earliest:
        return []
    missing = []
    while cursor not in covered and cursor >= earliest:
        missing.append(cursor)
        cursor -= timedelta(days=1)
    if cursor not in covered or not streaks(active, protected, cursor)[0]:
        return []
    return sorted(missing)


def hero_level(streak):
    return 1 + sum(streak >= milestone for milestone in (30, 100, 150, 200))


def overview(session, account_id, timezone, day=None, settings=None):
    day = day or today(timezone)
    ledger = events(session, account_id)
    active = {e.local_date for e in ledger if e.kind == "checkin"}
    protections = list(
        session.scalars(select(StreakProtection).where(StreakProtection.account_id == account_id))
    )
    protected = {p.protected_date for p in protections}
    # Preserve historical protection dates from the old event ledger.
    protected |= {e.local_date for e in ledger if e.kind == "freeze_spent"}
    current, longest = streaks(active, protected, day)
    month = day.replace(day=1)
    available = max(0, 5 - sum(p.quota_month == month for p in protections))
    missing = recovery_dates(active, protected, day)
    recovery = None
    if missing:
        pending, _ = streaks(active, protected | set(missing), day)
        after, _ = streaks(active | {day}, protected | set(missing), day)
        current = pending
        recovery = {
            "protected_local_dates": [str(d) for d in missing],
            "required": len(missing),
            "can_restore": available >= len(missing),
            "streak_after_recovery": after,
        }
    week = day - timedelta(days=day.weekday())
    weekend = day.weekday() >= 5
    test_override = weekly_test_override(settings) and not weekend
    return {
        "hero_level": hero_level(current),
        "current_streak": current,
        "longest_streak": longest,
        "protection_remaining": available,
        "protection_month": str(month),
        "streak_status": "needs_recovery"
        if recovery
        else "active"
        if current or not active
        else "broken",
        "today_checked_in": day in active,
        "today_local_date": day.isoformat(),
        "timezone": timezone,
        "recovery": recovery,
        "calendar": [
            {
                "date": str(week + timedelta(days=n)),
                "status": "active"
                if week + timedelta(days=n) in active
                else "protected"
                if week + timedelta(days=n) in protected
                else "upcoming"
                if week + timedelta(days=n) > day
                else "missed",
            }
            for n in range(7)
        ],
        "missions": [
            {"code": "daily", "title": "Check-in hari ini", "completed": day in active},
            {
                "code": "weekly",
                "title": "Review cerita mingguan",
                "completed": any(e.event_key == f"weekly:{week}" for e in ledger),
                "can_complete": weekend or test_override,
                "available_from": str(week + timedelta(days=5)),
                "test_override": test_override,
            },
        ],
    }


def checkin(session, account_id, timezone, day=None):
    day = day or today(timezone)
    record(session, account_id, timezone, f"daily:{day}", "checkin", day)


def on_expense(session, account_id, timezone, occurred_at):
    day = today(timezone)
    if occurred_at.astimezone(ZoneInfo(timezone)).date() == day:
        checkin(session, account_id, timezone, day)


def action(session, account_id, timezone, kind, protected_date=None, settings=None):
    day = today(timezone)
    if kind == "freeze":
        requested = sorted(set(protected_date or []))
        existing = set(
            session.scalars(
                select(StreakProtection.protected_date).where(
                    StreakProtection.account_id == account_id
                )
            )
        )
        if requested and set(requested) <= existing:
            return overview(session, account_id, timezone, day, settings)
        current = overview(session, account_id, timezone, day, settings)
        recovery = current["recovery"]
        if not recovery or [str(d) for d in requested] != recovery["protected_local_dates"]:
            raise ApiError(
                409,
                "STREAK_PROTECTION_NOT_ELIGIBLE",
                "Tanggal pemulihan sudah berubah. Muat ulang data.",
            )
        if not recovery["can_restore"]:
            raise ApiError(
                409, "STREAK_PROTECTION_UNAVAILABLE", "Jatah proteksi bulan ini tidak cukup."
            )
        for value in requested:
            session.add(
                StreakProtection(
                    account_id=account_id, protected_date=value, quota_month=day.replace(day=1)
                )
            )
        session.flush()
        checkin(session, account_id, timezone, day)
    else:
        if kind == "no-spend":
            start = datetime.combine(day, datetime.min.time(), ZoneInfo(timezone)).astimezone(UTC)
            end = datetime.combine(
                day + timedelta(days=1), datetime.min.time(), ZoneInfo(timezone)
            ).astimezone(UTC)
            if session.scalar(
                select(Expense.id)
                .where(
                    Expense.account_id == account_id,
                    Expense.deleted_at.is_(None),
                    Expense.occurred_at >= start,
                    Expense.occurred_at < end,
                )
                .limit(1)
            ):
                raise ApiError(409, "NO_SPEND_CONFLICT", "Hari ini sudah ada pengeluaran.")
        if kind == "weekly":
            if day.weekday() < 5 and not weekly_test_override(settings):
                raise ApiError(
                    409, "WEEKLY_REVIEW_NOT_AVAILABLE", "Review tersedia pada Sabtu dan Minggu."
                )
            week = day - timedelta(days=day.weekday())
            start = datetime.combine(week, datetime.min.time(), ZoneInfo(timezone))
            if not session.scalar(
                select(Expense.id)
                .where(
                    Expense.account_id == account_id,
                    Expense.deleted_at.is_(None),
                    Expense.occurred_at >= start,
                    Expense.occurred_at <= datetime.now(UTC),
                )
                .limit(1)
            ):
                raise ApiError(409, "REVIEW_EMPTY", "Belum ada transaksi minggu ini.")
            record(session, account_id, timezone, f"weekly:{week}", "weekly_review", day)
        checkin(session, account_id, timezone)
    session.flush()
    return overview(session, account_id, timezone, settings=settings)
