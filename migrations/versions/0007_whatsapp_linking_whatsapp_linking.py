"""whatsapp_linking

Revision ID: 0007_whatsapp_linking
Revises: 0006_account_deletion
"""

from alembic import op

revision = "0007_whatsapp_linking"
down_revision = "0006_account_deletion"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE cashly.whatsapp_challenges (
      id uuid PRIMARY KEY, account_id uuid NOT NULL REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      request_hash varchar(64) NOT NULL, reference_hash varchar(64) NOT NULL UNIQUE,
      reference_cipher text NOT NULL, created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL, resend_at timestamptz NOT NULL,
      state varchar(24) NOT NULL, attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
      sender_hash varchar(64), sender_cipher text, phone_hash varchar(64), phone_last4 varchar(4),
      aliases jsonb NOT NULL DEFAULT '[]', otp_digest varchar(64), otp_expires_at timestamptz,
      UNIQUE(account_id,request_hash),
      CHECK(state IN ('awaiting_message','otp_sent','verified','cancelled','expired','locked'))
    );
    CREATE INDEX ix_whatsapp_challenges_account_time ON cashly.whatsapp_challenges(account_id,created_at DESC);
    CREATE TABLE cashly.whatsapp_channels (
      id uuid PRIMARY KEY, account_id uuid NOT NULL UNIQUE REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      phone_hash varchar(64) NOT NULL UNIQUE, phone_last4 varchar(4) NOT NULL,
      verified_at timestamptz NOT NULL
    );
    CREATE TABLE cashly.whatsapp_aliases (
      identity_hash varchar(64) PRIMARY KEY,
      channel_id uuid NOT NULL REFERENCES cashly.whatsapp_channels(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_whatsapp_aliases_channel ON cashly.whatsapp_aliases(channel_id);
    CREATE TABLE cashly.whatsapp_events (
      id uuid PRIMARY KEY, bot_id varchar(64) NOT NULL, message_id varchar(255) NOT NULL,
      sender_hash varchar(64) NOT NULL, fingerprint varchar(64) NOT NULL,
      account_id uuid REFERENCES cashly.accounts(id) ON DELETE CASCADE, created_at timestamptz NOT NULL,
      UNIQUE(bot_id,message_id)
    );
    CREATE INDEX ix_whatsapp_events_sender_time ON cashly.whatsapp_events(sender_hash,created_at);
    CREATE INDEX ix_whatsapp_events_account ON cashly.whatsapp_events(account_id);
    CREATE TABLE cashly.whatsapp_outbox (
      id uuid PRIMARY KEY, event_id uuid NOT NULL UNIQUE REFERENCES cashly.whatsapp_events(id) ON DELETE CASCADE,
      account_id uuid REFERENCES cashly.accounts(id) ON DELETE CASCADE,
      challenge_id uuid REFERENCES cashly.whatsapp_challenges(id) ON DELETE CASCADE,
      payload_cipher text, state varchar(16) NOT NULL,
      created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
      lease_id uuid, lease_until timestamptz, outbound_message_id varchar(255),
      CHECK(state IN ('ready','claimed','accepted','unknown','cancelled','expired'))
    );
    CREATE INDEX ix_whatsapp_outbox_ready ON cashly.whatsapp_outbox(created_at) WHERE state='ready';
    CREATE INDEX ix_whatsapp_outbox_account ON cashly.whatsapp_outbox(account_id);
    CREATE INDEX ix_whatsapp_outbox_challenge ON cashly.whatsapp_outbox(challenge_id);
    """)
    account = "nullif(current_setting('cashly.account_id', true),'')::uuid"
    integration = "current_setting('cashly.whatsapp_integration',true)='on'"
    for table in ("whatsapp_challenges", "whatsapp_channels", "whatsapp_events", "whatsapp_outbox"):
        op.execute(f"ALTER TABLE cashly.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON cashly.{table} FROM PUBLIC, anon, authenticated")
        predicate = f"(account_id={account} OR {integration})"
        op.execute(
            f"CREATE POLICY whatsapp_scope ON cashly.{table} FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute("ALTER TABLE cashly.whatsapp_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON cashly.whatsapp_aliases FROM PUBLIC, anon, authenticated")
    predicate = f"({integration} OR EXISTS(SELECT 1 FROM cashly.whatsapp_channels c WHERE c.id=channel_id AND c.account_id={account}))"
    op.execute(
        f"CREATE POLICY whatsapp_scope ON cashly.whatsapp_aliases FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade():
    for table in (
        "whatsapp_outbox",
        "whatsapp_events",
        "whatsapp_aliases",
        "whatsapp_channels",
        "whatsapp_challenges",
    ):
        op.execute(f"DROP TABLE cashly.{table}")
