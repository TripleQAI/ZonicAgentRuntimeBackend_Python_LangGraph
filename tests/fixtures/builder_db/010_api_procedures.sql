-- 010_api_procedures.sql — THE WRITE SURFACE.
--
-- One procedure per atomic use case. Each is a single CALL, therefore a single
-- transaction: a save or an import cannot half-commit.
--
-- THE RULE (MIGRATION_PLAN.md §8.4): procedures persist a decision Python has
-- already made. No IF over workflow semantics lives here. Relationship
-- derivation, warnings, id generation and the raw_json overlay are all computed
-- in Python and arrive as arguments. The one lookup done here — resolving a
-- child agentic_workflow_id to its internal row — is a join, not a policy, and
-- doing it here is what makes the write atomic. Even its message text is passed
-- in as a format string so the wording stays in Python with all the others.
--
-- Payload convention: jsonb objects use SNAKE_CASE keys matching the table
-- columns, so jsonb_populate_record maps them with no per-column code and
-- absent keys keep their current value (PATCH semantics, matching the Django
-- view).
--
-- No dependency on column defaults. The Django-created tables have none (the
-- ORM applies them in Python), so every insert here supplies id, created_at and
-- updated_at explicitly and fills the remaining NOT NULL columns from
-- fn_agent_workflow_defaults(). 007 adds the defaults as well, but these
-- procedures work with or without it.
--
-- Idempotent: CREATE OR REPLACE throughout.

SET search_path TO agent_builder, public;

-- ==========================================================================
-- fn_agent_workflow_defaults — the value every NOT NULL column takes on a
-- freshly created row. One list, used by the insert path below.
-- ==========================================================================
CREATE OR REPLACE FUNCTION agent_builder.fn_agent_workflow_defaults()
RETURNS jsonb AS $$
    SELECT jsonb_build_object(
        'bpc', '', 'name', '', 'display_name', '', 'description', '',
        'edit_as', '', 'invocation_type', '',
        'is_disabled', false, 'is_available_for_migration', false,
        'enable_for_auto_invoke', false, 'include_thoughts', false,
        'app_id', '', 'module_id', '', 'environment', '', 'version_label', '',
        'is_root', false,
        'nodes', '[]'::jsonb, 'edges', '[]'::jsonb,
        'variables', '{}'::jsonb, 'interface', '{}'::jsonb,
        'skills', '[]'::jsonb, 'publish_channels', '[]'::jsonb,
        'security', '{}'::jsonb, 'raw_json', '{}'::jsonb,
        'semantic_search_enabled', false, 'semantic_domain', '',
        'semantic_keywords', '', 'semantic_use_cases', '',
        'embedding_text', ''
    );
$$ LANGUAGE sql IMMUTABLE;

-- ==========================================================================
-- fn_raw_json_overlay — the raw_json merge rule, in one place.
--
-- Two strategies, selected by which key the caller sent:
--   raw_json        -> replace wholesale (import); handled by jsonb_populate_record
--   raw_json_patch  -> merge onto the stored value, then re-overlay the
--                      first-class fields (save)
--
-- This is the same overlay v_agent_export applies on the way out, applied on
-- the way in. It is a merge rule, not a workflow decision.
-- ==========================================================================
CREATE OR REPLACE FUNCTION agent_builder.fn_raw_json_overlay(
    p_stored  jsonb,
    p_patch   jsonb,
    p_row     agent_builder."agentBuilder_agentworkflow"
) RETURNS jsonb AS $$
    SELECT COALESCE(p_stored, '{}'::jsonb)
        || COALESCE(p_patch, '{}'::jsonb)
        || jsonb_build_object(
               'nodes',                 p_row.nodes,
               'edges',                 p_row.edges,
               'variables',             p_row.variables,
               'interface',             p_row.interface,
               'skills',                p_row.skills,
               'publishChannels',       p_row.publish_channels,
               'security',              p_row.security,
               'semanticSearchEnabled', p_row.semantic_search_enabled,
               'semanticDomain',        p_row.semantic_domain,
               'semanticKeywords',      p_row.semantic_keywords,
               'semanticUseCases',      p_row.semantic_use_cases
           );
$$ LANGUAGE sql IMMUTABLE;

-- ==========================================================================
-- fn_agent_workflow_upsert — upsert one agent + its chip row.
-- Returns the internal agentBuilder_agentworkflow.id.
-- ==========================================================================
CREATE OR REPLACE FUNCTION agent_builder.fn_agent_workflow_upsert(p_agent jsonb)
RETURNS uuid AS $$
DECLARE
    v_wf_id uuid := (p_agent->>'agentic_workflow_id')::uuid;
    v_cur   agent_builder."agentBuilder_agentworkflow";
    v_new   agent_builder."agentBuilder_agentworkflow";
BEGIN
    IF v_wf_id IS NULL THEN
        RAISE EXCEPTION 'agent payload is missing agentic_workflow_id';
    END IF;

    SELECT * INTO v_cur
    FROM agent_builder."agentBuilder_agentworkflow"
    WHERE agentic_workflow_id = v_wf_id;

    IF NOT FOUND THEN
        -- New row: start from the defaults, apply the payload, then write the
        -- whole record. `SELECT (v_new).*` avoids repeating the column list.
        v_new := jsonb_populate_record(
            NULL::agent_builder."agentBuilder_agentworkflow",
            agent_builder.fn_agent_workflow_defaults() || p_agent
        );
        v_new.id                  := gen_random_uuid();
        v_new.agentic_workflow_id := v_wf_id;
        v_new.created_at          := now();
        v_new.updated_at          := now();

        IF p_agent ? 'raw_json_patch' THEN
            v_new.raw_json := agent_builder.fn_raw_json_overlay(
                '{}'::jsonb, p_agent->'raw_json_patch', v_new);
        END IF;

        INSERT INTO agent_builder."agentBuilder_agentworkflow" SELECT (v_new).*;
    ELSE
        -- Ownership (006_ownership.sql). A row another user owns is refused; the
        -- builder maps this error to "not found". Class 23, so the gateway reports
        -- 409 rather than a generic query error.
        IF v_cur.owner_sub IS NOT NULL
           AND v_cur.owner_sub IS DISTINCT FROM (p_agent->>'owner_sub') THEN
            RAISE EXCEPTION 'owner_mismatch: workflow % belongs to another owner', v_wf_id
                USING ERRCODE = '23O01';
        END IF;

        -- Existing row: base = current, patch = payload. Keys absent from the
        -- payload keep their stored value — the PATCH semantic the Django PUT
        -- had.
        v_new := jsonb_populate_record(v_cur, p_agent);

        -- Identity and creation time are never patchable. The owner is set once:
        -- on insert, or here when a row written before 006 is claimed.
        v_new.id          := v_cur.id;
        v_new.created_at  := v_cur.created_at;
        v_new.updated_at  := now();
        v_new.owner_sub   := COALESCE(v_cur.owner_sub, p_agent->>'owner_sub');
        v_new.owner_email := COALESCE(p_agent->>'owner_email', v_cur.owner_email);

        IF p_agent ? 'raw_json_patch' THEN
            v_new.raw_json := agent_builder.fn_raw_json_overlay(
                COALESCE(v_cur.raw_json, '{}'::jsonb), p_agent->'raw_json_patch', v_new);
        END IF;

        UPDATE agent_builder."agentBuilder_agentworkflow" SET
            bpc                        = v_new.bpc,
            name                       = v_new.name,
            display_name               = v_new.display_name,
            description                = v_new.description,
            edit_as                    = v_new.edit_as,
            invocation_type            = v_new.invocation_type,
            is_disabled                = v_new.is_disabled,
            is_available_for_migration = v_new.is_available_for_migration,
            enable_for_auto_invoke     = v_new.enable_for_auto_invoke,
            include_thoughts           = v_new.include_thoughts,
            app_id                     = v_new.app_id,
            module_id                  = v_new.module_id,
            environment                = v_new.environment,
            version_label              = v_new.version_label,
            is_root                    = v_new.is_root,
            parent_package_id          = v_new.parent_package_id,
            nodes                      = v_new.nodes,
            edges                      = v_new.edges,
            variables                  = v_new.variables,
            interface                  = v_new.interface,
            skills                     = v_new.skills,
            publish_channels           = v_new.publish_channels,
            security                   = v_new.security,
            raw_json                   = v_new.raw_json,
            semantic_search_enabled    = v_new.semantic_search_enabled,
            semantic_domain            = v_new.semantic_domain,
            semantic_keywords          = v_new.semantic_keywords,
            semantic_use_cases         = v_new.semantic_use_cases,
            owner_sub                  = v_new.owner_sub,
            owner_email                = v_new.owner_email,
            updated_at                 = v_new.updated_at
        WHERE id = v_cur.id;
    END IF;

    -- Chip: the denormalised label row, written in the same transaction.
    INSERT INTO agent_builder."agentBuilder_agentworkflowchip" AS c
        (id, agentic_workflow_id, bpc, name, display_name, description, is_disabled)
    VALUES (gen_random_uuid(), v_wf_id, v_new.bpc, v_new.name, v_new.display_name,
            v_new.description, v_new.is_disabled)
    ON CONFLICT (agentic_workflow_id) DO UPDATE SET
        bpc          = EXCLUDED.bpc,
        name         = EXCLUDED.name,
        display_name = EXCLUDED.display_name,
        description  = EXCLUDED.description,
        is_disabled  = EXCLUDED.is_disabled;

    RETURN v_new.id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- ==========================================================================
-- fn_agent_relationship_replace — delete and reinsert one parent's edges.
--
-- p_relationships is an array of objects shaped like the relationship table,
-- plus two flags that say how much of the answer the caller already knows:
--
--   resolve     true  -> look the child up; the join decides is_resolved and
--                        the validation message (a PUT, which resolves against
--                        the database)
--               false -> the caller supplied is_resolved and the message (an
--                        import, which resolves against the package)
--   link_child  true  -> populate child_agent_id from the lookup, but only when
--                        the row ends up resolved
--               false -> never link (handoff edges point at a node inside the
--                        same workflow, so their child id is the parent's own)
--
-- Children resolve only among the parent owner's workflows (006_ownership.sql):
-- a workflow ID that belongs to another user stays unresolved.
-- ==========================================================================
CREATE OR REPLACE FUNCTION agent_builder.fn_agent_relationship_replace(
    p_parent_id        uuid,
    p_parent_wf_id     uuid,
    p_relationships    jsonb,
    p_unresolved_fmt   text DEFAULT 'Child workflow %s not found in database'
) RETURNS integer AS $$
DECLARE
    v_count integer;
    v_owner varchar(64);
BEGIN
    SELECT owner_sub INTO v_owner
    FROM agent_builder."agentBuilder_agentworkflow"
    WHERE id = p_parent_id;

    -- SQL NULL means "the caller sent no nodes, so the canvas is unchanged and
    -- the stored edges still describe it" — leave them untouched and report
    -- what is there. That is what the Django view did: it rebuilt from the
    -- PERSISTED nodes, so a partial save recomputed the same set.
    --
    -- An empty array is different, and means "replace with none". Note that
    -- SQL NULL and JSON null are not the same thing here: 'null'::jsonb IS NULL
    -- is false, so both are checked.
    IF p_relationships IS NULL OR jsonb_typeof(p_relationships) = 'null' THEN
        SELECT count(*) INTO v_count
        FROM agent_builder."agentBuilder_agentrelationship"
        WHERE parent_agent_id = p_parent_id;
        RETURN v_count;
    END IF;

    IF jsonb_typeof(p_relationships) <> 'array' THEN
        RAISE EXCEPTION 'p_relationships must be a JSON array or null, got %',
            jsonb_typeof(p_relationships);
    END IF;

    DELETE FROM agent_builder."agentBuilder_agentrelationship"
    WHERE parent_agent_id = p_parent_id;

    WITH incoming AS (
        SELECT
            r.*,
            c.id AS found_child_id,
            CASE WHEN r.resolve THEN (c.id IS NOT NULL)
                 ELSE COALESCE(r.is_resolved, false) END AS resolved
        FROM jsonb_to_recordset(p_relationships) AS r(
            child_agentic_workflow_id uuid,
            source_node_id            text,
            source_node_name          text,
            source_node_type          text,
            relationship_type         text,
            config_inputs             jsonb,
            agent_info                jsonb,
            app_info                  jsonb,
            module_info               jsonb,
            is_resolved               boolean,
            validation_message        text,
            resolve                   boolean,
            link_child                boolean
        )
        LEFT JOIN agent_builder."agentBuilder_agentworkflow" c
               ON c.agentic_workflow_id = r.child_agentic_workflow_id
              AND c.owner_sub IS NOT DISTINCT FROM v_owner
    )
    INSERT INTO agent_builder."agentBuilder_agentrelationship" (
        id, parent_agent_id, child_agent_id,
        parent_agentic_workflow_id, child_agentic_workflow_id,
        source_node_id, source_node_name, source_node_type, relationship_type,
        config_inputs, agent_info, app_info, module_info,
        is_resolved, validation_message, created_at, updated_at
    )
    SELECT
        gen_random_uuid(),
        p_parent_id,
        CASE WHEN i.link_child AND i.resolved THEN i.found_child_id ELSE NULL END,
        p_parent_wf_id,
        i.child_agentic_workflow_id,
        COALESCE(i.source_node_id, ''),
        COALESCE(i.source_node_name, ''),
        COALESCE(i.source_node_type, 'workflow'),
        COALESCE(i.relationship_type, 'sub_flow'),
        COALESCE(i.config_inputs, '{}'::jsonb),
        COALESCE(i.agent_info, '{}'::jsonb),
        COALESCE(i.app_info, '{}'::jsonb),
        COALESCE(i.module_info, '{}'::jsonb),
        i.resolved,
        CASE WHEN i.resolve AND NOT i.resolved
             THEN format(p_unresolved_fmt, i.child_agentic_workflow_id)
             ELSE COALESCE(i.validation_message, '') END,
        now(),
        now()
    FROM incoming i;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- ==========================================================================
-- sp_agent_workflow_save — PUT /agents/{id}.
-- Agent + chip + relationships, one transaction.
-- INOUT parameters are passed as NULL placeholders by the caller and come back
-- as a single row (the gateway's documented procedure contract).
-- ==========================================================================
CREATE OR REPLACE PROCEDURE agent_builder.sp_agent_workflow_save(
    p_agent            jsonb,
    p_relationships    jsonb,
    p_unresolved_fmt   text,
    INOUT o_relationship_count integer DEFAULT NULL,
    INOUT o_node_count         integer DEFAULT NULL,
    INOUT o_edge_count         integer DEFAULT NULL
) AS $$
DECLARE
    v_id    uuid;
    v_wf_id uuid := (p_agent->>'agentic_workflow_id')::uuid;
BEGIN
    v_id := agent_builder.fn_agent_workflow_upsert(p_agent);
    o_relationship_count := agent_builder.fn_agent_relationship_replace(
        v_id, v_wf_id, p_relationships,
        COALESCE(p_unresolved_fmt, 'Child workflow %s not found in database'));

    SELECT
        CASE WHEN jsonb_typeof(nodes) = 'array' THEN jsonb_array_length(nodes) ELSE 0 END,
        CASE WHEN jsonb_typeof(edges) = 'array' THEN jsonb_array_length(edges) ELSE 0 END
    INTO o_node_count, o_edge_count
    FROM agent_builder."agentBuilder_agentworkflow" WHERE id = v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- ==========================================================================
-- sp_agent_package_import — POST /agents/import-package.
--
-- Root + every child + every relationship, one transaction. A failed import
-- leaves nothing behind; the Django implementation could leave a partially
-- imported package.
--
-- Children are written BEFORE the root's relationships so the resolve join in
-- fn_agent_relationship_replace can see them.
-- ==========================================================================
CREATE OR REPLACE PROCEDURE agent_builder.sp_agent_package_import(
    p_root           jsonb,
    p_sub_agents     jsonb,
    p_relationships  jsonb,
    p_unresolved_fmt text,
    INOUT o_root_id            uuid    DEFAULT NULL,
    INOUT o_sub_agent_count    integer DEFAULT NULL,
    INOUT o_relationship_count integer DEFAULT NULL
) AS $$
DECLARE
    v_root_id uuid;
    v_child   jsonb;
    v_count   integer := 0;
BEGIN
    IF p_sub_agents IS NOT NULL AND jsonb_typeof(p_sub_agents) = 'array' THEN
        FOR v_child IN SELECT * FROM jsonb_array_elements(p_sub_agents) LOOP
            PERFORM agent_builder.fn_agent_workflow_upsert(v_child);
            v_count := v_count + 1;
        END LOOP;
    END IF;

    v_root_id := agent_builder.fn_agent_workflow_upsert(p_root);

    o_root_id            := (p_root->>'agentic_workflow_id')::uuid;
    o_sub_agent_count    := v_count;
    o_relationship_count := agent_builder.fn_agent_relationship_replace(
        v_root_id, o_root_id, p_relationships,
        COALESCE(p_unresolved_fmt, 'Child workflow %s not found in database'));
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- ==========================================================================
-- sp_workflow_snapshot_create — POST /agents/{id}/snapshots.
-- The snapshot body is built from the persisted row, not from the request, so
-- a snapshot always reflects what is actually stored.
-- ==========================================================================
CREATE OR REPLACE PROCEDURE agent_builder.sp_workflow_snapshot_create(
    p_agentic_workflow_id uuid,
    p_comment             text,
    INOUT o_snapshot_id   uuid        DEFAULT NULL,
    INOUT o_created_at    timestamptz DEFAULT NULL
) AS $$
DECLARE
    v_agent agent_builder."agentBuilder_agentworkflow";
BEGIN
    SELECT * INTO v_agent
    FROM agent_builder."agentBuilder_agentworkflow"
    WHERE agentic_workflow_id = p_agentic_workflow_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent % not found', p_agentic_workflow_id
            USING ERRCODE = 'no_data_found';
    END IF;

    INSERT INTO agent_builder."agentBuilder_workflowsnapshot"
        (id, agent_workflow_id, snapshot_json, comment, created_at)
    VALUES (
        gen_random_uuid(),
        v_agent.id,
        v_agent.raw_json || jsonb_build_object('nodes', v_agent.nodes,
                                               'edges', v_agent.edges),
        COALESCE(p_comment, ''),
        now()
    )
    RETURNING id, created_at INTO o_snapshot_id, o_created_at;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- ==========================================================================
-- sp_agent_embedding_store — POST /agents/{id}/embed.
-- ==========================================================================
CREATE OR REPLACE PROCEDURE agent_builder.sp_agent_embedding_store(
    p_agentic_workflow_id uuid,
    p_vector              jsonb,
    p_text                text,
    INOUT o_dimensions    integer DEFAULT NULL
) AS $$
BEGIN
    UPDATE agent_builder."agentBuilder_agentworkflow" SET
        embedding_vector     = p_vector,
        embedding_text       = COALESCE(p_text, ''),
        embedding_updated_at = now(),
        updated_at           = now()
    WHERE agentic_workflow_id = p_agentic_workflow_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent % not found', p_agentic_workflow_id
            USING ERRCODE = 'no_data_found';
    END IF;

    o_dimensions := CASE WHEN jsonb_typeof(p_vector) = 'array'
                         THEN jsonb_array_length(p_vector) ELSE 0 END;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- ==========================================================================
-- sp_node_type_upsert / sp_tool_upsert — registry seeding.
-- The whole registry arrives as one jsonb array: one round trip, not one per
-- node type.
-- ==========================================================================
CREATE OR REPLACE PROCEDURE agent_builder.sp_node_type_upsert(
    p_defs jsonb,
    INOUT o_count integer DEFAULT NULL
) AS $$
BEGIN
    INSERT INTO agent_builder."agentBuilder_nodetypedefinition" (
        id, type, label, icon, color, description, default_name_prefix,
        default_config, handles, form_schema, output_schema, validation_schema,
        sort_order, is_active
    )
    SELECT
        gen_random_uuid(),
        d.type, d.label, COALESCE(d.icon, ''), COALESCE(d.color, ''),
        COALESCE(d.description, ''), COALESCE(d.default_name_prefix, ''),
        COALESCE(d.default_config, '{}'::jsonb), COALESCE(d.handles, '{}'::jsonb),
        COALESCE(d.form_schema, '{}'::jsonb), COALESCE(d.output_schema, '{}'::jsonb),
        COALESCE(d.validation_schema, '{}'::jsonb), COALESCE(d.sort_order, 0), true
    FROM jsonb_to_recordset(p_defs) AS d(
        type text, label text, icon text, color text, description text,
        default_name_prefix text, default_config jsonb, handles jsonb,
        form_schema jsonb, output_schema jsonb, validation_schema jsonb,
        sort_order integer
    )
    ON CONFLICT (type) DO UPDATE SET
        label               = EXCLUDED.label,
        icon                = EXCLUDED.icon,
        color               = EXCLUDED.color,
        description         = EXCLUDED.description,
        default_name_prefix = EXCLUDED.default_name_prefix,
        default_config      = EXCLUDED.default_config,
        handles             = EXCLUDED.handles,
        form_schema         = EXCLUDED.form_schema,
        output_schema       = EXCLUDED.output_schema,
        validation_schema   = EXCLUDED.validation_schema,
        sort_order          = EXCLUDED.sort_order,
        is_active           = true;

    GET DIAGNOSTICS o_count = ROW_COUNT;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

CREATE OR REPLACE PROCEDURE agent_builder.sp_tool_upsert(
    p_defs jsonb,
    INOUT o_count integer DEFAULT NULL
) AS $$
BEGIN
    INSERT INTO agent_builder."agentBuilder_tooldefinition"
        (id, name, title, description, tool_type, schema, default_config,
         is_active, created_at, updated_at)
    SELECT
        gen_random_uuid(),
        d.name, COALESCE(d.title, ''), COALESCE(d.description, ''),
        COALESCE(d.tool_type, 'function'), COALESCE(d.schema, '{}'::jsonb),
        COALESCE(d.default_config, '{}'::jsonb), true, now(), now()
    FROM jsonb_to_recordset(p_defs) AS d(
        name text, title text, description text, tool_type text,
        schema jsonb, default_config jsonb
    )
    ON CONFLICT (name) DO UPDATE SET
        title          = EXCLUDED.title,
        description    = EXCLUDED.description,
        tool_type      = EXCLUDED.tool_type,
        schema         = EXCLUDED.schema,
        default_config = EXCLUDED.default_config,
        is_active      = true,
        updated_at     = now();

    GET DIAGNOSTICS o_count = ROW_COUNT;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

INSERT INTO agent_builder.schema_migrations (version) VALUES ('010_api_procedures')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
