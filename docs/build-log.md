# Build Log

Progress against `docs/zonic-agent-runtime-langgraph-implementation-plan.md`, with the sanity checks run after each task and every deviation from the plan.

## Phase 0: complete; M0 approved 2026-10-26

| Task | Status | Sanity check |
|---|---|---|
| P0-01 Server scaffold | Done | `uv sync`; ruff, mypy, import-linter clean |
| P0-02 Base models and markers | Done | mypy strict |
| P0-03 Resource models | Done | M-01, M-02 |
| P0-04 Resource files and overlays | Done | M-01, M-02 in dev, test, prod |
| P0-05 Settings loading | Done | M-01, M-02 |
| P0-06 Generated schemas and principle tests | Done | M-03, X-08; `scripts.gen --check` |
| P0-07 Gateway port and adapter | Done | P-03, P-04, M-07 |
| P0-08 `agent_runtime` SQL and gateway registration | Done; D-2 request written, not yet sent | P-01, P-02, P-15: 56 SQL tests on Neon |
| P0-09 `agent_observability` SQL | Done | O-02, O-03, O-04, O-07: 29 SQL tests on Neon |
| P0-10 LangGraph spikes SP-1 to SP-10 | Done; decisions proposed | 63 spike checks; `docs/decisions/0001-langgraph-verification-spikes.md` |
| P0-11 `ScriptedChatModel` and core fakes | Done | 17 self-tests, including a `create_agent` tool loop |

### Checkpoint M0

| M0 criterion | State |
|---|---|
| CI green with every principle check active | ruff check and format, mypy (61 files), import-linter (4 contracts), `scripts.gen --check` (12 files), `scripts.sync_sql --check` (15 vendored files), `scripts.catalog_check` (296 cases, 14 with tests, none missing), 82 offline tests |
| Both schemas apply to the test database | `agent_runtime` and `agent_observability` apply twice (idempotent) and pass 85 SQL tests on Neon PostgreSQL 18.6, each in a rolled-back transaction; the branch was confirmed empty afterwards |
| Repository suite passes on both gateways | P-03 and P-04 pass on `InMemoryGateway` and through the HTTP adapter (P0-07). The deployed gateway waits for D-2. |
| Every spike has a decision record | Decision record 0001: no fallback needed; four spikes add conditions for later tasks |

**For the architect's review:** decision record 0001, the D-2 request (`docs/requests/D-2-gateway-registration.md`) and its open database question, and the findings for the Node.js project below.

### Where the SQL comes from

| Files | Source | Owner |
|---|---|---|
| `db/runtime/001_schema.sql`, `002_functions_triggers.sql`, `003_indexes.sql`, `005_procedures.sql` | `ZonicAgentRuntimeServer_Nodejs/db/runtime`, commit `5c75833`, byte for byte (`db/runtime/SOURCE.json`) | Node.js runtime project |
| `db/observability/001_schema_create.sql` to `005_procedures.sql` | `ZonicAgentRuntimeServer_Nodejs/db/observability`, commit `70d21be`, byte for byte (`db/observability/SOURCE.json`) | Node.js runtime project |
| `db/runtime/004_views.sql`, `006_grants.sql`, `run_all.sql`, `999_drop_all.sql`; `db/observability/006_grants.sql`, `run_all.sql`, `999_drop_all.sql` | Written here | This project |
| `tests/fixtures/builder_db/` (builder `001` to `005`, `009_api_views.sql`) | `ZonicAgentBuilderServer_FastAPI/db`, commit `2a14e4a`, byte for byte | Builder project; installed only inside the SQL tests' rolled-back transaction |

Refresh with `uv run python -m scripts.sync_sql [runtime|observability|builder]`; never edit a vendored file here. CI's `--check` fails when one is edited or its source changes. The source repositories are never modified.

### How the plan's deltas are met without changing the shared procedures

| Plan item | How it is met |
|---|---|
| DR-8 checkpoint `format`, `langgraph_version` | In the checkpoint `body` (`format`, `langgraphVersion`), exposed as columns of `v_checkpoint` |
| DR-8 `kind = 'drain'` | A drain saves through `sp_checkpoint_save` as `kind = 'async'`; `sp_run_release` keeps the row (P-15 test) |
| DR-8 `sp_run_claim` returning `has_checkpoint` | `v_run.has_checkpoint`; the claimer reads it after claiming |
| DR-9 `last_event_seq` | `body.lastEventSeq`, exposed on `v_run` and `v_checkpoint` |
| LangGraph interrupt IDs | 32 hexadecimal characters; they cast to `runtime_interrupt.interrupt_id uuid` unchanged (SP-2, P-15) |

### Findings for the Node.js project (`ZonicAgentRuntimeServer_Nodejs`)

Not changed here, because the files are vendored. To be fixed there and re-synced:

1. **`db/runtime/004_views.sql`, `v_agent_catalog`:** `channels_text` is built with `jsonb_array_elements_text`. The builder saves `publishChannels` as objects (`{refId, code, name}`), so the text holds whole JSON objects and the `LIKE ',api,'` filter never matches a builder workflow. It also reads `security->'groups'`, but builder workflows store `security.activities`. This project's `004` uses one row per channel code instead.
2. **`db/observability/005_procedures.sql`, `sp_obs_ingest`:** the months to partition are computed with `date_trunc('month', startTime)` in the **session** time zone, while `fn_obs_ensure_partition` creates months in UTC. Under a non-UTC session, an observation in the first hours (UTC) of a month fails with `no partition of relation "obs_observation" found for row`. This was reproduced with `America/New_York` and `2026-11-01T02:00:00Z`. Neon sessions default to GMT, so it does not occur today. The fix is to pass the timestamp itself (or truncate in UTC) in the month loop.

### Deviations

| Item | Plan | Built | Reason |
|---|---|---|---|
| SQL test database | Assumption 5: a `runtime_test` branch; production out of scope | Neon project `agent-build-langgraph`, branch `production` | The user's decision (2026-10-25). The branch was empty and is still empty: every SQL test ends in ROLLBACK. |
| `agent_runtime` and `agent_observability` SQL | Written in P0-08 and P0-09 | Vendored from the Node.js runtime | The user's existing, committed schemas; both runtimes share one schema of each name |
| `v_agent_catalog` | Node.js `004` | One row per channel; `security` returned whole | Finding 1 above |
| Error mapping | `002` header prefixes | The Node.js `fn_raise` reason tokens mapped in `http_gateway.py` | The procedures raise the bare token as the message |
| Error catalog | 63 codes | 64: `RUN_LEASE_LOST` | `stale_attempt` (fencing) needs its own code |
| Object counts | `agent_runtime`: 8 tables, 9 views | 9 tables (with `schema_migrations`), 9 views, 14 procedures | As vendored |
| `settings.models.executorThreads` | 32 | 160 | SP-5: the model executor must match concurrent streams |
| Spike lint | Full ruff rule set | `spikes/**` ignored async-style and minor rules while the spikes existed | Measurement code; removed after M0 together with its `ruff.toml` entry |
| PostgreSQL version | 17 in CI | Neon runs 18.6 | The scripts pass on both; CI keeps 17 as the floor |

### Product database (2026-10-26)

The user chose Neon project `agent-build-langgraph`, branch `production`, as the product database. It now holds all three schemas, installed permanently:

| Schema | Installed with | Objects |
|---|---|---|
| `agent_builder` | The builder's own `db/run_all.sql` (`ZonicAgentBuilderServer_FastAPI` commit `2a14e4a`; `001` to `005`, `009`, `010`), read in place | 7 tables, 8 views, 6 procedures |
| `agent_runtime` | `scripts/init_db.sh` → `db/runtime/run_all.sql` | 9 tables, 9 views, 14 procedures |
| `agent_observability` | `scripts/init_db.sh` → `db/observability/run_all.sql` | 6 tables, 3 monthly partitions (2026-10 to 2026-12), 6 views, 2 materialized views, 6 procedures |

Re-running the installer is a no-op (verified). The 85 SQL tests still pass on the populated database. `006_grants.sql` was not run: the gateway connects as the owner (`neondb_owner`).

**Gateway (D-2), 2026-10-26.** On the user's instruction the gateway was redeployed to AWS account `001634074733` (profile `tripleq-development`, `us-east-1`) at **`https://sdyvf4y53x524cwgdisy2qyfqy0bgjkm.lambda-url.us-east-1.on.aws`**, with `deploy/aws-lambda/deploy.sh`. It created the ECR repository, IAM role, function, Function URL and secret `zonic-postgresql-server/runtime` in that account. It serves `neondb`, `neondb_dev`, `agentruntime` and `agentobservability`, all healthy. These changes in `ZonicPostgresqlServer_FastAPI` made that possible; they are uncommitted:

| File | Change |
|---|---|
| `postgresqlmodule/code/config/pgfoundation.lambda.yaml` | Datasources `agentruntime` (`PGF_AGENTRUNTIME_DSN`) and `agentobservability` (`PGF_AGENTOBSERVABILITY_DSN`), both on `agent-build-langgraph`; caller `zonic-agent-runtime` with read and write on those two only |
| `deploy/aws-lambda/deploy.sh` | Reads the two new DSNs (observability defaults to runtime) and the runtime caller key (generated when missing); merges any value the secret lacks into it, keeping other keys; passes all six variables to the function |
| `deploy/aws-lambda/README.md` | Documents the above |

On the deployed gateway:
- The authorization matrix is as intended: the builder key reads `neondb`/`neondb_dev` and gets 403 on `agentruntime`; the runtime key reads `agentruntime`/`agentobservability` and gets 403 on the builder's databases; no key gets 401.
- `uv run pytest -m live` passes (21 pass, 1 skipped). The runtime's `.env` points `ZONIC_GATEWAY_BASE_URL` at the new URL.
- The runtime application uses it too. `EnvironmentModel` now reads `ZONIC_*` from the repository's git-ignored `.env` after the process environment, so a variable set in the process still wins. `.env.example` documents the gateway URL and where the `zonic-agent-runtime` key lives. `.dockerignore` keeps `.env` out of the build context. `tests/unit/test_env.py` covers the precedence.
- The builder's existing key (`PGF_API_KEY_ZONIC_AGENT_BUILDER`) is unchanged and works.

**The old deployment** in account `633160612720` (`tripleq-management`, `https://e3idqtzbodidr22fmbipzkg7xy0hjeiw...`) was deleted on the user's approval (2026-10-26), with an `AWSAdministratorAccess` SSO profile `tripleq-management` added to `~/.aws/config`. The function and its Function URL, the ECR repository (3 images), the log group and the IAM role `zonic-postgresql-server-lambda` are gone, and the old URL answers 403. The secret `zonic-postgresql-server/runtime` is scheduled for deletion and recoverable until 2026-11-24. The builder's `Strands-visual-builder/.env.docker` must point `PG_GATEWAY_BASE_URL` at the new URL; its key is unchanged.

Checked locally before deploying, with the gateway image built from the changed tree and run on the Lambda config with the same six variables:
- All four datasources are healthy.
- The authorization matrix is as intended: the builder key reads `neondb`/`neondb_dev` and gets 403 on `agentruntime`; the runtime key reads `agentruntime`/`agentobservability` and gets 403 on the builder's databases; no key gets 401.
- The live suite passes (21 pass, 1 skipped).
- The secret merge in `deploy.sh` was dry-run with a stubbed `aws` for a missing, identical and partial secret.

The values live only in git-ignored files: `PGF_AGENTRUNTIME_DSN` and `PGF_AGENTOBSERVABILITY_DSN` in the gateway's `.env`, a new `PGF_API_KEY_ZONIC_AGENT_RUNTIME` in its `.env.lambda`, and `ZONIC_GATEWAY_BASE_URL` / `ZONIC_GATEWAY_API_KEY` in this project's `.env`.

**Gateway finding (security):** `/v1/batch` is authorized as one global resource named `batch` (`authorize_global("write", "batch")`), and batch items are not checked against the caller's datasources. A caller granted `batch` could therefore read or write every datasource through a batch. No caller has the grant today, so batches are refused for the builder too. `zonic-agent-runtime` is not granted it either; the live batch test is skipped with that reason. Until the gateway checks each item's datasource and action, the runtime calls procedures one at a time (`sp_run_finish` already saves a turn in one transaction; only database-store datasets lose atomicity with it).

The live suite `tests/live/test_gateway_live.py` (`uv run pytest -m live`) drives the runtime's `HttpGateway`. It covers every runtime, builder and observability view, reason tokens mapped to catalog codes over real HTTP, and an idempotent procedure. It writes no runtime data. `tests/live/conftest.py` takes the gateway URL and key from the environment, else from `.env`.

`scripts/init_db.sh` was fixed for Git Bash on Windows: it now passes the volume path in Windows form and disables MSYS path conversion.

### Open

- **Builder data:** the builder application still writes workflows to its own database (Neon project `agent-build-run`). The runtime only sees workflows saved in `agent-build-langgraph`, so either the builder is pointed at this database or its workflows are copied here.
- **D-2:** deployed and verified in account `001634074733` (above). The changes in `ZonicPostgresqlServer_FastAPI` are uncommitted.
- **Gateway batch authorization:** per-item datasource checks are needed before any caller gets `batch` (above).

### Running the opt-in suites locally

- SQL: put `ZONIC_SQL_TEST_DSN=...` in the git-ignored `.env`, then `uv run pytest -m sql`. psql runs in the `postgres:17` Docker image and receives the DSN through its environment.
- Live: `uv run pytest -m live` runs against the deployed gateway named in `.env` (`ZONIC_GATEWAY_BASE_URL`, `ZONIC_GATEWAY_API_KEY`; see `.env.example`).
- Spikes: removed after M0; see decision record 0001 for how to restore them from git history.

## Cleanup before Phase 1 (2026-10-26)

| Removed | Why |
|---|---|
| `spikes/` (code) | The plan deletes the spikes after M0. Their reports moved to `docs/decisions/0001-spike-reports/`, and the code stays in git history at `bcd2fde`. The `spike` pytest marker and the `spikes` entries in `ruff.toml`, `.gitignore`, `.dockerignore` and `CLAUDE.md` went with it. |
| `config/resources.py: load_resource` | Duplicated `load_with_overlay`, which every resource load uses |
| `models/gateway/wire.py: FunctionIn` | The runtime reads views and calls procedures only; it never calls gateway functions |
| `models/gateway/wire.py: BatchResult.by_id` | Never used; batches stay unused until the gateway checks batch items per datasource |
| `models/gateway_meta.py: has_procedure_meta` | Never used; `procedure_meta` covers it |

Also: `.import_linter_cache/` was added to `.gitignore`, and the `sql` marker text now says what the SQL tests need (psql and `ZONIC_SQL_TEST_DSN`). Kept on purpose: `get_environment`, `secret_from_environment`, `fail_startup` and `GatewayRepository.read_one` (entry points for the Phase 1 and 2 tasks); `deploy/pgfoundation.agentruntime.yaml` (running the gateway locally without AWS); `docs/requests/D-2-gateway-registration.md` (record of the completed request).

## Workflows in the product database (2026-10-26)

The user approved M0 and decided that workflows live in `agent-build-langgraph`'s `agent_builder."agentBuilder_agentworkflow"`, with the builder saving there.

1. **Schema:** the builder that writes the data is `Strands-visual-builder`, so its full `agent_builder` install (`backend/db/run_all.sql`: the FastAPI builder's `001`–`005` plus `006` ownership, its own `009`/`010`, `012` builder records, `013` agent delete) was dry-run in a rolled-back transaction, then applied on top of the existing schema.
2. **Data:** the 9 workflows in `neondb_dev` not owned by `live-test-%` were copied with their 9 chips, 11 relationships and 6 snapshots, in one transaction, with explicit column lists. The 80 `live-test` workflows and all deployment, AgentCore-gateway and settings rows (all test data or empty) were not copied. Source and target fingerprints of the 9 rows (ids, owners, documents, channels) are identical. `neondb_dev` was only read.
3. **Gateway:** a new datasource `agentbuilder` (`PGF_AGENTBUILDER_DSN`, defaulting to the runtime database) for caller `zonic-agent-builder`, which keeps `neondb` and `neondb_dev`; redeployed to account `001634074733`. `deploy.sh` also got a fix: a missing optional variable no longer ends the script silently under `pipefail`.
4. **Builder:** `PG_GATEWAY_DATASOURCE=agentbuilder` in `.env.docker` and `.env.docker.example`, and `cdk/cdk.json` dev; containers recreated. `/health/ready` reports `agentbuilder` ready.
5. **Runtime:** the SQL tests' builder fixture is now the Strands builder's 10 scripts (`scripts/sync_sql.py`, set `builder`). Every suite passes: offline, SQL (85), live (21 + 1 skipped). `v_agent_catalog` lists the 6 root workflows; none has a publish channel yet.

Open: the 9 workflows are owned by `54488448-…`, the builder's earlier user pool, not the Docker builder's user. They are in the database but not listed in the builder until re-owned.
