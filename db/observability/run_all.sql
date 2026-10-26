-- run_all.sql — apply the agent_observability schema, in order.
--
--   psql "$AGENTOBSERVABILITY_DSN" -v ON_ERROR_STOP=1 -f db/observability/run_all.sql
--
-- or scripts/init_db.sh / scripts/init_db.ps1, which run psql in Docker.
--
-- 001 to 005 are vendored byte for byte from ZonicAgentRuntimeServer_Nodejs (SOURCE.json;
-- refresh with `uv run python -m scripts.sync_sql observability`, never edit here).
-- 006 and this file are this project's own.
--
-- NO psql? Paste 001 to 005 into the SQL editor in order, then run the CALL below.
-- Every file is idempotent. 006_grants.sql is NOT included: it needs the gateway's role name.
--
-- Partitions for the current month and settings.observability.partitionMonthsAhead (2)
-- months ahead are created here; afterwards the maintenance schedule calls
-- sp_obs_partition_ensure, and an ingest creates a missing month itself.

\echo '== 001 schema and tables'
\ir 001_schema_create.sql
\echo '== 002 functions'
\ir 002_functions.sql
\echo '== 003 indexes'
\ir 003_indexes.sql
\echo '== 004 views'
\ir 004_views.sql
\echo '== 005 procedures'
\ir 005_procedures.sql

CALL agent_observability.sp_obs_partition_ensure(2, NULL);

\echo ''
\echo 'Deployed schema versions:'
SELECT version, applied_at FROM agent_observability.schema_migrations ORDER BY version;
