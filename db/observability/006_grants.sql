-- 006_grants.sql — least privilege for the role the gateway connects as (datasource
-- agentobservability).
--
-- Name the role first, then run this file (no psql meta-commands, so it also pastes into a
-- web SQL editor):
--
--     SET agentobservability.role = 'agentruntime_app';
--
-- Run it after 005, and again whenever 005 adds a procedure. OPTIONAL when the gateway
-- connects as the database owner (the usual Neon arrangement).

DO $$
DECLARE
    v_role text := NULLIF(current_setting('agentobservability.role', true), '');
BEGIN
    IF v_role IS NULL THEN
        RAISE EXCEPTION 'no role named'
            USING HINT = 'Run:  SET agentobservability.role = ''your_role'';  first.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
        RAISE EXCEPTION 'role % does not exist', v_role;
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA agent_observability TO %I', v_role);

    -- Reads: views only (the daily views read the materialized rollups).
    EXECUTE format('GRANT SELECT ON
                    agent_observability.v_obs_trace,
                    agent_observability.v_obs_trace_list,
                    agent_observability.v_obs_observation,
                    agent_observability.v_obs_score,
                    agent_observability.v_obs_node_stats_daily,
                    agent_observability.v_obs_model_usage_daily TO %I', v_role);

    -- Writes: procedures only.
    EXECUTE format('GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA agent_observability TO %I', v_role);

    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
                    agent_observability.obs_trace,
                    agent_observability.obs_observation,
                    agent_observability.obs_score,
                    agent_observability.obs_model_price,
                    agent_observability.obs_partition
                    FROM %I', v_role);

    RAISE NOTICE 'Grants applied for %.', v_role;
END;
$$;

INSERT INTO agent_observability.schema_migrations (version) VALUES ('006_grants')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
