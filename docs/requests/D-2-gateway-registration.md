# Request: register the Zonic Agent Runtime with the pgfoundation gateway (D-2)

**To:** pgfoundation gateway maintainers (`ZonicPostgresqlServer_FastAPI`)
**From:** Zonic Agent Runtime (`ZonicAgentRuntimeBackend_Python_LangGraph`)
**Asks for:** one caller, two datasources, and a redeploy of the Lambda configuration

**Status (2026-10-26): done.** Deployed with these changes to AWS account `001634074733` at `https://sdyvf4y53x524cwgdisy2qyfqy0bgjkm.lambda-url.us-east-1.on.aws` and verified (`uv run pytest -m live`). The changes in `ZonicPostgresqlServer_FastAPI` are uncommitted. The key is kept in the new account's secret and in the gateway's git-ignored `.env.lambda`.

## What the runtime needs

1. **Caller `zonic-agent-runtime`** in `config/pgfoundation.lambda.yaml`, under `auth.policy.callers`, with actions `read` and `write`. Its key arrives as `PGF_API_KEY_ZONIC_AGENT_RUNTIME`.

   ```yaml
   - subject: zonic-agent-runtime
     datasources: [agentruntime, agentobservability]
     actions: [read, write]
   ```

2. **Datasources `agentruntime` and `agentobservability`.** The names are fixed: they are the runtime's `settings.gateway.datasources`. The DSNs come from Lambda environment variables, so no secret goes into the repository. Same Neon tuning as `neondb`:

   ```yaml
   - name: agentruntime
     dsn: ${env:PGF_AGENTRUNTIME_DSN}
     role: standalone
     statement_timeout_ms: 30000
     pool: { min_size: 0, max_size: 3, acquire_timeout_seconds: 10, max_idle_seconds: 240, max_lifetime_seconds: 1800, check_on_checkout: true }
   - name: agentobservability
     dsn: ${env:PGF_AGENTOBSERVABILITY_DSN}
     role: standalone
     statement_timeout_ms: 30000
     pool: { min_size: 0, max_size: 3, acquire_timeout_seconds: 10, max_idle_seconds: 240, max_lifetime_seconds: 1800, check_on_checkout: true }
   ```

   Adding two pools changes your connection budget and `concurrency.max_inflight_total`; please size them as you see fit. The local configuration we run against is `deploy/pgfoundation.agentruntime.yaml` in our repository.

3. **Raw SQL stays off.** The runtime only reads views and calls procedures.

## How the runtime uses the gateway

| Surface | Calls |
|---|---|
| Views, schema `agent_runtime` | `v_agent_catalog`, `v_session_summary`, `v_message`, `v_run`, `v_run_queue`, `v_interrupt_pending`, `v_session_state`, `v_checkpoint`, `v_dataset` |
| Views, schema `agent_builder` | `v_agent_export` (workflow documents), through `agentruntime` |
| Procedures, schema `agent_runtime` | 14, positional, JSON arguments as text, trailing `INOUT` nulls: `sp_session_create`, `sp_session_update`, `sp_run_start`, `sp_run_transition`, `sp_run_finish`, `sp_interrupt_create`, `sp_interrupt_answer`, `sp_feedback_save`, `sp_dataset_save`, `sp_checkpoint_save`, `sp_run_claim`, `sp_run_heartbeat`, `sp_run_release`, `sp_retention_purge` |
| Procedures, schema `agent_observability` | `sp_obs_ingest` (idempotent, retried), `sp_obs_score_upsert`, `sp_obs_model_price_upsert`, `sp_obs_partition_ensure`, `sp_obs_retention_purge`, `sp_obs_refresh_rollups` |
| Batches | Not used until the gateway checks each batch item's datasource and action; today `/v1/batch` is authorized as one global `batch` resource, so a `batch` grant would open every datasource |
| Errors we rely on | Class-23 SQLSTATEs returned as 409 with the procedure's reason token as the message (`run_not_found`, `stale_attempt`, `already_answered`, ...) |

Expected load: one call per turn start and turn end, one per pause, a lease heartbeat within every 30 s lease (`asyncRuns.leaseSeconds`) per async run, a queue claim every 1 to 10 s, and observability batches every 2 s. The runtime caps itself at `settings.gateway.maxInFlight` (32) and retries only reads and `sp_obs_ingest`.

## Which database

Both datasources point at Neon project `agent-build-langgraph`, branch `production`, database `neondb`. It holds `agent_builder`, `agent_runtime` and `agent_observability`, because `agent_runtime.v_agent_catalog` joins the builder's workflow table. The DSN is in the gateway's git-ignored `.env` (`PGF_AGENTRUNTIME_DSN`, `PGF_AGENTOBSERVABILITY_DSN`). It is not the builder's `neondb` / `neondb_dev`, which live in a different Neon project.

## How we will verify

`uv run pytest -m live` in our repository runs the repository suite through the deployed gateway (P-03, P-04, and P-01, P-02 against the real procedures).
