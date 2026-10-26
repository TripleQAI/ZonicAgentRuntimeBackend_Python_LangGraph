-- 006_ownership.sql — per-user ownership of workflows (Strands builder design,
-- Section 10.2; decision D10).
--
-- Adds the owner columns and their index. The rules that use them live with the
-- objects they change, so the installer stays idempotent:
--
--   009_api_views.sql       every per-user view exposes owner_sub (the relationship
--                           view exposes parent_owner_sub) and the repositories
--                           filter every read by it;
--   010_api_procedures.sql  fn_agent_workflow_upsert sets the owner on insert,
--                           claims a row that has none, and refuses a row another
--                           user owns (SQLSTATE 23O01, message "owner_mismatch: ...",
--                           which the builder reports as 404); child workflows
--                           resolve only within the same owner.
--
-- Rows written before this script have no owner. Nobody reads them until the user
-- who saves or imports them claims them (design Section 10.3).
--
-- Run before 009 (run_all.sql does): the views reference these columns.
-- Idempotent.

SET search_path TO agent_builder, public;

ALTER TABLE agent_builder."agentBuilder_agentworkflow"
    ADD COLUMN IF NOT EXISTS owner_sub   varchar(64),
    ADD COLUMN IF NOT EXISTS owner_email varchar(320);

COMMENT ON COLUMN agent_builder."agentBuilder_agentworkflow".owner_sub IS
    'Cognito subject of the owning user; NULL for rows written before 006_ownership';
COMMENT ON COLUMN agent_builder."agentBuilder_agentworkflow".owner_email IS
    'Email of the owning user when the row was last written by them';

CREATE INDEX IF NOT EXISTS ix_agentworkflow_owner_sub
    ON agent_builder."agentBuilder_agentworkflow" (owner_sub);

INSERT INTO agent_builder.schema_migrations (version) VALUES ('006_ownership')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
