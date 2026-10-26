-- 002_functions.sql — partition and cost helpers for agent_observability.

CREATE OR REPLACE FUNCTION agent_observability.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_obs_trace_updated_at ON agent_observability.obs_trace;
CREATE TRIGGER trg_obs_trace_updated_at BEFORE UPDATE ON agent_observability.obs_trace
    FOR EACH ROW EXECUTE FUNCTION agent_observability.set_updated_at();

-- Creates the monthly observation partition that holds p_time, if it is missing. Returns true when
-- it created one. Serialized by an advisory lock so concurrent ingests cannot race on CREATE.
CREATE OR REPLACE FUNCTION agent_observability.fn_obs_ensure_partition(p_time timestamptz)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
    v_start timestamptz := date_trunc('month', p_time AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    v_end   timestamptz := (date_trunc('month', p_time AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC';
    v_name  text := 'obs_observation_y' || to_char(v_start AT TIME ZONE 'UTC', 'YYYY') || 'm'
                    || to_char(v_start AT TIME ZONE 'UTC', 'MM');
BEGIN
    IF EXISTS (SELECT 1 FROM agent_observability.obs_partition WHERE partition_name = v_name) THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('agent_observability.obs_partition'));
    IF to_regclass('agent_observability.' || v_name) IS NULL THEN
        EXECUTE format('CREATE TABLE agent_observability.%I PARTITION OF agent_observability.obs_observation '
                       'FOR VALUES FROM (%L) TO (%L)', v_name, v_start, v_end);
        INSERT INTO agent_observability.obs_partition (partition_name, range_start, range_end)
        VALUES (v_name, v_start, v_end) ON CONFLICT (partition_name) DO NOTHING;
        RETURN true;
    END IF;
    INSERT INTO agent_observability.obs_partition (partition_name, range_start, range_end)
    VALUES (v_name, v_start, v_end) ON CONFLICT (partition_name) DO NOTHING;
    RETURN false;
END;
$$;

-- Cost of one observation from obs_model_price: per usage key, the price valid at p_time for the
-- most specific matching pattern (longest), times the units. Returns {cost_details, total_cost}.
CREATE OR REPLACE FUNCTION agent_observability.fn_obs_cost(p_model text, p_usage jsonb, p_time timestamptz)
RETURNS jsonb LANGUAGE sql STABLE AS $$
    WITH units AS (
        SELECT u.key AS usage_key, (u.value)::numeric AS amount
          FROM jsonb_each_text(COALESCE(p_usage, '{}'::jsonb)) AS u (key, value)
         WHERE u.value ~ '^-?[0-9]+(\.[0-9]+)?$'
    ), priced AS (
        SELECT units.usage_key, units.amount * p.price_per_unit AS cost
          FROM units
          JOIN LATERAL (
              SELECT price_per_unit
                FROM agent_observability.obs_model_price mp
               WHERE mp.usage_key = units.usage_key
                 AND p_model ~ mp.model_pattern
                 AND mp.valid_from <= p_time
                 AND (mp.valid_to IS NULL OR mp.valid_to > p_time)
               ORDER BY length(mp.model_pattern) DESC, mp.valid_from DESC
               LIMIT 1
          ) p ON true
    )
    SELECT CASE WHEN p_model IS NULL OR NOT EXISTS (SELECT 1 FROM priced) THEN NULL
                ELSE jsonb_build_object(
                    'cost_details', (SELECT jsonb_object_agg(usage_key, round(cost, 6)) FROM priced),
                    'total_cost', (SELECT round(sum(cost), 6) FROM priced))
           END;
$$;

INSERT INTO agent_observability.schema_migrations (version) VALUES ('002_functions') ON CONFLICT (version) DO NOTHING;
