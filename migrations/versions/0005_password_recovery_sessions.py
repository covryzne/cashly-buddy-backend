"""Single-use password recovery sessions; no provider token is stored."""

import sqlalchemy as sa
from alembic import op

revision = "0005_password_recovery"
down_revision = "0004_profile_protection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "password_recovery_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("auth_user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        schema="cashly",
    )
    op.create_index(
        "ix_recovery_identity", "password_recovery_sessions", ["auth_user_id"], schema="cashly"
    )
    op.execute("ALTER TABLE cashly.password_recovery_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON cashly.password_recovery_sessions FROM PUBLIC, anon, authenticated")
    predicate = "auth_user_id = nullif(current_setting('cashly.auth_user_id', true), '')::uuid"
    op.execute(
        f"CREATE POLICY identity_scope ON cashly.password_recovery_sessions FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cashly_runtime') THEN
        GRANT SELECT,INSERT,UPDATE ON cashly.password_recovery_sessions TO cashly_runtime;
        END IF; END $$""")


def downgrade():
    op.drop_table("password_recovery_sessions", schema="cashly")
