-- Only for the disposable Supabase Docker project cashly-backend-local.
-- Apply runtime_role.sql first. Never run this known password on hosted databases.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cashly_app') THEN
        CREATE ROLE cashly_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            INHERIT NOBYPASSRLS PASSWORD 'cashly-local-only';
    END IF;
END $$;
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'cashly_app'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR NOT rolinherit)
    ) THEN
        RAISE EXCEPTION 'Existing local runtime role has unexpected privileges';
    END IF;
END $$;
GRANT cashly_runtime TO cashly_app;
