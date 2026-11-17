"""Reset this week's review for the seeded local dev account only."""

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from cashly.core.environment import PROJECT_ROOT, ConfigurationError, resolve_environment
from cashly.core.settings import Settings
from cashly.db.connection import build_engine
from cashly.db.local_data import verify_local_target


def reset_weekly(settings, email):
    verify_local_target(settings)
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
                raise ConfigurationError("Only the seeded development account can be reset.")
            now = datetime.now(ZoneInfo(row.timezone))
            start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            params = {
                "account": row.id,
                "key": f"weekly:{start.date()}",
                "start": start,
                "end": start + timedelta(days=7),
            }
            removed = conn.execute(
                text(
                    "DELETE FROM cashly.game_events WHERE account_id=:account "
                    "AND kind='weekly_review' AND event_key=:key"
                ),
                params,
            ).rowcount
            conn.execute(
                text(
                    "DELETE FROM cashly.idempotency_records WHERE account_id=:account "
                    "AND operation='game:weekly' AND created_at>=:start AND created_at<:end"
                ),
                params,
            )
        return {"weekly_reviews_reset": removed, "week_start": str(start.date())}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["reset-current-week"], required=True)
    args = parser.parse_args()
    del args
    settings = Settings.from_selection(resolve_environment(override="dev"))
    verify_local_target(settings)
    # Only email is used. Never print or forward the password.
    credentials = json.loads(
        (PROJECT_ROOT / ".cashly-dev-account.json").read_text(encoding="utf-8")
    )
    print(json.dumps(reset_weekly(settings, credentials["email"])))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("[Cashly] Weekly reset failed; verify local configuration and seeded account.")
        raise SystemExit(1) from None
