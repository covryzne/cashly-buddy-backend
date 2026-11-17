"""Event-backed rules approved for the initial development flow (ADR-0073)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashly.core.errors import ApiError
from cashly.core.settings import Settings
from cashly.db.connection import database_url
from cashly.db.models import Expense, GameEvent, GameState


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


def overview(session, account_id, timezone, day=None, settings=None):
    day = day or today(timezone)
    ledger = events(session, account_id)
    active = {e.local_date for e in ledger if e.kind == "checkin"}
    protected = {e.local_date for e in ledger if e.kind == "freeze_spent"}
    current, longest = streaks(active, protected, day)
    state = session.get(GameState, account_id)
    stage = state.max_stage if state else 0
    xp = sum(e.xp for e in ledger)
    balance = sum(e.freeze_delta for e in ledger)
    yesterday = day - timedelta(days=1)
    recoverable = (
        yesterday not in active | protected and yesterday - timedelta(days=1) in active | protected
    )
    week = day - timedelta(days=day.weekday())
    weekend = day.weekday() >= 5
    test_override = weekly_test_override(settings) and not weekend
    return {
        "level": 1 + xp // 100,
        "total_xp": xp,
        "current_streak": current,
        "longest_streak": longest,
        "freeze_balance": balance,
        "max_mascot_stage_unlocked": stage,
        "today_checked_in": day in active,
        "today_local_date": day.isoformat(),
        "timezone": timezone,
        "recovery": {"protected_local_date": yesterday.isoformat()} if recoverable else None,
        "calendar": [
            {
                "date": (week + timedelta(days=n)).isoformat(),
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
            {"code": "daily", "title": "Check-in hari ini", "completed": day in active, "xp": 10},
            {
                "code": "weekly",
                "title": "Review cerita mingguan",
                "completed": any(e.event_key == f"weekly:{week}" for e in ledger),
                "xp": 40,
                "can_complete": weekend or test_override,
                "available_from": (week + timedelta(days=5)).isoformat(),
                "test_override": test_override,
            },
        ],
    }


def checkin(session, account_id, timezone, day=None):
    day = day or today(timezone)
    if record(session, account_id, timezone, f"daily:{day}", "checkin", day, xp=10):
        ledger = events(session, account_id)
        active = {e.local_date for e in ledger if e.kind == "checkin"}
        if len(active) % 7 == 0:
            balance = sum(e.freeze_delta for e in ledger)
            record(
                session,
                account_id,
                timezone,
                f"freeze-grant:{len(active) // 7}",
                "freeze_grant",
                day,
                freeze=int(balance < 2),
            )
        protected = {e.local_date for e in ledger if e.kind == "freeze_spent"}
        _, longest = streaks(active, protected, day)
        unlocked = sum(longest >= milestone for milestone in (10, 30, 100, 150, 200))
        state = session.get(GameState, account_id)
        if state is None:
            session.add(GameState(account_id=account_id, max_stage=unlocked))
        else:
            state.max_stage = max(state.max_stage, unlocked)
        session.flush()


def on_expense(session, account_id, timezone, occurred_at):
    day = today(timezone)
    record(session, account_id, timezone, "first-expense", "first_expense", day, xp=50)
    if occurred_at.astimezone(ZoneInfo(timezone)).date() == day:
        checkin(session, account_id, timezone, day)


def action(session, account_id, timezone, kind, protected_date=None, settings=None):
    day = today(timezone)
    if kind == "freeze":
        current = overview(session, account_id, timezone, day, settings)
        if any(
            e.kind == "freeze_spent" and e.local_date == protected_date
            for e in events(session, account_id)
        ):
            return current
        if not current["recovery"] or protected_date != day - timedelta(days=1):
            raise ApiError(409, "STREAK_FREEZE_NOT_ELIGIBLE", "Tanggal tidak dapat dipulihkan.")
        if current["freeze_balance"] < 1:
            raise ApiError(409, "STREAK_FREEZE_UNAVAILABLE", "Stok Freeze habis.")
        record(
            session,
            account_id,
            timezone,
            f"freeze:{protected_date}",
            "freeze_spent",
            protected_date,
            freeze=-1,
        )
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
            record(session, account_id, timezone, f"weekly:{week}", "weekly_review", day, xp=40)
        checkin(session, account_id, timezone)
    session.flush()
    return overview(session, account_id, timezone, settings=settings)
