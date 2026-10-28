-- 003_functions_triggers.sql — updated_at maintenance.
--
-- Replaces Django's auto_now. Doing it in the database means the timestamp is
-- right whatever wrote the row (plan §L9).
--
-- The procedures in 010 also set updated_at explicitly, so they do not depend
-- on these triggers; the triggers exist so a direct write — from Django, from
-- psql — cannot leave a stale timestamp behind.

CREATE OR REPLACE FUNCTION agent_builder.set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_workflow_updated_at
    ON agent_builder."agentBuilder_agentworkflow";
CREATE TRIGGER trg_agent_workflow_updated_at
    BEFORE UPDATE ON agent_builder."agentBuilder_agentworkflow"
    FOR EACH ROW EXECUTE FUNCTION agent_builder.set_updated_at();

DROP TRIGGER IF EXISTS trg_agent_relationship_updated_at
    ON agent_builder."agentBuilder_agentrelationship";
CREATE TRIGGER trg_agent_relationship_updated_at
    BEFORE UPDATE ON agent_builder."agentBuilder_agentrelationship"
    FOR EACH ROW EXECUTE FUNCTION agent_builder.set_updated_at();

DROP TRIGGER IF EXISTS trg_tool_definition_updated_at
    ON agent_builder."agentBuilder_tooldefinition";
CREATE TRIGGER trg_tool_definition_updated_at
    BEFORE UPDATE ON agent_builder."agentBuilder_tooldefinition"
    FOR EACH ROW EXECUTE FUNCTION agent_builder.set_updated_at();

INSERT INTO agent_builder.schema_migrations (version) VALUES ('003_functions_triggers')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
