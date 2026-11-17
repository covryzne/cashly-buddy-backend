-- Optional administrator step after migration. Group role only, no password.
-- Create a separate LOGIN role privately and grant cashly_runtime membership.
-- Do not make the application's login role a table owner or give it BYPASSRLS.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cashly_runtime') THEN
        CREATE ROLE cashly_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END $$;
GRANT USAGE ON SCHEMA cashly TO cashly_runtime;
GRANT SELECT ON cashly.expense_categories, cashly.alembic_version TO cashly_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    cashly.accounts, cashly.profiles, cashly.account_auth_links,
    cashly.expenses, cashly.expense_items, cashly.idempotency_records TO cashly_runtime;
-- No CREATE, TRUNCATE, category writes or auth-schema access is granted.
-- RLS trusts only transaction-local identity set by the verified backend.
GRANT SELECT, INSERT ON cashly.game_events TO cashly_runtime;
GRANT SELECT, INSERT, UPDATE ON cashly.game_states TO cashly_runtime;
