from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_auth import auth_postgres_settings as auth_postgres_settings
from test_auth import prepare, runtime_database

from cashly.core.auth import Identity
from cashly.core.errors import ApiError
from cashly.db.models import GameEvent, StreakProtection
from cashly.services import finance, gamification
from cashly.services.accounts import bootstrap


@pytest.mark.parametrize("missed", [1, 4, 5])
def test_recovery_window_level_and_one_checkin(auth_postgres_settings, missed):
    identity = Identity(uuid4(), "streak@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    db = runtime_database(auth_postgres_settings)
    day = date(2026, 12, 20)
    try:
        account = bootstrap(db, auth_postgres_settings, identity)
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            initial = gamification.overview(session, account.account_id, "Asia/Jakarta", day)
            assert initial["hero_level"] == 1 and initial["protection_remaining"] == 5
            assert "total_xp" not in initial and "level" not in initial
            for offset in range(30):
                gamification.checkin(
                    session,
                    account.account_id,
                    "Asia/Jakarta",
                    day - timedelta(days=missed + 1 + offset),
                )
            before = gamification.overview(session, account.account_id, "Asia/Jakarta", day)
            assert before["longest_streak"] == 30
            if missed == 5:
                assert before["recovery"] is None
                assert before["current_streak"] == 0 and before["hero_level"] == 1
                return
            assert before["current_streak"] == 30 and before["hero_level"] == 2
            assert before["streak_status"] == "needs_recovery"
            dates = [
                date.fromisoformat(value) for value in before["recovery"]["protected_local_dates"]
            ]
            assert len(dates) == missed

        # Two tabs racing must protect/charge/check-in once, under the same account lock.
        # Use a single global clock patch outside threads to avoid monkeypatch races.
        from unittest.mock import patch

        with patch("cashly.services.gamification.today", return_value=day):

            def locked_restore(_):
                with db.transaction(auth_user_id=identity.user_id) as session:
                    finance.locked_account(session, identity)
                    return gamification.action(
                        session, account.account_id, "Asia/Jakarta", "freeze", dates
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(locked_restore, range(2)))
        for result in results:
            assert result["current_streak"] == 31
            assert result["protection_remaining"] == 5 - missed
            assert result["today_checked_in"] and result["recovery"] is None
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            assert session.scalar(select(func.count()).select_from(StreakProtection)) == missed
            assert session.scalar(select(func.sum(GameEvent.xp))) == 0
            january = gamification.overview(
                session, account.account_id, "Asia/Jakarta", date(2027, 1, 1)
            )
            assert january["protection_remaining"] == 5
            assert january["hero_level"] == 1
    finally:
        db.dispose()


def test_partial_and_insufficient_recovery_never_spend(auth_postgres_settings):
    from unittest.mock import patch

    identity = Identity(uuid4(), "quota@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    db = runtime_database(auth_postgres_settings)
    day = date(2026, 12, 20)
    try:
        account = bootstrap(db, auth_postgres_settings, identity)
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            used = {date(2026, 12, 2), date(2026, 12, 3)}
            for offset in range(32):
                value = day - timedelta(days=5 + offset)
                if value not in used:
                    gamification.checkin(session, account.account_id, "Asia/Jakarta", value)
            for value in used:
                session.add(
                    StreakProtection(
                        account_id=account.account_id,
                        protected_date=value,
                        quota_month=day.replace(day=1),
                    )
                )
            session.flush()
            before = gamification.overview(session, account.account_id, "Asia/Jakarta", day)
            assert before["protection_remaining"] == 3
            assert not before["recovery"]["can_restore"]
            dates = [date.fromisoformat(d) for d in before["recovery"]["protected_local_dates"]]
            with patch("cashly.services.gamification.today", return_value=day):
                with pytest.raises(ApiError):
                    gamification.action(
                        session, account.account_id, "Asia/Jakarta", "freeze", dates[:1]
                    )
                with pytest.raises(ApiError):
                    gamification.action(
                        session, account.account_id, "Asia/Jakarta", "freeze", dates
                    )
            assert session.scalar(select(func.count()).select_from(StreakProtection)) == 2
            assert not gamification.overview(session, account.account_id, "Asia/Jakarta", day)[
                "today_checked_in"
            ]
    finally:
        db.dispose()


@pytest.mark.parametrize(
    "streak,level", [(0, 1), (29, 1), (30, 2), (99, 2), (100, 3), (150, 4), (200, 5)]
)
def test_hero_milestones(streak, level):
    assert gamification.hero_level(streak) == level


def test_recovery_charges_restoration_month_across_year_boundary(auth_postgres_settings):
    from unittest.mock import patch

    identity = Identity(uuid4(), "month@example.invalid", "Tester", datetime.now(UTC))
    prepare(auth_postgres_settings, [identity])
    db = runtime_database(auth_postgres_settings)
    day = date(2027, 1, 2)
    try:
        account = bootstrap(db, auth_postgres_settings, identity)
        with db.transaction(auth_user_id=identity.user_id) as session:
            finance.locked_account(session, identity)
            for offset in range(30):
                gamification.checkin(
                    session, account.account_id, "Asia/Jakarta", day - timedelta(days=5 + offset)
                )
            before = gamification.overview(session, account.account_id, "Asia/Jakarta", day)
            dates = [date.fromisoformat(d) for d in before["recovery"]["protected_local_dates"]]
            with patch("cashly.services.gamification.today", return_value=day):
                result = gamification.action(
                    session, account.account_id, "Asia/Jakarta", "freeze", dates
                )
            assert result["current_streak"] == 31 and result["protection_remaining"] == 1
            assert {p.quota_month for p in session.scalars(select(StreakProtection))} == {
                date(2027, 1, 1)
            }
            assert (
                gamification.overview(
                    session, account.account_id, "Asia/Jakarta", date(2026, 12, 31)
                )["protection_remaining"]
                == 5
            )
    finally:
        db.dispose()
