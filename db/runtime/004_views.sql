-- 004_views.sql — the read surface. Every read the runtime makes names one of these views.
--
-- This project's own file, over the agent_runtime tables vendored from
-- ZonicAgentRuntimeServer_Nodejs (001 to 003, 005; see SOURCE.json). Column sets follow that
-- project's 004 with two changes:
--   * v_agent_catalog reads publishChannels and security in the shape the builder saves
--     them (objects with a code), one row per channel;
--   * the LangGraph checkpoint fields (plan DR-8, DR-9) are read from the checkpoint body
--     ({format, langgraphVersion, lastEventSeq, ...}), so the procedures need no change.
--
-- The gateway's view payload cannot join or express OR, so shaping happens here. Views that
-- serve user data carry owner_sub; the runtime's repository always filters on it.
-- v_agent_catalog reads agent_builder, so the builder schema must be installed first.
-- Idempotent: CREATE OR REPLACE throughout (new columns are appended at the end).

-- Runnable workflows: root and not disabled. One row per (workflow, publish channel), so the
-- channel rule (design Q2) is an equality filter on runtime_channel; runtime_channel is NULL
-- for a workflow without channels, which only development settings list. security passes the
-- builder's {activities: [{code}]} through for display; the runtime does not enforce it, as the
-- Cognito token is its only access control (docs/decisions/0004).
CREATE OR REPLACE VIEW agent_runtime.v_agent_catalog AS
SELECT
    a.agentic_workflow_id,
    a.name,
    a.display_name,
    a.description,
    a.bpc,
    a.environment,
    a.updated_at,
    COALESCE(a.publish_channels, '[]'::jsonb) AS publish_channels,
    COALESCE(a.security, '{}'::jsonb) AS security,
    CASE WHEN jsonb_typeof(a.nodes) = 'array' THEN jsonb_array_length(a.nodes) ELSE 0 END AS node_count,
    ch.code AS runtime_channel
FROM agent_builder."agentBuilder_agentworkflow" a
LEFT JOIN LATERAL (
    SELECT CASE jsonb_typeof(c) WHEN 'object' THEN c->>'code' ELSE c #>> '{}' END AS code
    FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.publish_channels) = 'array' THEN a.publish_channels ELSE '[]'::jsonb END) AS c
) ch ON true
WHERE a.is_root AND NOT a.is_disabled;

CREATE OR REPLACE VIEW agent_runtime.v_session_summary AS
SELECT
    s.session_id,
    s.agentic_workflow_id,
    s.title,
    s.status,
    s.tenant_id,
    s.created_at,
    s.updated_at,
    s.last_message_at,
    s.owner_sub
FROM agent_runtime.runtime_session s;

CREATE OR REPLACE VIEW agent_runtime.v_message AS
SELECT
    m.message_id,
    m.session_id,
    m.run_id,
    m.node_id,
    m.visit,
    m.seq,
    m.role,
    m.kind,
    m.visibility,
    m.thread,
    m.content_parts,
    m.text_preview,
    m.created_at,
    f.rating AS feedback_rating,
    s.owner_sub
FROM agent_runtime.runtime_message m
JOIN agent_runtime.runtime_session s ON s.session_id = m.session_id
LEFT JOIN agent_runtime.runtime_feedback f ON f.message_id = m.message_id AND f.owner_sub = s.owner_sub;

-- has_checkpoint and last_event_seq tell a claimer or ResumeRun whether to restore and where
-- event seq continues (DR-8, DR-9).
CREATE OR REPLACE VIEW agent_runtime.v_run AS
SELECT
    r.run_id,
    r.session_id,
    r.agentic_workflow_id,
    r.workflow_revision,
    r.document_version,
    r.status,
    r.mode,
    r.input,
    r.result,
    r.error,
    r.usage,
    r.node_timeline,
    r.idempotency_key,
    r.langfuse_trace_id,
    r.queued_at,
    r.started_at,
    r.finished_at,
    r.deadline_at,
    r.updated_at,
    r.attempt,
    r.lease_owner,
    r.lease_address,
    r.lease_expires_at,
    r.callback,
    r.tenant_id,
    COALESCE((SELECT jsonb_agg(i.interrupt_id ORDER BY i.requested_at)
                FROM agent_runtime.runtime_interrupt i
               WHERE i.run_id = r.run_id AND i.status = 'pending'), '[]'::jsonb) AS pending_interrupts,
    r.owner_sub,
    r.priority,
    (c.run_id IS NOT NULL) AS has_checkpoint,
    COALESCE((c.body->>'lastEventSeq')::bigint, 0) AS last_event_seq
FROM agent_runtime.runtime_run r
LEFT JOIN agent_runtime.runtime_checkpoint c ON c.run_id = r.run_id;

-- Queue depth per tenant, for autoscaling and the claimer's back-off. Not user-facing.
CREATE OR REPLACE VIEW agent_runtime.v_run_queue AS
SELECT
    r.tenant_id,
    count(*)            AS queued_count,
    min(r.queued_at)    AS oldest_queued_at
FROM agent_runtime.runtime_run r
WHERE r.status = 'queued'
GROUP BY r.tenant_id;

CREATE OR REPLACE VIEW agent_runtime.v_interrupt_pending AS
SELECT
    i.interrupt_id,
    i.run_id,
    r.session_id,
    i.node_id,
    i.kind,
    i.prompt,
    i.answer_schema,
    i.ui_schema,
    i.status,
    i.requested_at,
    i.expires_at,
    i.attempt,
    r.owner_sub
FROM agent_runtime.runtime_interrupt i
JOIN agent_runtime.runtime_run r ON r.run_id = i.run_id
WHERE i.status = 'pending';

CREATE OR REPLACE VIEW agent_runtime.v_session_state AS
SELECT
    st.session_id,
    st.agentic_workflow_id,
    st.flow,
    st.version,
    st.updated_at,
    s.owner_sub
FROM agent_runtime.runtime_session_state st
JOIN agent_runtime.runtime_session s ON s.session_id = st.session_id;

-- The TurnSaver row. format, langgraph_version and last_event_seq come from the body;
-- run_status and run_attempt let a resume check the fencing token without a second read.
CREATE OR REPLACE VIEW agent_runtime.v_checkpoint AS
SELECT
    c.run_id,
    c.attempt,
    c.kind,
    c.body,
    c.created_at,
    c.expires_at,
    r.owner_sub,
    c.body->>'format' AS format,
    c.body->>'langgraphVersion' AS langgraph_version,
    COALESCE((c.body->>'lastEventSeq')::bigint, 0) AS last_event_seq,
    r.status AS run_status,
    r.attempt AS run_attempt
FROM agent_runtime.runtime_checkpoint c
JOIN agent_runtime.runtime_run r ON r.run_id = c.run_id;

-- chunk_no 0 is the metadata row; chunks 1..n hold the body of database-store datasets.
CREATE OR REPLACE VIEW agent_runtime.v_dataset AS
SELECT
    d.dataset_id,
    d.chunk_no,
    d.run_id,
    d.session_id,
    d.node_id,
    d.visit,
    d.store,
    d.cache_key,
    d.kind,
    d.content_type,
    d.row_count,
    d.columns,
    d.size_bytes,
    d.sha256,
    d.body,
    d.created_at,
    d.expires_at,
    d.owner_sub
FROM agent_runtime.runtime_dataset d;

INSERT INTO agent_runtime.schema_migrations (version) VALUES ('004_views') ON CONFLICT (version) DO NOTHING;
