"""Store production reminder slots alongside staging and legacy history."""

from alembic import op

revision = "0013_production_reminder_slots"
down_revision = "0012_staging_reminder_slots"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE cashly.notification_preferences ADD COLUMN production_enabled_at timestamptz;
    ALTER TABLE cashly.notifications ADD COLUMN production_reminder_time time;
    ALTER TABLE cashly.notifications ADD CONSTRAINT notifications_single_environment_slot
      CHECK (staging_reminder_time IS NULL OR production_reminder_time IS NULL);
    CREATE UNIQUE INDEX uq_notifications_production_slot
      ON cashly.notifications (account_id, local_day, production_reminder_time)
      WHERE production_reminder_time IS NOT NULL;
    DROP INDEX cashly.uq_notifications_daily;
    CREATE UNIQUE INDEX uq_notifications_daily ON cashly.notifications (account_id, local_day)
      WHERE staging_reminder_time IS NULL AND production_reminder_time IS NULL;
    """)


def downgrade():
    # Preserve inbox history. If production has multiple slots in one day, this fails safely.
    op.execute("""
    DROP INDEX cashly.uq_notifications_daily;
    CREATE UNIQUE INDEX uq_notifications_daily ON cashly.notifications (account_id, local_day)
      WHERE staging_reminder_time IS NULL;
    DROP INDEX cashly.uq_notifications_production_slot;
    ALTER TABLE cashly.notifications DROP CONSTRAINT notifications_single_environment_slot;
    ALTER TABLE cashly.notifications DROP COLUMN production_reminder_time;
    ALTER TABLE cashly.notification_preferences DROP COLUMN production_enabled_at;
    """)
