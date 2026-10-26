-- 006_grants.sql — least privilege for the role the gateway connects as.
--
-- Defence in depth: raw SQL is off at the gateway AND table DML is revoked here,
-- so the write surface is exactly the procedures even if the first control were
-- misconfigured. Procedures are SECURITY DEFINER, so they still write the tables.
--
-- Name the role first, then run this file (no psql meta-commands, so it also
-- pastes into a web SQL editor):
--
--     SET agentruntime.role = 'agentruntime_app';
--
-- Run it after 005, and again whenever 005 adds a procedure: EXECUTE is granted
-- on the procedures that exist at the time.
--
-- OPTIONAL when the gateway connects as the database owner (the usual Neon
-- arrangement): REVOKE on your own tables is a no-op.

DO $$
DECLARE
    v_role text := NULLIF(current_setting('agentruntime.role', true), '');
BEGIN
    IF v_role IS NULL THEN
        RAISE EXCEPTION 'no role named'
            USING HINT = 'Run:  SET agentruntime.role = ''your_role'';  first.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
        RAISE EXCEPTION 'role % does not exist', v_role;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_namespace n
        JOIN pg_roles r ON r.oid = n.nspowner
        WHERE n.nspname = 'agent_runtime' AND r.rolname = v_role
    ) THEN
        RAISE NOTICE
            'Role % owns the agent_runtime schema, so REVOKE cannot restrict it. '
            'Granting reads and executes anyway.', v_role;
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA agent_runtime TO %I', v_role);

    -- Reads: views only.
    EXECUTE format('GRANT SELECT ON
                    agent_runtime.v_agent_catalog,
                    agent_runtime.v_session_summary,
                    agent_runtime.v_message,
                    agent_runtime.v_run,
                    agent_runtime.v_run_queue,
                    agent_runtime.v_interrupt_pending,
                    agent_runtime.v_session_state,
                    agent_runtime.v_checkpoint,
                    agent_runtime.v_dataset TO %I', v_role);

    -- Writes: procedures only.
    EXECUTE format('GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA agent_runtime TO %I', v_role);

    -- Workflow documents come from the builder's export view.
    EXECUTE format('GRANT USAGE ON SCHEMA agent_builder TO %I', v_role);
    EXECUTE format('GRANT SELECT ON agent_builder.v_agent_export TO %I', v_role);

    -- Explicitly no direct DML on the base tables.
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
                    agent_runtime.runtime_session,
                    agent_runtime.runtime_run,
                    agent_runtime.runtime_message,
                    agent_runtime.runtime_checkpoint,
                    agent_runtime.runtime_dataset,
                    agent_runtime.runtime_interrupt,
                    agent_runtime.runtime_session_state,
                    agent_runtime.runtime_feedback
                    FROM %I', v_role);

    RAISE NOTICE 'Grants applied for %.', v_role;
END;
$$;

INSERT INTO agent_runtime.schema_migrations (version) VALUES ('006_grants')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
