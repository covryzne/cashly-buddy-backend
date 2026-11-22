"""Support clearing the inbox and rename the food-and-drink category."""

from alembic import op

revision = "0014_inbox_clear_food_label"
down_revision = "0013_production_reminder_slots"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE cashly.notifications ADD COLUMN cleared_at timestamptz;
    DROP INDEX cashly.ix_notifications_inbox;
    DROP INDEX cashly.ix_notifications_unread;
    CREATE INDEX ix_notifications_inbox
      ON cashly.notifications (account_id, created_at DESC, id DESC) WHERE cleared_at IS NULL;
    CREATE INDEX ix_notifications_unread ON cashly.notifications (account_id)
      WHERE cleared_at IS NULL AND read_at IS NULL;
    UPDATE cashly.expense_categories SET label_id = 'Makanan dan Minuman'
      WHERE code = 'food_drink';
    """)


def downgrade():
    op.execute("""
    UPDATE cashly.expense_categories SET label_id = 'Makanan & Minuman'
      WHERE code = 'food_drink';
    DROP INDEX cashly.ix_notifications_inbox;
    DROP INDEX cashly.ix_notifications_unread;
    CREATE INDEX ix_notifications_inbox
      ON cashly.notifications (account_id, created_at DESC, id DESC);
    CREATE INDEX ix_notifications_unread ON cashly.notifications (account_id) WHERE read_at IS NULL;
    ALTER TABLE cashly.notifications DROP COLUMN cleared_at;
    """)
