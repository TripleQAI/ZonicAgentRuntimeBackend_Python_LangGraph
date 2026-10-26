-- run_all.sql — apply the agent_runtime schema, in order.
--
--   psql "$AGENTRUNTIME_DSN" -v ON_ERROR_STOP=1 -f db/runtime/run_all.sql
--
-- or scripts/init_db.sh / scripts/init_db.ps1, which run psql in Docker.
--
-- 001, 002, 003 and 005 are vendored byte for byte from ZonicAgentRuntimeServer_Nodejs
-- (SOURCE.json; refresh with `uv run python -m scripts.sync_sql runtime`, never edit here).
-- 004 and 006 are this project's own.
--
-- NO psql? Paste these files into the SQL editor one at a time, in order:
--     001, 002, 003, 004, 005
-- None uses a psql meta-command. Every file is idempotent, so re-running is a
-- no-op that also picks up a changed function, view or procedure.
--
-- Prerequisite: the agent_builder schema of ZonicAgentBuilderServer_FastAPI
-- (its db/run_all.sql), whose "agentBuilder_agentworkflow" v_agent_catalog in 004
-- reads and whose v_agent_export the runtime loads workflows from.
--
-- 006_grants.sql is NOT included: it needs the gateway's role name.
--   psql "$AGENTRUNTIME_DSN" -v ON_ERROR_STOP=1 \
--        -c "SET agentruntime.role = 'agentruntime_app'" -f db/runtime/006_grants.sql

\echo '== 001 schema and tables'
\ir 001_schema.sql
\echo '== 002 functions and triggers'
\ir 002_functions_triggers.sql
\echo '== 003 indexes'
\ir 003_indexes.sql
\echo '== 004 views'
\ir 004_views.sql
\echo '== 005 procedures'
\ir 005_procedures.sql

\echo ''
\echo 'Deployed schema versions:'
SELECT version, applied_at FROM agent_runtime.schema_migrations ORDER BY version;
