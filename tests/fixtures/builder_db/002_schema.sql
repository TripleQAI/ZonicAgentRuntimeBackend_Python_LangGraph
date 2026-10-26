-- 002_schema.sql — base tables.
--
-- THE TABLES ARE THE ONES THE DJANGO APP ALREADY CREATED, in the agent_builder
-- schema, with Django's naming convention `<app_label>_<model>`:
--
--     agent_builder."agentBuilder_agentworkflow"
--     agent_builder."agentBuilder_agentrelationship"
--     agent_builder."agentBuilder_agentworkflowchip"
--     agent_builder."agentBuilder_workflowsnapshot"
--     agent_builder."agentBuilder_nodetypedefinition"
--     agent_builder."agentBuilder_tooldefinition"
--
-- The names are mixed case, so they must be double-quoted everywhere: an
-- unquoted `agentBuilder_agentworkflow` folds to `agentbuilder_agentworkflow`
-- and does not exist. Those quotes appear only in this file, 003, 004, 007, 009
-- and 010 — the application never names a table, only the views and procedures
-- built on top of them (all lower case), so the convention is invisible above
-- the database.
--
-- On a database the Django app already built, every CREATE below is a no-op and
-- 007_adapt_existing_schema.sql does the real work. On a clean database these
-- statements produce the same shape Django would have.
--
-- Idempotent: safe to re-run.

SET search_path TO agent_builder, public;

-- --------------------------------------------------------------------------
-- schema_migrations — what is deployed here. Stamped by every file.
-- Ours, not Django's. The application reads it and warns; it never runs DDL.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder.schema_migrations (
    version     varchar(64)  PRIMARY KEY,
    applied_at  timestamptz  NOT NULL DEFAULT now(),
    checksum    varchar(64)  NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------------
-- agentBuilder_agentworkflow — the aggregate root.
--
-- nodes/edges/variables/interface/skills/publish_channels/security are
-- first-class columns AND are mirrored inside raw_json, which preserves the
-- complete original payload so unknown keys survive a round trip. The overlay
-- rule lives in v_agent_export (009) and fn_agent_workflow_upsert (010).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder."agentBuilder_agentworkflow" (
    id                          uuid          NOT NULL PRIMARY KEY,
    agentic_workflow_id         uuid          NOT NULL UNIQUE,

    bpc                         varchar(100)  NOT NULL DEFAULT '',
    name                        varchar(255)  NOT NULL,
    display_name                varchar(255)  NOT NULL DEFAULT '',
    description                 text          NOT NULL DEFAULT '',
    edit_as                     varchar(100)  NOT NULL DEFAULT '',
    invocation_type             varchar(100)  NOT NULL DEFAULT '',

    is_disabled                 boolean       NOT NULL DEFAULT false,
    is_available_for_migration  boolean       NOT NULL DEFAULT false,
    enable_for_auto_invoke      boolean       NOT NULL DEFAULT false,
    include_thoughts            boolean       NOT NULL DEFAULT false,

    app_id                      varchar(255)  NOT NULL DEFAULT '',
    module_id                   varchar(255)  NOT NULL DEFAULT '',
    environment                 varchar(100)  NOT NULL DEFAULT '',
    version_label               varchar(100)  NOT NULL DEFAULT '',

    is_root                     boolean       NOT NULL DEFAULT false,
    parent_package_id           uuid,

    nodes                       jsonb         NOT NULL DEFAULT '[]'::jsonb,
    edges                       jsonb         NOT NULL DEFAULT '[]'::jsonb,
    variables                   jsonb         NOT NULL DEFAULT '{}'::jsonb,
    interface                   jsonb         NOT NULL DEFAULT '{}'::jsonb,
    skills                      jsonb         NOT NULL DEFAULT '[]'::jsonb,
    publish_channels            jsonb         NOT NULL DEFAULT '[]'::jsonb,
    security                    jsonb         NOT NULL DEFAULT '{}'::jsonb,
    raw_json                    jsonb         NOT NULL DEFAULT '{}'::jsonb,

    semantic_search_enabled     boolean       NOT NULL DEFAULT false,
    semantic_domain             varchar(100)  NOT NULL DEFAULT '',
    semantic_keywords           text          NOT NULL DEFAULT '',
    semantic_use_cases          text          NOT NULL DEFAULT '',

    embedding_vector            jsonb,
    embedding_text              text          NOT NULL DEFAULT '',
    embedding_updated_at        timestamptz,

    created_at                  timestamptz   NOT NULL DEFAULT now(),
    updated_at                  timestamptz   NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- agentBuilder_agentrelationship — parent -> child edges derived from a canvas.
-- Rebuilt on every save; derivation happens in Python (plan §8.4).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder."agentBuilder_agentrelationship" (
    id                          uuid          NOT NULL PRIMARY KEY,

    parent_agent_id             uuid          NOT NULL
        REFERENCES agent_builder."agentBuilder_agentworkflow"(id) ON DELETE CASCADE,
    child_agent_id              uuid
        REFERENCES agent_builder."agentBuilder_agentworkflow"(id) ON DELETE SET NULL,

    parent_agentic_workflow_id  uuid          NOT NULL,
    child_agentic_workflow_id   uuid          NOT NULL,

    source_node_id              varchar(255)  NOT NULL DEFAULT '',
    source_node_name            varchar(255)  NOT NULL DEFAULT '',
    source_node_type            varchar(100)  NOT NULL DEFAULT 'workflow',
    relationship_type           varchar(100)  NOT NULL DEFAULT 'sub_flow',

    config_inputs               jsonb         NOT NULL DEFAULT '{}'::jsonb,
    agent_info                  jsonb         NOT NULL DEFAULT '{}'::jsonb,
    app_info                    jsonb         NOT NULL DEFAULT '{}'::jsonb,
    module_info                 jsonb         NOT NULL DEFAULT '{}'::jsonb,

    is_resolved                 boolean       NOT NULL DEFAULT false,
    validation_message          text          NOT NULL DEFAULT '',

    created_at                  timestamptz   NOT NULL DEFAULT now(),
    updated_at                  timestamptz   NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- agentBuilder_agentworkflowchip — denormalised label row, written with the agent.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder."agentBuilder_agentworkflowchip" (
    id                    uuid          NOT NULL PRIMARY KEY,
    agentic_workflow_id   uuid          NOT NULL UNIQUE,
    bpc                   varchar(100)  NOT NULL DEFAULT '',
    name                  varchar(255)  NOT NULL,
    display_name          varchar(255)  NOT NULL DEFAULT '',
    description           text          NOT NULL DEFAULT '',
    is_disabled           boolean       NOT NULL DEFAULT false
);

-- --------------------------------------------------------------------------
-- agentBuilder_workflowsnapshot — point-in-time copies.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder."agentBuilder_workflowsnapshot" (
    id                 uuid         NOT NULL PRIMARY KEY,
    agent_workflow_id  uuid         NOT NULL
        REFERENCES agent_builder."agentBuilder_agentworkflow"(id) ON DELETE CASCADE,
    snapshot_json      jsonb        NOT NULL,
    comment            text         NOT NULL DEFAULT '',
    created_at         timestamptz  NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- agentBuilder_nodetypedefinition — DB mirror of the JSON node registry.
-- The API prefers this table and falls back to the JSON resources when empty.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder."agentBuilder_nodetypedefinition" (
    id                   uuid          NOT NULL PRIMARY KEY,
    type                 varchar(100)  NOT NULL UNIQUE,
    label                varchar(255)  NOT NULL,
    icon                 varchar(100)  NOT NULL DEFAULT '',
    color                varchar(50)   NOT NULL DEFAULT '',
    description          text          NOT NULL DEFAULT '',
    default_name_prefix  varchar(100)  NOT NULL DEFAULT '',
    default_config       jsonb         NOT NULL DEFAULT '{}'::jsonb,
    handles              jsonb         NOT NULL DEFAULT '{}'::jsonb,
    form_schema          jsonb         NOT NULL DEFAULT '{}'::jsonb,
    output_schema        jsonb         NOT NULL DEFAULT '{}'::jsonb,
    validation_schema    jsonb         NOT NULL DEFAULT '{}'::jsonb,
    sort_order           integer       NOT NULL DEFAULT 0,
    is_active            boolean       NOT NULL DEFAULT true
);

-- --------------------------------------------------------------------------
-- agentBuilder_tooldefinition — tool catalogue for the picker.
-- Django's `choices` becomes a real CHECK constraint on a clean install; on an
-- existing table 007 adds it only if it is missing.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_builder."agentBuilder_tooldefinition" (
    id              uuid          NOT NULL PRIMARY KEY,
    name            varchar(255)  NOT NULL,
    title           varchar(255)  NOT NULL DEFAULT '',
    description     text          NOT NULL DEFAULT '',
    tool_type       varchar(50)   NOT NULL DEFAULT 'function',
    schema          jsonb         NOT NULL DEFAULT '{}'::jsonb,
    default_config  jsonb         NOT NULL DEFAULT '{}'::jsonb,
    is_active       boolean       NOT NULL DEFAULT true,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    updated_at      timestamptz   NOT NULL DEFAULT now()
);

INSERT INTO agent_builder.schema_migrations (version) VALUES ('002_schema')
    ON CONFLICT (version) DO NOTHING;
