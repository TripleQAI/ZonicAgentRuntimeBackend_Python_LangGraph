-- 999_drop_all.sql — remove the agent_runtime schema and everything in it.
--
-- Unlike the builder's teardown, the runtime owns its tables, so this DOES drop
-- data: sessions, messages, runs and checkpoints. It refuses to run unless the
-- caller confirms with the schema name:
--
--     SET agentruntime.confirm_drop = 'agent_runtime';
--
-- agent_builder is never touched.

DO $$
BEGIN
    IF COALESCE(current_setting('agentruntime.confirm_drop', true), '') <> 'agent_runtime' THEN
        RAISE EXCEPTION 'drop not confirmed'
            USING HINT = 'Run:  SET agentruntime.confirm_drop = ''agent_runtime'';  first.';
    END IF;
END;
$$;

DROP SCHEMA IF EXISTS agent_runtime CASCADE;
