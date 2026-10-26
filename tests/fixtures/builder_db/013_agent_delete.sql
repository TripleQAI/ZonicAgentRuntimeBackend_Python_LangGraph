-- 013_agent_delete.sql — delete one workflow (DELETE /api/v1/agents/{id}).
--
-- One CALL, one transaction. Deletes only a row the caller owns (006): a
-- workflow of another user, or none at all, reports o_deleted = false and the
-- API answers 404, the same as every other per-user read.
--
-- What goes with it:
--   its chip row                         (no foreign key; deleted here)
--   its snapshots and its own edges      (ON DELETE CASCADE)
-- What stays, marked:
--   edges of OTHER workflows that used it as a Sub Flow child keep their row
--   (ON DELETE SET NULL clears child_agent_id) and become unresolved, with the
--   message Python passes in, exactly as fn_agent_relationship_replace marks a
--   child that is not found.
--
-- Run after 010. Idempotent.

SET search_path TO agent_builder, public;

CREATE OR REPLACE PROCEDURE agent_builder.sp_agent_workflow_delete(
    p_agentic_workflow_id uuid,
    p_owner_sub           text,
    p_unresolved_fmt      text,
    INOUT o_deleted       boolean DEFAULT NULL
) AS $$
DECLARE
    v_id uuid;
BEGIN
    SELECT a.id INTO v_id
    FROM agent_builder."agentBuilder_agentworkflow" a
    WHERE a.agentic_workflow_id = p_agentic_workflow_id
      AND a.owner_sub = p_owner_sub;

    IF v_id IS NULL THEN
        o_deleted := false;
        RETURN;
    END IF;

    UPDATE agent_builder."agentBuilder_agentrelationship"
    SET is_resolved = false,
        validation_message = format(p_unresolved_fmt, p_agentic_workflow_id),
        updated_at = now()
    WHERE child_agent_id = v_id
      AND parent_agent_id <> v_id;

    DELETE FROM agent_builder."agentBuilder_agentworkflowchip"
    WHERE agentic_workflow_id = p_agentic_workflow_id;

    DELETE FROM agent_builder."agentBuilder_agentworkflow" WHERE id = v_id;
    o_deleted := true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

INSERT INTO agent_builder.schema_migrations (version) VALUES ('013_agent_delete')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
