-- 009_api_views.sql — THE READ SURFACE.
--
-- Every read the application performs names one of these views. The gateway's
-- ViewIn payload cannot join or express OR, so all shaping happens here
-- (MIGRATION_PLAN.md §8.9). Views contain shaping, never policy.
--
-- They also encapsulate the Django table names: the application asks for
-- `v_agent_summary` in schema `agent_builder` and never has to know it is
-- backed by "agentBuilder_agentworkflow".
--
-- Per-user views end with owner_sub (parent_owner_sub for relationships), added
-- by 006_ownership.sql; the repositories filter every read by it. The column
-- comes last because CREATE OR REPLACE VIEW can only append columns.
--
-- Idempotent: CREATE OR REPLACE throughout.

SET search_path TO agent_builder, public;

-- --------------------------------------------------------------------------
-- v_agent_summary — the agent list and the Agent Explorer.
-- Node/edge counts are computed in SQL instead of len() per row in Python.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_agent_summary AS
SELECT
    a.agentic_workflow_id,
    a.name,
    a.display_name,
    a.is_root,
    a.environment,
    a.updated_at,
    CASE WHEN jsonb_typeof(a.nodes) = 'array' THEN jsonb_array_length(a.nodes) ELSE 0 END
        AS node_count,
    CASE WHEN jsonb_typeof(a.edges) = 'array' THEN jsonb_array_length(a.edges) ELSE 0 END
        AS edge_count,
    a.owner_sub
FROM agent_builder."agentBuilder_agentworkflow" a;

-- --------------------------------------------------------------------------
-- v_agent_detail — GET /agents/{id}.
--
-- detail_json reproduces the Django view exactly: raw_json, with the
-- first-class columns and the four semantic fields overlaid on top.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_agent_detail AS
SELECT
    a.agentic_workflow_id,
    a.name,
    a.display_name,
    a.description,
    a.bpc,
    a.is_root,
    a.parent_package_id,
    a.semantic_search_enabled,
    a.updated_at,
    -- COALESCE is not decoration: `NULL || anything` is NULL in jsonb, so a
    -- single null raw_json would blank the entire document rather than
    -- degrade one field. The columns are NOT NULL under Django, but this view
    -- also runs over migrated data, where that is an assumption rather than a
    -- guarantee.
    COALESCE(a.raw_json, '{}'::jsonb)
        || jsonb_build_object(
               'agenticWorkflowId',     a.agentic_workflow_id,
               'nodes',                 COALESCE(a.nodes, '[]'::jsonb),
               'edges',                 COALESCE(a.edges, '[]'::jsonb),
               'variables',             COALESCE(a.variables, '{}'::jsonb),
               'interface',             COALESCE(a.interface, '{}'::jsonb),
               'skills',                COALESCE(a.skills, '[]'::jsonb),
               'publishChannels',       COALESCE(a.publish_channels, '[]'::jsonb),
               'security',              COALESCE(a.security, '{}'::jsonb),
               'semanticSearchEnabled', COALESCE(a.semantic_search_enabled, false),
               'semanticDomain',        COALESCE(a.semantic_domain, ''),
               'semanticKeywords',      COALESCE(a.semantic_keywords, ''),
               'semanticUseCases',      COALESCE(a.semantic_use_cases, '')
           ) AS detail_json,
    a.owner_sub
FROM agent_builder."agentBuilder_agentworkflow" a;

-- --------------------------------------------------------------------------
-- v_agent_export — THE raw_json overlay contract, expressed once.
--
-- export_single() in the Django app starts from raw_json and overwrites the
-- seven first-class fields. Both /export and /export-package read this view,
-- so the rule cannot drift between them.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_agent_export AS
SELECT
    a.agentic_workflow_id,
    a.parent_package_id,
    a.is_root,
    COALESCE(a.raw_json, '{}'::jsonb)
        || jsonb_build_object(
               'nodes',           COALESCE(a.nodes, '[]'::jsonb),
               'edges',           COALESCE(a.edges, '[]'::jsonb),
               'variables',       COALESCE(a.variables, '{}'::jsonb),
               'interface',       COALESCE(a.interface, '{}'::jsonb),
               'skills',          COALESCE(a.skills, '[]'::jsonb),
               'publishChannels', COALESCE(a.publish_channels, '[]'::jsonb),
               'security',        COALESCE(a.security, '{}'::jsonb)
           ) AS export_json,
    a.owner_sub
FROM agent_builder."agentBuilder_agentworkflow" a;

-- --------------------------------------------------------------------------
-- v_agent_relationship_detail — the relationship map in ONE read.
-- The Django implementation issues one extra query per child.
-- A child's details show only when it has the parent's owner, so a link made
-- before 006_ownership cannot reveal another user's workflow.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_agent_relationship_detail AS
SELECT
    r.id,
    r.parent_agentic_workflow_id,
    r.child_agentic_workflow_id,
    r.source_node_id,
    r.source_node_name,
    r.source_node_type,
    r.relationship_type,
    r.config_inputs,
    r.is_resolved,
    r.validation_message,
    c.name              AS child_name,
    c.display_name      AS child_display_name,
    CASE WHEN c.id IS NULL THEN 0
         WHEN jsonb_typeof(c.nodes) = 'array' THEN jsonb_array_length(c.nodes)
         ELSE 0 END     AS child_node_count,
    CASE WHEN c.id IS NULL THEN 0
         WHEN jsonb_typeof(c.edges) = 'array' THEN jsonb_array_length(c.edges)
         ELSE 0 END     AS child_edge_count,
    p.owner_sub         AS parent_owner_sub
FROM agent_builder."agentBuilder_agentrelationship" r
JOIN agent_builder."agentBuilder_agentworkflow" p ON p.id = r.parent_agent_id
LEFT JOIN agent_builder."agentBuilder_agentworkflow" c
       ON c.id = r.child_agent_id
      AND c.owner_sub IS NOT DISTINCT FROM p.owner_sub;

-- --------------------------------------------------------------------------
-- v_package_agent — package membership, used by export-debug and the
-- "unreferenced sub-agents" warning.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_package_agent AS
SELECT
    a.parent_package_id,
    a.agentic_workflow_id,
    a.name,
    a.display_name,
    a.is_root,
    a.owner_sub
FROM agent_builder."agentBuilder_agentworkflow" a
WHERE a.parent_package_id IS NOT NULL;

-- --------------------------------------------------------------------------
-- Registry reads.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_node_type_active AS
SELECT
    n.type,
    n.label,
    n.icon,
    n.color,
    n.description,
    n.default_name_prefix,
    n.default_config,
    n.handles,
    n.form_schema,
    n.output_schema,
    n.validation_schema,
    n.sort_order
FROM agent_builder."agentBuilder_nodetypedefinition" n
WHERE n.is_active;

CREATE OR REPLACE VIEW agent_builder.v_tool_active AS
SELECT
    t.name,
    t.title,
    t.description,
    t.tool_type,
    t.schema,
    t.default_config
FROM agent_builder."agentBuilder_tooldefinition" t
WHERE t.is_active;

-- --------------------------------------------------------------------------
-- v_schema_migration — lets the app report what is deployed. It warns; it
-- never migrates.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW agent_builder.v_schema_migration AS
SELECT version, applied_at, checksum FROM agent_builder.schema_migrations;

INSERT INTO agent_builder.schema_migrations (version) VALUES ('009_api_views')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
