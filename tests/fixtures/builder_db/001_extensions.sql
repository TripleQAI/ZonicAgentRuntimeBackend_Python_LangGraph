-- 001_extensions.sql — the schema, and the one function we depend on.
-- Idempotent.

-- Everything this project reads or writes lives in agent_builder. The schema
-- already exists on a database created by the Django app; this is here so a
-- clean install works too.
CREATE SCHEMA IF NOT EXISTS agent_builder;

-- gen_random_uuid() is built into PostgreSQL 13 and later. On older servers it
-- comes from pgcrypto.
--
-- A managed service may not let the connecting role create extensions, and an
-- outright CREATE EXTENSION would then abort the whole install under
-- ON_ERROR_STOP. So: only try if the function is actually missing, treat a
-- permission failure as non-fatal, and fail with a clear message only if the
-- function is still unavailable afterwards — because every insert needs it.
DO $$
BEGIN
    IF to_regprocedure('gen_random_uuid()') IS NOT NULL THEN
        RAISE NOTICE 'gen_random_uuid() is already available.';
        RETURN;
    END IF;

    BEGIN
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
        RAISE NOTICE 'pgcrypto installed for gen_random_uuid().';
    EXCEPTION WHEN insufficient_privilege OR feature_not_supported THEN
        RAISE NOTICE 'Cannot create pgcrypto here (%). Checking for a built-in.', SQLERRM;
    END;

    IF to_regprocedure('gen_random_uuid()') IS NULL THEN
        RAISE EXCEPTION 'gen_random_uuid() is not available'
            USING HINT = 'Upgrade to PostgreSQL 13+, or ask an administrator to '
                         'run: CREATE EXTENSION pgcrypto;';
    END IF;
END;
$$;
