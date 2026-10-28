-- 004_indexes.sql — indexes the application's access paths actually need.
-- Django created none of these beyond its FK and UNIQUE defaults.

CREATE INDEX IF NOT EXISTS idx_agent_workflow_updated_at
    ON agent_builder."agentBuilder_agentworkflow" (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_workflow_parent_package
    ON agent_builder."agentBuilder_agentworkflow" (parent_package_id);

CREATE INDEX IF NOT EXISTS idx_agent_workflow_is_root
    ON agent_builder."agentBuilder_agentworkflow" (is_root) WHERE is_root;

CREATE INDEX IF NOT EXISTS idx_agent_workflow_semantic
    ON agent_builder."agentBuilder_agentworkflow" (agentic_workflow_id)
    WHERE semantic_search_enabled;

CREATE INDEX IF NOT EXISTS idx_agent_relationship_parent
    ON agent_builder."agentBuilder_agentrelationship" (parent_agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_relationship_child
    ON agent_builder."agentBuilder_agentrelationship" (child_agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_relationship_parent_wf
    ON agent_builder."agentBuilder_agentrelationship" (parent_agentic_workflow_id);

CREATE INDEX IF NOT EXISTS idx_agent_relationship_parent_resolved
    ON agent_builder."agentBuilder_agentrelationship" (parent_agent_id, is_resolved);

CREATE INDEX IF NOT EXISTS idx_workflow_snapshot_agent
    ON agent_builder."agentBuilder_workflowsnapshot" (agent_workflow_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_node_type_definition_active
    ON agent_builder."agentBuilder_nodetypedefinition" (sort_order) WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_tool_definition_active
    ON agent_builder."agentBuilder_tooldefinition" (tool_type) WHERE is_active;

INSERT INTO agent_builder.schema_migrations (version) VALUES ('004_indexes')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
