"""Account-owned game ledger, permanent mascot state and expense revisions. No seed."""

import sqlalchemy as sa
from alembic import op

revision = "0003_game_and_crud"
down_revision = "0002_expense_details"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "expenses",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        schema="cashly",
    )
    op.create_table(
        "game_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("cashly.accounts.id"), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("xp", sa.Integer(), server_default="0", nullable=False),
        sa.Column("freeze_delta", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("account_id", "event_key", name="uq_game_event_key"),
        sa.CheckConstraint("xp >= 0", name="ck_game_events_xp"),
        schema="cashly",
    )
    op.create_index(
        "ix_game_events_account_date", "game_events", ["account_id", "local_date"], schema="cashly"
    )
    op.create_index(
        "uq_game_daily_checkin",
        "game_events",
        ["account_id", "local_date"],
        unique=True,
        postgresql_where=sa.text("kind = 'checkin'"),
        schema="cashly",
    )
    op.create_table(
        "game_states",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("cashly.accounts.id"), primary_key=True),
        sa.Column("max_stage", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("max_stage BETWEEN 0 AND 5", name="ck_game_states_stage"),
        schema="cashly",
    )
    predicate = "account_id = nullif(current_setting('cashly.account_id', true), '')::uuid"
    for table in ("game_events", "game_states"):
        op.execute(f"ALTER TABLE cashly.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON cashly.{table} FROM PUBLIC, anon, authenticated")
        op.execute(
            f"CREATE POLICY account_scope ON cashly.{table} FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cashly_runtime') THEN
        GRANT SELECT, INSERT ON cashly.game_events TO cashly_runtime;
        GRANT SELECT, INSERT, UPDATE ON cashly.game_states TO cashly_runtime;
        END IF; END $$""")


def downgrade():
    op.drop_table("game_states", schema="cashly")
    op.drop_table("game_events", schema="cashly")
    op.drop_column("expenses", "version", schema="cashly")
