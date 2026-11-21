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
GRANT SELECT, INSERT ON cashly.streak_protections TO cashly_runtime;
GRANT SELECT, INSERT, UPDATE ON cashly.account_security_challenges TO cashly_runtime;

GRANT SELECT, INSERT, UPDATE ON cashly.password_recovery_sessions TO cashly_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    cashly.whatsapp_challenges, cashly.whatsapp_channels, cashly.whatsapp_aliases,
    cashly.whatsapp_events, cashly.whatsapp_outbox TO cashly_runtime;

-- History survives disconnect; application runtime can only read/append.
GRANT SELECT, INSERT ON cashly.whatsapp_link_history TO cashly_runtime;
GRANT SELECT, INSERT, UPDATE ON cashly.whatsapp_capture_jobs TO cashly_runtime;
