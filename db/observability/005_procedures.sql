-- 005_procedures.sql — the write surface of agent_observability (design Section 13.6).
--
-- Records arrive as camelCase JSON (ObsTraceRecord, ObsObservationRecord, ObsScoreRecord) through
-- the gateway, positional, with trailing nulls for the INOUT counters. sp_obs_ingest is idempotent
-- and is the one procedure the runtime retries.

-- One transaction per batch:
--   * observation partitions are created for every month the batch touches;
--   * traces are upserted in batch order: later non-null scalar values win, tags are unioned,
--     metadata is merged, the earliest start time is kept;
--   * observations and scores may arrive before their trace header: a minimal trace row is created
--     and completed by the later header (case O-05);
--   * observations are inserted once (duplicates on (observation_id, start_time) are ignored); cost
--     is computed from obs_model_price when the exporter did not supply it;
--   * scores are upserted by score_id;
--   * counters, tokens and cost are recomputed for every touched trace.
-- The INOUT counters return the number of records accepted of each kind.
CREATE OR REPLACE PROCEDURE agent_observability.sp_obs_ingest(
    p_traces jsonb,
    p_observations jsonb,
    p_scores jsonb,
    INOUT o_trace_count integer DEFAULT NULL,
    INOUT o_observation_count integer DEFAULT NULL,
    INOUT o_score_count integer DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_observability, pg_temp AS $$
DECLARE
    v_traces       jsonb := COALESCE(p_traces, '[]'::jsonb);
    v_observations jsonb := COALESCE(p_observations, '[]'::jsonb);
    v_scores       jsonb := COALESCE(p_scores, '[]'::jsonb);
    v_month        timestamptz;
    v_trace        jsonb;
    v_touched      text[];
BEGIN
    FOR v_month IN
        SELECT DISTINCT date_trunc('month', (o->>'startTime')::timestamptz)
          FROM jsonb_array_elements(v_observations) AS o
    LOOP
        PERFORM fn_obs_ensure_partition(v_month);
    END LOOP;

    FOR v_trace IN SELECT t FROM jsonb_array_elements(v_traces) WITH ORDINALITY AS x (t, n) ORDER BY n LOOP
        INSERT INTO obs_trace AS tr (
            trace_id, run_id, name, environment, release, version, session_id, user_id, tenant_id,
            agentic_workflow_id, tags, metadata, input, output, status, start_time, end_time)
        VALUES (
            v_trace->>'traceId',
            COALESCE((v_trace->>'runId')::uuid, (v_trace->>'traceId')::uuid),
            COALESCE(v_trace->>'name', ''),
            COALESCE(v_trace->>'environment', 'default'),
            v_trace->>'release', v_trace->>'version', (v_trace->>'sessionId')::uuid, v_trace->>'userId',
            v_trace->>'tenantId', (v_trace->>'agenticWorkflowId')::uuid,
            COALESCE(ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_trace->'tags', '[]'::jsonb))), '{}'),
            COALESCE(v_trace->'metadata', '{}'::jsonb), v_trace->'input', v_trace->'output', v_trace->>'status',
            -- A header without a start (an update that only ends the run) must not start at now(): its
            -- end may be weeks earlier, which would give a negative latency; it falls back to the end.
            COALESCE((v_trace->>'startTime')::timestamptz, (v_trace->>'endTime')::timestamptz, now()),
            (v_trace->>'endTime')::timestamptz)
        ON CONFLICT (trace_id) DO UPDATE SET
            name                = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE tr.name END,
            environment         = CASE WHEN v_trace ? 'environment' THEN EXCLUDED.environment ELSE tr.environment END,
            release             = COALESCE(EXCLUDED.release, tr.release),
            version             = COALESCE(EXCLUDED.version, tr.version),
            session_id          = COALESCE(EXCLUDED.session_id, tr.session_id),
            user_id             = COALESCE(EXCLUDED.user_id, tr.user_id),
            tenant_id           = COALESCE(EXCLUDED.tenant_id, tr.tenant_id),
            agentic_workflow_id = COALESCE(EXCLUDED.agentic_workflow_id, tr.agentic_workflow_id),
            tags                = ARRAY(SELECT DISTINCT unnest(tr.tags || EXCLUDED.tags) ORDER BY 1),
            metadata            = tr.metadata || EXCLUDED.metadata,
            input               = COALESCE(EXCLUDED.input, tr.input),
            output              = COALESCE(EXCLUDED.output, tr.output),
            status              = COALESCE(EXCLUDED.status, tr.status),
            start_time          = LEAST(tr.start_time, EXCLUDED.start_time),
            end_time            = COALESCE(EXCLUDED.end_time, tr.end_time);
    END LOOP;

    -- Minimal trace rows for observations and scores whose header has not arrived yet.
    INSERT INTO obs_trace (trace_id, run_id, start_time)
    SELECT x.trace_id, x.trace_id::uuid, min(x.start_time)
      FROM (
          SELECT o->>'traceId' AS trace_id, (o->>'startTime')::timestamptz AS start_time
            FROM jsonb_array_elements(v_observations) AS o
          UNION ALL
          SELECT s->>'traceId', COALESCE((s->>'timestamp')::timestamptz, now())
            FROM jsonb_array_elements(v_scores) AS s
      ) x
     GROUP BY x.trace_id
    ON CONFLICT (trace_id) DO NOTHING;

    INSERT INTO obs_observation (
        observation_id, trace_id, parent_observation_id, type, name, level, status_message, environment, version,
        start_time, end_time, completion_start_time, latency_ms, input, output, metadata, model, model_parameters,
        usage_details, cost_details, total_cost, node_id, node_type, node_name, node_kind, visit, path,
        hook_binding_id)
    SELECT
        o->>'observationId', o->>'traceId', o->>'parentObservationId', o->>'type', left(o->>'name', 200),
        COALESCE(o->>'level', 'DEFAULT'), left(o->>'statusMessage', 500), COALESCE(o->>'environment', 'default'),
        o->>'version', (o->>'startTime')::timestamptz, (o->>'endTime')::timestamptz,
        (o->>'completionStartTime')::timestamptz,
        COALESCE((o->>'latencyMs')::bigint,
                 CASE WHEN o->>'endTime' IS NOT NULL
                      THEN (EXTRACT(EPOCH FROM (o->>'endTime')::timestamptz - (o->>'startTime')::timestamptz) * 1000)::bigint
                 END),
        o->'input', o->'output', COALESCE(o->'metadata', '{}'::jsonb), o->>'model', o->'modelParameters',
        o->'usageDetails',
        COALESCE(o->'costDetails', cost.value->'cost_details'),
        COALESCE((o->>'totalCost')::numeric, (cost.value->>'total_cost')::numeric),
        o->>'nodeId', o->>'nodeType', o->>'nodeName', o->>'nodeKind', (o->>'visit')::integer, o->>'path',
        o->>'hookBindingId'
      FROM jsonb_array_elements(v_observations) AS o
      LEFT JOIN LATERAL (
          SELECT fn_obs_cost(o->>'model', o->'usageDetails', (o->>'startTime')::timestamptz) AS value
           WHERE o->>'totalCost' IS NULL
      ) cost ON true
    ON CONFLICT (observation_id, start_time) DO NOTHING;

    INSERT INTO obs_score AS sc (
        score_id, trace_id, observation_id, session_id, name, data_type, value, string_value, source, comment,
        author_user_id, environment, timestamp, metadata)
    SELECT DISTINCT ON ((s->>'scoreId')::uuid)
        (s->>'scoreId')::uuid, s->>'traceId', s->>'observationId', (s->>'sessionId')::uuid, s->>'name',
        s->>'dataType', (s->>'value')::numeric, s->>'stringValue', COALESCE(s->>'source', 'API'), s->>'comment',
        s->>'authorUserId', COALESCE(s->>'environment', 'default'), COALESCE((s->>'timestamp')::timestamptz, now()),
        COALESCE(s->'metadata', '{}'::jsonb)
      FROM jsonb_array_elements(v_scores) WITH ORDINALITY AS x (s, n)
     ORDER BY (s->>'scoreId')::uuid, n DESC
    ON CONFLICT (score_id) DO UPDATE SET
        value = EXCLUDED.value, string_value = EXCLUDED.string_value, comment = EXCLUDED.comment,
        metadata = sc.metadata || EXCLUDED.metadata, timestamp = EXCLUDED.timestamp;

    v_touched := ARRAY(
        SELECT DISTINCT trace_id FROM (
            SELECT t->>'traceId' AS trace_id FROM jsonb_array_elements(v_traces) AS t
            UNION SELECT o->>'traceId' FROM jsonb_array_elements(v_observations) AS o
        ) touched);

    UPDATE obs_trace t
       SET observation_count = agg.observation_count,
           error_count       = agg.error_count,
           input_tokens      = agg.input_tokens,
           output_tokens     = agg.output_tokens,
           total_cost        = agg.total_cost
      FROM (
          SELECT tr.trace_id,
                 count(o.observation_id) FILTER (WHERE o.level <> 'DEBUG')                    AS observation_count,
                 count(o.observation_id) FILTER (WHERE o.level = 'ERROR')                     AS error_count,
                 COALESCE(sum((o.usage_details->>'input')::bigint), 0)                         AS input_tokens,
                 COALESCE(sum((o.usage_details->>'output')::bigint), 0)                        AS output_tokens,
                 COALESCE(sum(o.total_cost), 0)                                                AS total_cost
            FROM obs_trace tr
            LEFT JOIN obs_observation o ON o.trace_id = tr.trace_id
           WHERE tr.trace_id = ANY (v_touched)
           GROUP BY tr.trace_id
      ) agg
     WHERE t.trace_id = agg.trace_id;

    o_trace_count := jsonb_array_length(v_traces);
    o_observation_count := jsonb_array_length(v_observations);
    o_score_count := jsonb_array_length(v_scores);
END;
$$;

-- Feedback and evaluation scores outside a batch.
CREATE OR REPLACE PROCEDURE agent_observability.sp_obs_score_upsert(p_score jsonb, INOUT o_score_id uuid DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_observability, pg_temp AS $$
DECLARE
    v_score jsonb := p_score || jsonb_build_object('scoreId', COALESCE(p_score->>'scoreId', gen_random_uuid()::text));
    v_t integer; v_o integer; v_s integer;
BEGIN
    CALL sp_obs_ingest('[]'::jsonb, '[]'::jsonb, jsonb_build_array(v_score), v_t, v_o, v_s);
    o_score_id := (v_score->>'scoreId')::uuid;
END;
$$;

-- Maintains prices: each new price closes the open validity period of the same pattern and key.
CREATE OR REPLACE PROCEDURE agent_observability.sp_obs_model_price_upsert(p_prices jsonb, INOUT o_count integer DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_observability, pg_temp AS $$
DECLARE
    v_price jsonb;
BEGIN
    o_count := 0;
    FOR v_price IN SELECT p FROM jsonb_array_elements(COALESCE(p_prices, '[]'::jsonb)) WITH ORDINALITY AS x (p, n) ORDER BY n LOOP
        UPDATE obs_model_price
           SET valid_to = (v_price->>'validFrom')::timestamptz
         WHERE model_pattern = v_price->>'modelPattern' AND usage_key = v_price->>'usageKey'
           AND valid_to IS NULL AND valid_from < (v_price->>'validFrom')::timestamptz;
        INSERT INTO obs_model_price (model_pattern, usage_key, price_per_unit, currency, valid_from, valid_to)
        VALUES (v_price->>'modelPattern', v_price->>'usageKey', (v_price->>'pricePerUnit')::numeric,
                COALESCE(v_price->>'currency', 'USD'), (v_price->>'validFrom')::timestamptz,
                (v_price->>'validTo')::timestamptz)
        ON CONFLICT (model_pattern, usage_key, valid_from) DO UPDATE
           SET price_per_unit = EXCLUDED.price_per_unit, currency = EXCLUDED.currency, valid_to = EXCLUDED.valid_to;
        o_count := o_count + 1;
    END LOOP;
END;
$$;

-- Creates monthly partitions from the current month to p_months_ahead months ahead.
CREATE OR REPLACE PROCEDURE agent_observability.sp_obs_partition_ensure(p_months_ahead integer, INOUT o_created integer DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_observability, pg_temp AS $$
DECLARE
    i integer;
BEGIN
    o_created := 0;
    FOR i IN 0 .. GREATEST(p_months_ahead, 0) LOOP
        IF fn_obs_ensure_partition(now() + make_interval(months => i)) THEN
            o_created := o_created + 1;
        END IF;
    END LOOP;
END;
$$;

-- Drops whole observation partitions that end before p_before, and deletes the traces (and, by
-- cascade, scores) that started before it. No row-by-row deletes of observations.
CREATE OR REPLACE PROCEDURE agent_observability.sp_obs_retention_purge(
    p_before timestamptz, INOUT o_partitions_dropped integer DEFAULT NULL, INOUT o_traces_deleted integer DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_observability, pg_temp AS $$
DECLARE
    v_name text;
BEGIN
    o_partitions_dropped := 0;
    FOR v_name IN SELECT partition_name FROM obs_partition WHERE range_end <= p_before ORDER BY range_start LOOP
        EXECUTE format('DROP TABLE IF EXISTS agent_observability.%I', v_name);
        DELETE FROM obs_partition WHERE partition_name = v_name;
        o_partitions_dropped := o_partitions_dropped + 1;
    END LOOP;
    DELETE FROM obs_trace WHERE start_time < p_before;
    GET DIAGNOSTICS o_traces_deleted = ROW_COUNT;
END;
$$;

-- Refreshes the daily rollups without blocking readers.
CREATE OR REPLACE PROCEDURE agent_observability.sp_obs_refresh_rollups(INOUT o_refreshed integer DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_observability, pg_temp AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_obs_node_stats_daily;
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_obs_model_usage_daily;
    o_refreshed := 2;
END;
$$;

INSERT INTO agent_observability.schema_migrations (version) VALUES ('005_procedures') ON CONFLICT (version) DO NOTHING;
