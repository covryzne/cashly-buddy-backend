"""Store short-lived WhatsApp chat redirects for follow-up questions."""

from alembic import op

revision = "0010_whatsapp_redirect"
down_revision = "0009_whatsapp_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE cashly.whatsapp_capture_jobs ADD COLUMN redirect_context_cipher text;
    CREATE INDEX ix_whatsapp_capture_jobs_redirect_expiry
      ON cashly.whatsapp_capture_jobs (expires_at) WHERE redirect_context_cipher IS NOT NULL;
    """)


def downgrade():
    op.execute("""
    DROP INDEX cashly.ix_whatsapp_capture_jobs_redirect_expiry;
    ALTER TABLE cashly.whatsapp_capture_jobs DROP COLUMN redirect_context_cipher;
    """)
