"""Allow staging reminders to use one slot per account, day, and selected time."""

from alembic import op

revision = "0012_staging_reminder_slots"
down_revision = "0011_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE cashly.notification_preferences ADD COLUMN staging_start_day date;
    ALTER TABLE cashly.notifications ADD COLUMN staging_reminder_time time;
    CREATE UNIQUE INDEX uq_notifications_daily ON cashly.notifications (account_id, local_day)
      WHERE staging_reminder_time IS NULL;
    CREATE UNIQUE INDEX uq_notifications_staging_slot
      ON cashly.notifications (account_id, local_day, staging_reminder_time)
      WHERE staging_reminder_time IS NOT NULL;
    ALTER TABLE cashly.notifications DROP CONSTRAINT notifications_account_id_local_day_key;
    """)


def downgrade():
    # Preserve inbox history. PostgreSQL refuses this downgrade if a day has multiple entries.
    op.execute("""
    ALTER TABLE cashly.notifications ADD CONSTRAINT notifications_account_id_local_day_key
      UNIQUE (account_id, local_day);
    DROP INDEX cashly.uq_notifications_staging_slot;
    DROP INDEX cashly.uq_notifications_daily;
    ALTER TABLE cashly.notifications DROP COLUMN staging_reminder_time;
    ALTER TABLE cashly.notification_preferences DROP COLUMN staging_start_day;
    """)
