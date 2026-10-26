-- 004_views.sql — the read surface of agent_observability (design Section 13.6).
--
-- Trace and observation views carry user_id and tenant_id for owner and role filtering. The two
-- daily views read materialized rollups refreshed by sp_obs_refresh_rollups, so dashboards never
-- scan the partitions.

CREATE OR REPLACE VIEW agent_observability.v_obs_trace AS
SELECT
    t.trace_id, t.run_id, t.name, t.environment, t.release, t.version, t.session_id,
    t.agentic_workflow_id, t.tags, t.metadata, t.input, t.output, t.status, t.start_time, t.end_time,
    t.latency_ms, t.observation_count, t.error_count, t.input_tokens, t.output_tokens, t.total_cost,
    t.user_id, t.tenant_id
FROM agent_observability.obs_trace t;

CREATE OR REPLACE VIEW agent_observability.v_obs_trace_list AS
SELECT
    t.trace_id, t.run_id, t.name, t.status, t.start_time, t.latency_ms, t.input_tokens, t.output_tokens,
    t.input_tokens + t.output_tokens AS total_tokens, t.total_cost, t.error_count, t.observation_count,
    t.tags, t.session_id, t.agentic_workflow_id, t.environment, t.user_id, t.tenant_id
FROM agent_observability.obs_trace t;

CREATE OR REPLACE VIEW agent_observability.v_obs_observation AS
SELECT
    o.observation_id, o.trace_id, o.parent_observation_id, o.type, o.name, o.level, o.status_message,
    o.environment, o.version, o.start_time, o.end_time, o.completion_start_time, o.latency_ms, o.input,
    o.output, o.metadata, o.model, o.model_parameters, o.usage_details, o.cost_details, o.total_cost,
    o.node_id, o.node_type, o.node_name, o.node_kind, o.visit, o.path, o.hook_binding_id,
    t.user_id, t.tenant_id
FROM agent_observability.obs_observation o
JOIN agent_observability.obs_trace t ON t.trace_id = o.trace_id;

CREATE OR REPLACE VIEW agent_observability.v_obs_score AS
SELECT
    s.score_id, s.trace_id, s.observation_id, s.session_id, s.name, s.data_type, s.value, s.string_value,
    s.source, s.comment, s.author_user_id, s.environment, s.timestamp, s.metadata,
    t.user_id, t.tenant_id
FROM agent_observability.obs_score s
JOIN agent_observability.obs_trace t ON t.trace_id = s.trace_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS agent_observability.mv_obs_node_stats_daily AS
SELECT
    date_trunc('day', o.start_time AT TIME ZONE 'UTC')::date                 AS day,
    COALESCE(t.agentic_workflow_id, '00000000-0000-0000-0000-000000000000') AS agentic_workflow_id,
    COALESCE(o.node_type, '')                                                AS node_type,
    COALESCE(o.node_name, '')                                                AS node_name,
    count(*)                                                                 AS executions,
    count(*) FILTER (WHERE o.level = 'ERROR')                                AS errors,
    round(count(*) FILTER (WHERE o.level = 'ERROR')::numeric / count(*), 4)  AS error_rate,
    round(avg(o.latency_ms)::numeric, 1)                                     AS avg_latency_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY o.latency_ms)               AS p95_latency_ms,
    COALESCE(sum((o.usage_details->>'input')::bigint), 0)                    AS input_tokens,
    COALESCE(sum((o.usage_details->>'output')::bigint), 0)                   AS output_tokens,
    COALESCE(sum(o.total_cost), 0)                                           AS total_cost
FROM agent_observability.obs_observation o
JOIN agent_observability.obs_trace t ON t.trace_id = o.trace_id
WHERE o.node_id IS NOT NULL AND o.level <> 'DEBUG'
GROUP BY 1, 2, 3, 4;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_obs_node_stats_daily
    ON agent_observability.mv_obs_node_stats_daily (day, agentic_workflow_id, node_type, node_name);

CREATE MATERIALIZED VIEW IF NOT EXISTS agent_observability.mv_obs_model_usage_daily AS
SELECT
    date_trunc('day', o.start_time AT TIME ZONE 'UTC')::date AS day,
    o.model,
    COALESCE(t.tenant_id, '')                                AS tenant_id,
    count(*)                                                 AS calls,
    COALESCE(sum((o.usage_details->>'input')::bigint), 0)    AS input_tokens,
    COALESCE(sum((o.usage_details->>'output')::bigint), 0)   AS output_tokens,
    COALESCE(sum(o.total_cost), 0)                           AS total_cost
FROM agent_observability.obs_observation o
JOIN agent_observability.obs_trace t ON t.trace_id = o.trace_id
WHERE o.model IS NOT NULL
GROUP BY 1, 2, 3;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_obs_model_usage_daily
    ON agent_observability.mv_obs_model_usage_daily (day, model, tenant_id);

CREATE OR REPLACE VIEW agent_observability.v_obs_node_stats_daily AS
SELECT * FROM agent_observability.mv_obs_node_stats_daily;

CREATE OR REPLACE VIEW agent_observability.v_obs_model_usage_daily AS
SELECT * FROM agent_observability.mv_obs_model_usage_daily;

INSERT INTO agent_observability.schema_migrations (version) VALUES ('004_views') ON CONFLICT (version) DO NOTHING;
