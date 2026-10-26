-- 001_schema.sql — agent_runtime: tables (design Section 13.2).
--
-- Eight tables; no event, log or node-output tables (the per-node record is the Langfuse trace in
-- agent_observability). UUID keys generated in the database, timestamptz maintained by trigger
-- (002), jsonb documents, owner columns on every user-facing table.
--
-- Idempotent: CREATE ... IF NOT EXISTS throughout.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS agent_runtime;

CREATE TABLE IF NOT EXISTS agent_runtime.schema_migrations (
    version     varchar(64)  PRIMARY KEY,
    applied_at  timestamptz  NOT NULL DEFAULT now(),
    checksum    varchar(64)  NOT NULL DEFAULT ''
);

-- One conversation with one workflow. next_seq allocates message seq values per session.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_session (
    session_id           uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_sub            varchar(128)  NOT NULL,
    owner_email          varchar(320)  NOT NULL DEFAULT '',
    tenant_id            varchar(64)   NOT NULL DEFAULT '',
    agentic_workflow_id  uuid          NOT NULL,
    title                varchar(200)  NOT NULL DEFAULT '',
    status               varchar(16)   NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'archived')),
    next_seq             bigint        NOT NULL DEFAULT 0,
    metadata             jsonb         NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz   NOT NULL DEFAULT now(),
    updated_at           timestamptz   NOT NULL DEFAULT now(),
    last_message_at      timestamptz   NOT NULL DEFAULT now()
);

-- One execution of a workflow turn: its state and outcome, not its log; also the durable queue
-- entry of an async run (design Section 9.5). attempt is the fencing token of every write.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_run (
    run_id               uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id           uuid          NOT NULL REFERENCES agent_runtime.runtime_session (session_id) ON DELETE CASCADE,
    owner_sub            varchar(128)  NOT NULL,
    tenant_id            varchar(64)   NOT NULL DEFAULT '',
    agentic_workflow_id  uuid          NOT NULL,
    workflow_revision    varchar(64)   NOT NULL DEFAULT '',
    document_version     varchar(16)   NOT NULL DEFAULT '',
    status               varchar(16)   NOT NULL
                         CHECK (status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled', 'timed_out')),
    mode                 varchar(8)    NOT NULL CHECK (mode IN ('stream', 'sync', 'async')),
    input                jsonb         NOT NULL DEFAULT '{}'::jsonb,
    result               jsonb,
    error                jsonb,
    usage                jsonb         NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key      varchar(200),
    langfuse_trace_id    varchar(32),
    queued_at            timestamptz   NOT NULL DEFAULT now(),
    started_at           timestamptz,
    finished_at          timestamptz,
    deadline_at          timestamptz,
    priority             integer       NOT NULL DEFAULT 0,
    node_timeline        jsonb         NOT NULL DEFAULT '[]'::jsonb,
    lease_owner          varchar(200),
    lease_address        varchar(500),
    lease_expires_at     timestamptz,
    attempt              integer       NOT NULL DEFAULT 0,
    finish_hash          varchar(32),
    callback             jsonb,
    created_at           timestamptz   NOT NULL DEFAULT now(),
    updated_at           timestamptz   NOT NULL DEFAULT now()
);

-- The whole chat history, including agents' intermediate turns, with keyset paging on seq.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_message (
    message_id           uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id           uuid          NOT NULL REFERENCES agent_runtime.runtime_session (session_id) ON DELETE CASCADE,
    run_id               uuid          REFERENCES agent_runtime.runtime_run (run_id) ON DELETE SET NULL,
    node_id              varchar(100),
    visit                integer,
    seq                  bigint        NOT NULL,
    role                 varchar(16)   NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    kind                 varchar(16)   NOT NULL
                         CHECK (kind IN ('user_input', 'answer', 'agent_turn', 'tool_call', 'tool_result', 'handoff')),
    visibility           varchar(16)   NOT NULL DEFAULT 'chat' CHECK (visibility IN ('chat', 'internal')),
    thread               varchar(100)  NOT NULL DEFAULT 'messages',
    content_parts        jsonb         NOT NULL DEFAULT '[]'::jsonb,
    text_preview         varchar(2000) NOT NULL DEFAULT '',
    created_at           timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

-- The single row a paused run resumes from, or a long async turn restarts from (Section 8.2).
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_checkpoint (
    run_id               uuid          PRIMARY KEY REFERENCES agent_runtime.runtime_run (run_id) ON DELETE CASCADE,
    attempt              integer       NOT NULL,
    kind                 varchar(8)    NOT NULL CHECK (kind IN ('pause', 'async')),
    body                 jsonb         NOT NULL,
    created_at           timestamptz   NOT NULL DEFAULT now(),
    expires_at           timestamptz
);

-- Full data of database and HTTP nodes (Section 8.2). chunk_no 0 is the metadata row (always);
-- chunks 1..n hold the body for the database store only.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_dataset (
    dataset_id           uuid          NOT NULL,
    chunk_no             integer       NOT NULL DEFAULT 0 CHECK (chunk_no >= 0),
    run_id               uuid          NOT NULL REFERENCES agent_runtime.runtime_run (run_id) ON DELETE CASCADE,
    session_id           uuid          NOT NULL,
    node_id              varchar(100)  NOT NULL DEFAULT '',
    visit                integer       NOT NULL DEFAULT 1,
    owner_sub            varchar(128)  NOT NULL,
    store                varchar(8)    NOT NULL CHECK (store IN ('cache', 'database')),
    cache_key            varchar(500),
    kind                 varchar(16)   NOT NULL CHECK (kind IN ('rows', 'http_response', 'tool_result')),
    content_type         varchar(100)  NOT NULL DEFAULT 'application/json',
    row_count            bigint,
    columns              jsonb,
    size_bytes           bigint        NOT NULL DEFAULT 0,
    sha256               varchar(64)   NOT NULL DEFAULT '',
    body                 jsonb,
    created_at           timestamptz   NOT NULL DEFAULT now(),
    expires_at           timestamptz,
    PRIMARY KEY (dataset_id, chunk_no)
);

-- Human input and approvals.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_interrupt (
    interrupt_id         uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id               uuid          NOT NULL REFERENCES agent_runtime.runtime_run (run_id) ON DELETE CASCADE,
    node_id              varchar(100)  NOT NULL,
    kind                 varchar(16)   NOT NULL,
    prompt               jsonb         NOT NULL DEFAULT '{}'::jsonb,
    answer_schema        jsonb         NOT NULL DEFAULT '{}'::jsonb,
    ui_schema            jsonb         NOT NULL DEFAULT '{}'::jsonb,
    answer               jsonb,
    status               varchar(16)   NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'answered', 'expired', 'cancelled')),
    attempt              integer       NOT NULL DEFAULT 0,
    requested_at         timestamptz   NOT NULL DEFAULT now(),
    answered_at          timestamptz,
    answered_by          varchar(128),
    expires_at           timestamptz
);

-- Persistent variables between turns, with optimistic locking.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_session_state (
    session_id           uuid          NOT NULL REFERENCES agent_runtime.runtime_session (session_id) ON DELETE CASCADE,
    agentic_workflow_id  uuid          NOT NULL,
    flow                 jsonb         NOT NULL DEFAULT '{}'::jsonb,
    version              integer       NOT NULL DEFAULT 0,
    updated_at           timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, agentic_workflow_id)
);

-- User feedback shown in the UI; also sent to Langfuse as a trace score.
CREATE TABLE IF NOT EXISTS agent_runtime.runtime_feedback (
    feedback_id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id           uuid          NOT NULL REFERENCES agent_runtime.runtime_message (message_id) ON DELETE CASCADE,
    owner_sub            varchar(128)  NOT NULL,
    rating               smallint      NOT NULL CHECK (rating BETWEEN -1 AND 5),
    comment              text          NOT NULL DEFAULT '',
    created_at           timestamptz   NOT NULL DEFAULT now(),
    updated_at           timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (message_id, owner_sub)
);

-- Large documents compress with LZ4 (PostgreSQL 14 or later).
ALTER TABLE agent_runtime.runtime_checkpoint ALTER COLUMN body SET COMPRESSION lz4;
ALTER TABLE agent_runtime.runtime_dataset ALTER COLUMN body SET COMPRESSION lz4;
ALTER TABLE agent_runtime.runtime_message ALTER COLUMN content_parts SET COMPRESSION lz4;
ALTER TABLE agent_runtime.runtime_run ALTER COLUMN node_timeline SET COMPRESSION lz4;

INSERT INTO agent_runtime.schema_migrations (version) VALUES ('001_schema') ON CONFLICT (version) DO NOTHING;
