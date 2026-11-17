"""Explicit reset of today's check-in for the seeded local development account."""

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from cashly.core.environment import PROJECT_ROOT, ConfigurationError, resolve_environment
from cashly.core.settings import Settings
from cashly.db.connection import build_engine
from cashly.db.local_data import verify_local_target


def reset_daily(settings, email):
    verify_local_target(settings)
    if not settings.daily_checkin_test_mode:
        raise ConfigurationError("Set DAILY_CHECKIN_TEST_MODE=true for this explicit local reset.")
    engine = build_engine(settings, migration=True)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT a.id,p.timezone FROM cashly.accounts a "
                    "JOIN cashly.profiles p ON p.account_id=a.id "
                    "JOIN cashly.account_auth_links l ON l.account_id=a.id "
                    "JOIN auth.users u ON u.id=l.auth_user_id WHERE lower(u.email)=:email "
                    "AND EXISTS (SELECT 1 FROM cashly.idempotency_records i WHERE "
                    "i.account_id=a.id AND i.operation='seed-development' AND i.key='scenario-v1') "
                    "FOR UPDATE OF a"
                ),
                {"email": email.lower()},
            ).first()
            if not row:
                raise ConfigurationError("Only the seeded local development account can be reset.")
            now = datetime.now(ZoneInfo(row.timezone))
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            params = {
                "account": row.id,
                "day": now.date(),
                "start": start,
                "end": start + timedelta(days=1),
            }
            # Roll back a grant made by today's active-day milestone too, so
            # repeated resets/check-ins cannot duplicate that milestone grant.
            grant = conn.scalar(
                text(
                    "SELECT coalesce(sum(freeze_delta),0) FROM cashly.game_events "
                    "WHERE account_id=:account AND local_date=:day AND kind='freeze_grant'"
                ),
                params,
            )
            balance = conn.scalar(
                text(
                    "SELECT coalesce(sum(freeze_delta),0) FROM cashly.game_events "
                    "WHERE account_id=:account"
                ),
                params,
            )
            if balance < grant:
                raise ConfigurationError("Today's Freeze grant has been consumed; reset refused.")
            removed = conn.execute(
                text(
                    "DELETE FROM cashly.game_events WHERE account_id=:account "
                    "AND local_date=:day AND kind='checkin'"
                ),
                params,
            ).rowcount
            conn.execute(
                text(
                    "DELETE FROM cashly.game_events WHERE account_id=:account "
                    "AND local_date=:day AND kind='freeze_grant'"
                ),
                params,
            )
            conn.execute(
                text(
                    "DELETE FROM cashly.idempotency_records WHERE account_id=:account "
                    "AND operation IN ('game:review','game:no-spend') "
                    "AND created_at>=:start AND created_at<:end"
                ),
                params,
            )
        return {
            "daily_checkins_reset": removed,
            "local_date": str(now.date()),
            "xp_removed": removed * 10,
            "freeze_grants_reversed": grant,
        }
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["reset-today"], required=True)
    parser.parse_args()
    settings = Settings.from_selection(resolve_environment(override="dev"))
    verify_local_target(settings)
    credentials = json.loads(
        (PROJECT_ROOT / ".cashly-dev-account.json").read_text(encoding="utf-8")
    )
    print(json.dumps(reset_daily(settings, credentials["email"])))


if __name__ == "__main__":
    try:
        main()
    except ConfigurationError as error:
        print(f"[Cashly] {error}")
        raise SystemExit(1) from None
    except Exception:
        print("[Cashly] Daily reset failed; connection details omitted.")
        raise SystemExit(1) from None
