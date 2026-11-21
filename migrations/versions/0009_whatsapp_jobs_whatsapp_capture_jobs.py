"""whatsapp_capture_jobs

Revision ID: 0009_whatsapp_jobs
Revises: 0008_whatsapp_history
"""

from alembic import op

revision = "0009_whatsapp_jobs"
down_revision = "0008_whatsapp_history"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE cashly.whatsapp_capture_jobs (
      id uuid PRIMARY KEY,
      event_id uuid NOT NULL UNIQUE REFERENCES cashly.whatsapp_events(id) ON DELETE CASCADE,
      channel_id uuid NOT NULL REFERENCES cashly.whatsapp_channels(id) ON DELETE CASCADE,
      kind varchar(8) NOT NULL CHECK (kind IN ('text','receipt','voice')),
      state varchar(16) NOT NULL CHECK (state IN ('ready','running','saved','needs_review','failed','ignored')),
      expires_at timestamptz NOT NULL,
      input_cipher text,
      context_cipher text,
      lease_id uuid,
      lease_until timestamptz
    );
    CREATE INDEX ix_whatsapp_capture_jobs_channel ON cashly.whatsapp_capture_jobs(channel_id);
    CREATE INDEX ix_whatsapp_capture_jobs_expiry ON cashly.whatsapp_capture_jobs(expires_at)
      WHERE state IN ('ready','running') OR context_cipher IS NOT NULL;
    ALTER TABLE cashly.whatsapp_capture_jobs ENABLE ROW LEVEL SECURITY;
    REVOKE ALL ON cashly.whatsapp_capture_jobs FROM PUBLIC, anon, authenticated;
    CREATE POLICY whatsapp_capture_integration ON cashly.whatsapp_capture_jobs
      USING (current_setting('cashly.whatsapp_integration',true)='on')
      WITH CHECK (current_setting('cashly.whatsapp_integration',true)='on');
    """)


def downgrade():
    op.execute("DROP TABLE cashly.whatsapp_capture_jobs")
