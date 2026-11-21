"""whatsapp_link_history

Revision ID: 0008_whatsapp_history
Revises: 0007_whatsapp_linking
"""

from alembic import op

revision = "0008_whatsapp_history"
down_revision = "0007_whatsapp_linking"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE cashly.whatsapp_link_history (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      connection_id uuid NOT NULL,
      event varchar(16) NOT NULL,
      occurred_at timestamptz NOT NULL,
      phone_last4 varchar(4) NOT NULL,
      source varchar(24) NOT NULL,
      CONSTRAINT uq_whatsapp_link_history_event UNIQUE(connection_id,event),
      CONSTRAINT ck_whatsapp_link_history_event CHECK(event IN ('linked','disconnected')),
      CONSTRAINT ck_whatsapp_link_history_source CHECK(source IN ('action','existing_connection'))
    );
    CREATE INDEX ix_whatsapp_link_history_account_time
      ON cashly.whatsapp_link_history(account_id,occurred_at DESC,id DESC);

    -- Existing active connections have an authoritative verification time.
    -- Old deleted channels cannot be reconstructed from mutable challenges.
    LOCK TABLE cashly.whatsapp_channels IN SHARE ROW EXCLUSIVE MODE;
    INSERT INTO cashly.whatsapp_link_history
      (id,account_id,connection_id,event,occurred_at,phone_last4,source)
      SELECT id,account_id,id,'linked',verified_at,phone_last4,'existing_connection'
      FROM cashly.whatsapp_channels;

    ALTER TABLE cashly.whatsapp_link_history ENABLE ROW LEVEL SECURITY;
    REVOKE ALL ON cashly.whatsapp_link_history FROM PUBLIC, anon, authenticated;
    CREATE POLICY whatsapp_history_read ON cashly.whatsapp_link_history FOR SELECT
      USING (account_id=nullif(current_setting('cashly.account_id',true),'')::uuid);
    CREATE POLICY whatsapp_history_append ON cashly.whatsapp_link_history FOR INSERT
      WITH CHECK (account_id=nullif(current_setting('cashly.account_id',true),'')::uuid);
    """)


def downgrade():
    op.execute("DROP TABLE cashly.whatsapp_link_history")
