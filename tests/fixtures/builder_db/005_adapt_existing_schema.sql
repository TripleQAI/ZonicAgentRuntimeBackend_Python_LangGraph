-- 005_adapt_existing_schema.sql — make the Django-created tables usable by
-- this service. Additive and non-destructive: no column is dropped, no row is
-- touched, and Django keeps working unchanged.
--
-- Why it is needed. Django applies field defaults in Python, so the tables it
-- creates have NO column defaults at all, and `auto_now` timestamps are set by
-- the ORM rather than by the database. Anything writing outside Django — these
-- procedures, psql, a report job — has to supply every NOT NULL column by hand
-- or the insert fails.
--
-- Two independent guards are applied:
--   1. The procedures in 010 supply id, created_at and updated_at explicitly,
--      so they work whether or not this file has been run.
--   2. This file adds the defaults anyway, so a hand-written INSERT behaves.
--
-- It also verifies, loudly, that the tables actually have the columns this
-- project expects. `CREATE TABLE IF NOT EXISTS` in 002 silently skips a table
-- that already exists, so a shape mismatch would otherwise surface as a
-- confusing runtime error much later.

SET search_path TO agent_builder, public;

-- ==========================================================================
-- 1. Verify the shape before changing anything.
-- ==========================================================================
DO $$
DECLARE
    v_expected CONSTANT text[][] := ARRAY[
        -- The later Django migrations first: those are the columns most likely
        -- to be absent if the migration history was not fully applied before
        -- the data was moved.
        ['agentBuilder_agentworkflow',    'semantic_search_enabled'],   -- 0002
        ['agentBuilder_agentworkflow',    'semantic_domain'],           -- 0002
        ['agentBuilder_agentworkflow',    'semantic_keywords'],         -- 0002
        ['agentBuilder_agentworkflow',    'semantic_use_cases'],        -- 0002
        ['agentBuilder_agentworkflow',    'embedding_vector'],          -- 0003
        ['agentBuilder_agentworkflow',    'embedding_text'],            -- 0003
        ['agentBuilder_agentworkflow',    'embedding_updated_at'],      -- 0003
        ['agentBuilder_agentworkflowchip','is_disabled'],               -- 0005
        -- Then the columns the read and write surfaces depend on.
        ['agentBuilder_agentworkflow',    'agentic_workflow_id'],
        ['agentBuilder_agentworkflow',    'raw_json'],
        ['agentBuilder_agentworkflow',    'parent_package_id'],
        ['agentBuilder_agentworkflow',    'nodes'],
        ['agentBuilder_agentworkflow',    'edges'],
        ['agentBuilder_agentworkflow',    'publish_channels'],
        ['agentBuilder_agentworkflow',    'version_label'],
        ['agentBuilder_agentworkflow',    'created_at'],
        ['agentBuilder_agentworkflow',    'updated_at'],
        ['agentBuilder_agentrelationship','parent_agent_id'],
        ['agentBuilder_agentrelationship','child_agent_id'],
        ['agentBuilder_agentrelationship','is_resolved'],
        ['agentBuilder_agentrelationship','validation_message'],
        ['agentBuilder_agentworkflowchip','agentic_workflow_id'],
        ['agentBuilder_workflowsnapshot', 'agent_workflow_id'],
        ['agentBuilder_workflowsnapshot', 'snapshot_json'],
        ['agentBuilder_nodetypedefinition','form_schema'],
        ['agentBuilder_nodetypedefinition','validation_schema'],
        ['agentBuilder_tooldefinition',   'tool_type'],
        ['agentBuilder_tooldefinition',   'schema']
    ];
    v_missing text := '';
    i integer;
BEGIN
    FOR i IN 1 .. array_length(v_expected, 1) LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'agent_builder'
              AND table_name   = v_expected[i][1]
              AND column_name  = v_expected[i][2]
        ) THEN
            v_missing := v_missing || format('  %s.%s%s', v_expected[i][1],
                                             v_expected[i][2], chr(10));
        END IF;
    END LOOP;

    IF v_missing <> '' THEN
        RAISE EXCEPTION
            'agent_builder schema is missing columns this service requires:%s%s',
            chr(10), v_missing
            USING HINT = 'Run the Django migrations first, or apply 002_schema.sql '
                         'to a clean database.';
    END IF;

    RAISE NOTICE 'agent_builder schema verified.';
END;
$$;

-- ==========================================================================
-- 2. Column defaults, so a write that omits a column still succeeds.
--    ALTER ... SET DEFAULT does not rewrite the table and does not touch
--    existing rows.
-- ==========================================================================
ALTER TABLE agent_builder."agentBuilder_agentworkflow"
    ALTER COLUMN id                         SET DEFAULT gen_random_uuid(),
    ALTER COLUMN bpc                        SET DEFAULT '',
    ALTER COLUMN display_name               SET DEFAULT '',
    ALTER COLUMN description                SET DEFAULT '',
    ALTER COLUMN edit_as                    SET DEFAULT '',
    ALTER COLUMN invocation_type            SET DEFAULT '',
    ALTER COLUMN is_disabled                SET DEFAULT false,
    ALTER COLUMN is_available_for_migration SET DEFAULT false,
    ALTER COLUMN enable_for_auto_invoke     SET DEFAULT false,
    ALTER COLUMN include_thoughts           SET DEFAULT false,
    ALTER COLUMN app_id                     SET DEFAULT '',
    ALTER COLUMN module_id                  SET DEFAULT '',
    ALTER COLUMN environment                SET DEFAULT '',
    ALTER COLUMN version_label              SET DEFAULT '',
    ALTER COLUMN is_root                    SET DEFAULT false,
    ALTER COLUMN nodes                      SET DEFAULT '[]'::jsonb,
    ALTER COLUMN edges                      SET DEFAULT '[]'::jsonb,
    ALTER COLUMN variables                  SET DEFAULT '{}'::jsonb,
    ALTER COLUMN interface                  SET DEFAULT '{}'::jsonb,
    ALTER COLUMN skills                     SET DEFAULT '[]'::jsonb,
    ALTER COLUMN publish_channels           SET DEFAULT '[]'::jsonb,
    ALTER COLUMN security                   SET DEFAULT '{}'::jsonb,
    ALTER COLUMN raw_json                   SET DEFAULT '{}'::jsonb,
    ALTER COLUMN semantic_search_enabled    SET DEFAULT false,
    ALTER COLUMN semantic_domain            SET DEFAULT '',
    ALTER COLUMN semantic_keywords          SET DEFAULT '',
    ALTER COLUMN semantic_use_cases         SET DEFAULT '',
    ALTER COLUMN embedding_text             SET DEFAULT '',
    ALTER COLUMN created_at                 SET DEFAULT now(),
    ALTER COLUMN updated_at                 SET DEFAULT now();

ALTER TABLE agent_builder."agentBuilder_agentrelationship"
    ALTER COLUMN id                 SET DEFAULT gen_random_uuid(),
    ALTER COLUMN source_node_id     SET DEFAULT '',
    ALTER COLUMN source_node_name   SET DEFAULT '',
    ALTER COLUMN source_node_type   SET DEFAULT 'workflow',
    ALTER COLUMN relationship_type  SET DEFAULT 'sub_flow',
    ALTER COLUMN config_inputs      SET DEFAULT '{}'::jsonb,
    ALTER COLUMN agent_info         SET DEFAULT '{}'::jsonb,
    ALTER COLUMN app_info           SET DEFAULT '{}'::jsonb,
    ALTER COLUMN module_info        SET DEFAULT '{}'::jsonb,
    ALTER COLUMN is_resolved        SET DEFAULT false,
    ALTER COLUMN validation_message SET DEFAULT '',
    ALTER COLUMN created_at         SET DEFAULT now(),
    ALTER COLUMN updated_at         SET DEFAULT now();

ALTER TABLE agent_builder."agentBuilder_agentworkflowchip"
    ALTER COLUMN id           SET DEFAULT gen_random_uuid(),
    ALTER COLUMN bpc          SET DEFAULT '',
    ALTER COLUMN display_name SET DEFAULT '',
    ALTER COLUMN description  SET DEFAULT '',
    ALTER COLUMN is_disabled  SET DEFAULT false;

ALTER TABLE agent_builder."agentBuilder_workflowsnapshot"
    ALTER COLUMN id         SET DEFAULT gen_random_uuid(),
    ALTER COLUMN comment    SET DEFAULT '',
    ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE agent_builder."agentBuilder_nodetypedefinition"
    ALTER COLUMN id                  SET DEFAULT gen_random_uuid(),
    ALTER COLUMN icon                SET DEFAULT '',
    ALTER COLUMN color               SET DEFAULT '',
    ALTER COLUMN description         SET DEFAULT '',
    ALTER COLUMN default_name_prefix SET DEFAULT '',
    ALTER COLUMN default_config      SET DEFAULT '{}'::jsonb,
    ALTER COLUMN handles             SET DEFAULT '{}'::jsonb,
    ALTER COLUMN form_schema         SET DEFAULT '{}'::jsonb,
    ALTER COLUMN output_schema       SET DEFAULT '{}'::jsonb,
    ALTER COLUMN validation_schema   SET DEFAULT '{}'::jsonb,
    ALTER COLUMN sort_order          SET DEFAULT 0,
    ALTER COLUMN is_active           SET DEFAULT true;

ALTER TABLE agent_builder."agentBuilder_tooldefinition"
    ALTER COLUMN id             SET DEFAULT gen_random_uuid(),
    ALTER COLUMN title          SET DEFAULT '',
    ALTER COLUMN description    SET DEFAULT '',
    ALTER COLUMN tool_type      SET DEFAULT 'function',
    ALTER COLUMN schema         SET DEFAULT '{}'::jsonb,
    ALTER COLUMN default_config SET DEFAULT '{}'::jsonb,
    ALTER COLUMN is_active      SET DEFAULT true,
    ALTER COLUMN created_at     SET DEFAULT now(),
    ALTER COLUMN updated_at     SET DEFAULT now();

-- ==========================================================================
-- 3. A unique key on tool name.
--
-- The tool catalogue is looked up by name and seeded with an upsert, so name
-- has to be unique. Django did not declare it. This fails loudly if duplicate
-- names already exist, which is the right outcome — silently keeping one of
-- them would be worse.
-- ==========================================================================
DO $$
DECLARE
    v_duplicates text;
BEGIN
    SELECT string_agg(name, ', ') INTO v_duplicates
    FROM (
        SELECT name FROM agent_builder."agentBuilder_tooldefinition"
        GROUP BY name HAVING count(*) > 1
    ) d;

    IF v_duplicates IS NOT NULL THEN
        RAISE EXCEPTION 'duplicate tool names block the unique index: %', v_duplicates
            USING HINT = 'Remove or rename the duplicates, then re-run this file.';
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_definition_name
    ON agent_builder."agentBuilder_tooldefinition" (name);

-- ==========================================================================
-- 4. The tool_type CHECK constraint Django expressed only as `choices`.
-- ==========================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_tool_definition_tool_type'
          AND conrelid = 'agent_builder."agentBuilder_tooldefinition"'::regclass
    ) THEN
        ALTER TABLE agent_builder."agentBuilder_tooldefinition"
            ADD CONSTRAINT ck_tool_definition_tool_type
            CHECK (tool_type IN ('function', 'handoff', 'ui_view', 'mcp'))
            NOT VALID;   -- NOT VALID: new rows are checked, existing rows are
                         -- left alone so this cannot fail on legacy data.
    END IF;
END;
$$;

INSERT INTO agent_builder.schema_migrations (version) VALUES ('005_adapt_existing_schema')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
