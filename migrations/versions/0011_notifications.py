"""Add the notification inbox, reminder settings and Web Push delivery queue."""

from alembic import op

revision = "0011_notifications"
down_revision = "0010_whatsapp_redirect"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE cashly.notification_preferences (
      account_id uuid PRIMARY KEY REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      daily_enabled boolean NOT NULL,
      reminder_time time NOT NULL
    );
    CREATE INDEX ix_notification_reminder_time ON cashly.notification_preferences (reminder_time)
      WHERE daily_enabled;
    CREATE TABLE cashly.notifications (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      account_id uuid NOT NULL REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      local_day date NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      read_at timestamptz,
      UNIQUE (account_id, local_day)
    );
    CREATE INDEX ix_notifications_inbox ON cashly.notifications (account_id, created_at DESC, id DESC);
    CREATE INDEX ix_notifications_unread ON cashly.notifications (account_id) WHERE read_at IS NULL;
    CREATE TABLE cashly.push_subscriptions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      account_id uuid NOT NULL REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      endpoint text NOT NULL UNIQUE,
      public_key text NOT NULL,
      auth_secret text NOT NULL
    );
    CREATE INDEX ix_push_subscriptions_account ON cashly.push_subscriptions (account_id);
    CREATE TABLE cashly.push_deliveries (
      notification_id uuid REFERENCES cashly.notifications(id) ON DELETE CASCADE,
      subscription_id uuid REFERENCES cashly.push_subscriptions(id) ON DELETE CASCADE,
      available_at timestamptz NOT NULL DEFAULT now(),
      attempts smallint NOT NULL DEFAULT 0,
      PRIMARY KEY (notification_id, subscription_id)
    );
    CREATE INDEX ix_push_delivery_due ON cashly.push_deliveries (available_at);
    CREATE INDEX ix_push_delivery_subscription ON cashly.push_deliveries (subscription_id);
    """)
    worker = "current_setting('cashly.notification_worker', true) = 'on'"
    account = "account_id = nullif(current_setting('cashly.account_id', true), '')::uuid"
    for table in ("notification_preferences", "notifications", "push_subscriptions", "push_deliveries"):
        op.execute(f"ALTER TABLE cashly.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON cashly.{table} FROM PUBLIC, anon, authenticated")
        predicate = worker if table == "push_deliveries" else f"({account}) OR ({worker})"
        op.execute(
            f"CREATE POLICY notification_access ON cashly.{table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    for table in ("accounts", "expenses", "game_events"):
        op.execute(
            f"CREATE POLICY reminder_worker_read ON cashly.{table} FOR SELECT USING ({worker})"
        )
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cashly_runtime') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON cashly.notification_preferences,
          cashly.notifications, cashly.push_subscriptions, cashly.push_deliveries TO cashly_runtime;
      END IF;
    END $$;
    """)


def downgrade():
    for table in ("accounts", "expenses", "game_events"):
        op.execute(f"DROP POLICY reminder_worker_read ON cashly.{table}")
    for table in ("push_deliveries", "push_subscriptions", "notifications", "notification_preferences"):
        op.drop_table(table, schema="cashly")
