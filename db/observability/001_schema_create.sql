-- 001_schema_create.sql — agent_observability: Langfuse records in PostgreSQL (design Section 13.6).
--
-- Traces, observations (partitioned by month on start_time), scores, model prices and partition
-- bookkeeping, following the Langfuse data model so the same records could be sent to a Langfuse
-- server unchanged. Written only through sp_obs_ingest and the other procedures (005).
--
-- Idempotent: CREATE ... IF NOT EXISTS throughout.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS agent_observability;

CREATE TABLE IF NOT EXISTS agent_observability.schema_migrations (
    version     varchar(64)  PRIMARY KEY,
    applied_at  timestamptz  NOT NULL DEFAULT now(),
    checksum    varchar(64)  NOT NULL DEFAULT ''
);

-- One row per run; trace_id is the run ID as 32 hexadecimal characters.
CREATE TABLE IF NOT EXISTS agent_observability.obs_trace (
    trace_id            varchar(32)   PRIMARY KEY,
    run_id              uuid          NOT NULL UNIQUE,
    name                varchar(200)  NOT NULL DEFAULT '',
    environment         varchar(50)   NOT NULL DEFAULT 'default',
    release             varchar(50),
    version             varchar(64),
    session_id          uuid,
    user_id             varchar(128),
    tenant_id           varchar(64),
    agentic_workflow_id uuid,
    tags                text[]        NOT NULL DEFAULT '{}',
    metadata            jsonb         NOT NULL DEFAULT '{}'::jsonb,
    input               jsonb,
    output              jsonb,
    status              varchar(20),
    start_time          timestamptz   NOT NULL,
    end_time            timestamptz,
    latency_ms          integer GENERATED ALWAYS AS
                          (CASE WHEN end_time IS NULL THEN NULL
                                ELSE (EXTRACT(EPOCH FROM end_time - start_time) * 1000)::integer END) STORED,
    observation_count   integer       NOT NULL DEFAULT 0,
    error_count         integer       NOT NULL DEFAULT 0,
    input_tokens        bigint        NOT NULL DEFAULT 0,
    output_tokens       bigint        NOT NULL DEFAULT 0,
    total_cost          numeric(14,6) NOT NULL DEFAULT 0,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now()
);

-- One row per node visit, hook execution, model call, tool call, swarm member step and event.
CREATE TABLE IF NOT EXISTS agent_observability.obs_observation (
    observation_id        varchar(16)   NOT NULL,
    trace_id              varchar(32)   NOT NULL,
    parent_observation_id varchar(16),
    type                  varchar(20)   NOT NULL
                          CHECK (type IN ('span', 'generation', 'event', 'agent', 'tool', 'chain', 'retriever',
                                          'evaluator', 'embedding', 'guardrail')),
    name                  varchar(200)  NOT NULL,
    level                 varchar(10)   NOT NULL DEFAULT 'DEFAULT'
                          CHECK (level IN ('DEBUG', 'DEFAULT', 'WARNING', 'ERROR')),
    status_message        varchar(500),
    environment           varchar(50)   NOT NULL DEFAULT 'default',
    version               varchar(64),
    start_time            timestamptz   NOT NULL,
    end_time              timestamptz,
    completion_start_time timestamptz,
    latency_ms            integer,
    input                 jsonb,
    output                jsonb,
    metadata              jsonb         NOT NULL DEFAULT '{}'::jsonb,
    model                 varchar(200),
    model_parameters      jsonb,
    usage_details         jsonb,
    cost_details          jsonb,
    total_cost            numeric(14,6),
    node_id               varchar(100),
    node_type             varchar(50),
    node_name             varchar(200),
    node_kind             varchar(50),
    visit                 integer,
    path                  varchar(500),
    hook_binding_id       varchar(100),
    created_at            timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (observation_id, start_time)
) PARTITION BY RANGE (start_time);

CREATE TABLE IF NOT EXISTS agent_observability.obs_score (
    score_id        uuid          PRIMARY KEY,
    trace_id        varchar(32)   NOT NULL REFERENCES agent_observability.obs_trace (trace_id) ON DELETE CASCADE,
    observation_id  varchar(16),
    session_id      uuid,
    name            varchar(100)  NOT NULL,
    data_type       varchar(12)   NOT NULL CHECK (data_type IN ('NUMERIC', 'CATEGORICAL', 'BOOLEAN', 'CORRECTION', 'TEXT')),
    value           numeric,
    string_value    text,
    source          varchar(12)   NOT NULL CHECK (source IN ('API', 'EVAL', 'ANNOTATION')),
    comment         text,
    author_user_id  varchar(128),
    environment     varchar(50)   NOT NULL DEFAULT 'default',
    timestamp       timestamptz   NOT NULL DEFAULT now(),
    metadata        jsonb         NOT NULL DEFAULT '{}'::jsonb
);

-- Prices per token used to compute cost at ingestion; maintained by procedure, never in code.
CREATE TABLE IF NOT EXISTS agent_observability.obs_model_price (
    price_id            uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    model_pattern       varchar(200)   NOT NULL,
    usage_key           varchar(50)    NOT NULL,
    price_per_unit      numeric(18,12) NOT NULL,
    currency            char(3)        NOT NULL DEFAULT 'USD',
    valid_from          timestamptz    NOT NULL,
    valid_to            timestamptz,
    UNIQUE (model_pattern, usage_key, valid_from)
);

CREATE TABLE IF NOT EXISTS agent_observability.obs_partition (
    partition_name  varchar(63)  PRIMARY KEY,
    range_start     timestamptz  NOT NULL,
    range_end       timestamptz  NOT NULL,
    created_at      timestamptz  NOT NULL DEFAULT now()
);

ALTER TABLE agent_observability.obs_trace ALTER COLUMN input SET COMPRESSION lz4;
ALTER TABLE agent_observability.obs_trace ALTER COLUMN output SET COMPRESSION lz4;
ALTER TABLE agent_observability.obs_trace ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE agent_observability.obs_observation ALTER COLUMN input SET COMPRESSION lz4;
ALTER TABLE agent_observability.obs_observation ALTER COLUMN output SET COMPRESSION lz4;
ALTER TABLE agent_observability.obs_observation ALTER COLUMN metadata SET COMPRESSION lz4;

INSERT INTO agent_observability.schema_migrations (version) VALUES ('001_schema_create') ON CONFLICT (version) DO NOTHING;
