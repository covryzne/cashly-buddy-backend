"""Atomically purge Cashly data when its Supabase Auth owner is deleted."""

from alembic import op

revision = "0006_account_deletion"
down_revision = "0005_password_recovery"
branch_labels = None
depends_on = None

FK = "fk_account_auth_links_auth_user_id_users"


def upgrade():
    # Supabase owns auth.users. Alter only the referencing Cashly table.
    op.drop_constraint(FK, "account_auth_links", schema="cashly", type_="foreignkey")
    op.create_foreign_key(
        FK,
        "account_auth_links",
        "users",
        ["auth_user_id"],
        ["id"],
        source_schema="cashly",
        referent_schema="auth",
        ondelete="CASCADE",
    )
    # A private lifecycle trigger, not an RLS-bypassing application RPC. Auth's
    # DB role must not receive general access to finance tables. The migration
    # owner performs a fixed, OLD-row-bound purge in the Auth delete transaction.
    op.execute("""CREATE FUNCTION cashly.purge_unlinked_account() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
          DELETE FROM cashly.password_recovery_sessions WHERE auth_user_id=OLD.auth_user_id;
          IF EXISTS (SELECT 1 FROM cashly.account_auth_links WHERE account_id=OLD.account_id) THEN
            RETURN OLD;
          END IF;
          PERFORM id FROM cashly.accounts WHERE id=OLD.account_id FOR UPDATE;
          DELETE FROM cashly.expense_items WHERE expense_id IN
            (SELECT id FROM cashly.expenses WHERE account_id=OLD.account_id);
          DELETE FROM cashly.expenses WHERE account_id=OLD.account_id;
          DELETE FROM cashly.idempotency_records WHERE account_id=OLD.account_id;
          DELETE FROM cashly.game_events WHERE account_id=OLD.account_id;
          DELETE FROM cashly.game_states WHERE account_id=OLD.account_id;
          DELETE FROM cashly.streak_protections WHERE account_id=OLD.account_id;
          DELETE FROM cashly.account_security_challenges WHERE account_id=OLD.account_id;
          DELETE FROM cashly.profiles WHERE account_id=OLD.account_id;
          DELETE FROM cashly.accounts WHERE id=OLD.account_id;
          RETURN OLD;
        END; $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION cashly.purge_unlinked_account() FROM PUBLIC, anon, authenticated"
    )
    op.execute("""CREATE TRIGGER purge_unlinked_account AFTER DELETE ON cashly.account_auth_links
        FOR EACH ROW EXECUTE FUNCTION cashly.purge_unlinked_account()""")


def downgrade():
    op.execute("DROP TRIGGER purge_unlinked_account ON cashly.account_auth_links")
    op.execute("DROP FUNCTION cashly.purge_unlinked_account()")
    op.drop_constraint(FK, "account_auth_links", schema="cashly", type_="foreignkey")
    op.create_foreign_key(
        FK,
        "account_auth_links",
        "users",
        ["auth_user_id"],
        ["id"],
        source_schema="cashly",
        referent_schema="auth",
    )
