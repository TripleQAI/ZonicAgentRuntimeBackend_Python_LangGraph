-- 003_indexes.sql — agent_observability indexes (design Section 13.6). Indexes on the partitioned
-- observation table propagate to every partition.

CREATE INDEX IF NOT EXISTS ix_trace_session ON agent_observability.obs_trace (session_id, start_time DESC);
CREATE INDEX IF NOT EXISTS ix_trace_user ON agent_observability.obs_trace (user_id, start_time DESC);
CREATE INDEX IF NOT EXISTS ix_trace_workflow ON agent_observability.obs_trace (agentic_workflow_id, start_time DESC);
CREATE INDEX IF NOT EXISTS ix_trace_tags ON agent_observability.obs_trace USING gin (tags);
CREATE INDEX IF NOT EXISTS ix_trace_start ON agent_observability.obs_trace (start_time);

CREATE INDEX IF NOT EXISTS ix_obs_trace ON agent_observability.obs_observation (trace_id, start_time);
CREATE INDEX IF NOT EXISTS ix_obs_node_type ON agent_observability.obs_observation (node_type, start_time);
CREATE INDEX IF NOT EXISTS ix_obs_node_name ON agent_observability.obs_observation (node_name, start_time);
CREATE INDEX IF NOT EXISTS ix_obs_errors ON agent_observability.obs_observation (start_time)
    WHERE level IN ('WARNING', 'ERROR');

CREATE INDEX IF NOT EXISTS ix_score_trace ON agent_observability.obs_score (trace_id);

INSERT INTO agent_observability.schema_migrations (version) VALUES ('003_indexes') ON CONFLICT (version) DO NOTHING;
