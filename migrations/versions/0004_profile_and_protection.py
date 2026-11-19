"""Persistent profile, account challenges and monthly protections. No seed/reset."""

import sqlalchemy as sa
from alembic import op

revision = "0004_profile_protection"
down_revision = "0003_game_and_crud"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("profiles", sa.Column("username", sa.String(24)), schema="cashly")
    # Deterministic collision-free names for existing accounts, including prior demos.
    op.execute("""WITH names AS (
        SELECT account_id, 'cashly_' || row_number() OVER (ORDER BY account_id)::text AS username
        FROM cashly.profiles)
        UPDATE cashly.profiles p SET username=n.username FROM names n WHERE p.account_id=n.account_id""")
    op.alter_column("profiles", "username", nullable=False, schema="cashly")
    op.create_check_constraint(
        "username_format", "profiles", "username ~ '^[a-z0-9._]{3,24}$'", schema="cashly"
    )
    op.execute("CREATE UNIQUE INDEX uq_profiles_username ON cashly.profiles (lower(username))")
    op.add_column(
        "profiles",
        sa.Column(
            "password_login_enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        schema="cashly",
    )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema='auth' AND table_name='users' AND column_name='encrypted_password') THEN
        UPDATE cashly.profiles p SET password_login_enabled=true
        FROM cashly.account_auth_links l JOIN auth.users u ON u.id=l.auth_user_id
        WHERE p.account_id=l.account_id AND coalesce(u.encrypted_password,'') <> '';
        END IF; END $$""")
    op.create_table(
        "streak_protections",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("cashly.accounts.id"), primary_key=True),
        sa.Column("protected_date", sa.Date(), primary_key=True),
        sa.Column("quota_month", sa.Date(), nullable=False),
        sa.Column(
            "used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="cashly",
    )
    op.create_index(
        "ix_protection_month", "streak_protections", ["account_id", "quota_month"], schema="cashly"
    )
    op.execute("""INSERT INTO cashly.streak_protections(account_id,protected_date,quota_month,used_at)
        SELECT e.account_id,e.local_date,
          date_trunc('month',e.created_at AT TIME ZONE p.timezone)::date,e.created_at
        FROM cashly.game_events e JOIN cashly.profiles p ON p.account_id=e.account_id
        WHERE e.kind='freeze_spent' ON CONFLICT DO NOTHING""")
    op.create_table(
        "account_security_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("cashly.accounts.id"), nullable=False),
        sa.Column("auth_user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("current_email", sa.String(254), nullable=False),
        sa.Column("new_email", sa.String(254)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_new", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        schema="cashly",
    )
    predicate = "account_id = nullif(current_setting('cashly.account_id', true), '')::uuid"
    for table in ("streak_protections", "account_security_challenges"):
        op.execute(f"ALTER TABLE cashly.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON cashly.{table} FROM PUBLIC, anon, authenticated")
        op.execute(
            f"CREATE POLICY account_scope ON cashly.{table} FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cashly_runtime') THEN
        GRANT SELECT,INSERT ON cashly.streak_protections TO cashly_runtime;
        GRANT SELECT,INSERT,UPDATE ON cashly.account_security_challenges TO cashly_runtime;
        END IF; END $$""")


def downgrade():
    op.drop_table("account_security_challenges", schema="cashly")
    op.drop_table("streak_protections", schema="cashly")
    op.drop_column("profiles", "password_login_enabled", schema="cashly")
    op.execute("DROP INDEX cashly.uq_profiles_username")
    op.drop_constraint("username_format", "profiles", type_="check", schema="cashly")
    op.drop_column("profiles", "username", schema="cashly")
