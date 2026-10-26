-- 999_drop_all.sql — remove the agent_observability schema and every trace in it.
--
-- Refuses to run unless the caller confirms with the schema name:
--
--     SET agentobservability.confirm_drop = 'agent_observability';
--
-- agent_runtime and agent_builder are never touched.

DO $$
BEGIN
    IF COALESCE(current_setting('agentobservability.confirm_drop', true), '') <> 'agent_observability' THEN
        RAISE EXCEPTION 'drop not confirmed'
            USING HINT = 'Run:  SET agentobservability.confirm_drop = ''agent_observability'';  first.';
    END IF;
END;
$$;

DROP SCHEMA IF EXISTS agent_observability CASCADE;
