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

## Phase 1

| Task | Status | Sanity check |
|---|---|---|
| P1-01 Document models and fixtures | Done | M-04, F-01, F-02: 76 tests; `scripts.sync_builder --check` in CI |
| P1-02 Node configuration, engine record and blueprint models | Done | M-05, M-08, M-04 (configuration half): 63 tests; all 397 fixture nodes convert with no unknown key |
| P1-03 Dialect normalizer | Done | B-01 to B-11, B-14, B-15, D-12, D-13, F-16, F-02 (normalization parts): 287 tests in `tests/compat/`; all 45 fixture documents normalize with no unknown field |
| P1-04 Loader and catalog | Done | D-01, D-02, D-03, B-16, A-01 (data part), F-02: 94 tests; live: the 6 runnable workflows in the product database load through the deployed gateway |
| P1-05 Blueprint | Done | D-04 to D-11, D-14, D-15, F-01, F-03, B-01 (to the plan; compilation is P1-09): 103 tests; live: the product database's workflows plan, and `new-workflow` fails SW-1 |
| P1-06 Templates and conditions | Done | E-11, E-12, N-09, F-04, F-05, B-08, B-09: 108 tests |
| P1-07 Node input and output contract | Done | I-01 to I-11, B-10: 57 tests; the vendored documents' only unknown template path is the known stale reference |
| P1-08 `RunState`, reducers and `RunContext` | Done | E-10, E-21, E-22, B-06, B-07: 15 tests (3 hypothesis properties, one real LangGraph parallel superstep) |
| P1-09 Compiler, routing and graph cache | Done | E-01 to E-05, E-20, G-01 to G-04, G-07, G-09 to G-13, D-09, F-15, B-01 (compile): 104 tests; 42 compiled snapshots; G-09 property test against a reference interpreter |
| P1-10 Node template, kind registry and error policy | Done | C-01 to C-16, E-13 to E-16, E-18, G-14: 34 tests (conformance on test kinds until P2-02); LangGraph upgraded to 1.2.18, decision 0002 |
| P1-11 Loops and limits | Done | E-06 to E-09, F-06: 28 tests; the 8 platform loops and 3 synthetic loops run to their exit and stop at the visit limit |

### P1-01 notes

- **Models** (`models/document/`): `WorkflowDocument`, `WorkflowNode`, `WorkflowEdge`, `VariableDefinition`, `SystemParam`, `Point`, `WorkflowTemplate`. Unknown keys are kept and listed by `unknown_key_report()`; the canvas keys `selected`, `dragging` and `deletable` count as layout, not unknown. `variables` accepts both shapes (the builder's name map and the platform's `{flow: [...]}`). Node types are not enumerated in code: a test keeps `node-runtime.json` equal to the builder schema's `nodeType` enum.
- **M-04** compares the models' JSON Schema with the vendored `workflow-document-v2.schema.json`: every builder field exists, types are equal or narrower, `minLength` and `pattern` are kept, and builder-required fields are required. A probe with a deliberately loose model confirmed the check reports it. Optional fields accept `null` as absent.
- **Fixtures** (`tests/fixtures/workflows/`): 33 vendored files with `SOURCE.json` (`scripts/sync_builder.py`, Strands builder `90b5885`, Django runtime `cf55ca2`) and 12 synthetic 2.0 documents, each valid against the builder schema. Together they cover all 18 node types (test `test_F01_every_node_type_has_a_fixture`).
- **F-01/F-02 split:** the parts that need later tasks were added to their Proves columns. F-01's "same blueprint" is now also proven by P1-05; F-02's "empty model resolves to the catalog default" by P1-03 and "template IDs never in the catalog" by P1-04.

| Plan 20.1 says | Built | Why |
|---|---|---|
| `zonic-v2/`: the 7 exports with `schemaVersion: "2.0"` | `zonic-versioned/`: the builder now stamps them `"1.0"` | The builder changed these files after commit `24e29ee`. The test is that a missing version and an explicit `1.0` load to the same document. |
| Open-data versions exported with the builder's `export-package` API | Exported from the product database's `v_agent_export` through the gateway (`scripts/export_workflows.py`, runtime caller) | The same documents, already in the database the runtime reads; no builder ID token needed |
| Django workflows "only where they add a case" | `complex-agent.json` only | The others duplicate Zonic exports |

### P1-02 notes

- **Configuration models** (`models/nodes/`): `NodeConfigBase` and 17 per-kind models whose fields are what the builders write in both dialects, surveyed over all 45 fixture documents. Every one of the 397 nodes converts with no unknown key, and the Facility Operations Swarm's configurations round-trip exactly by alias.
- **Kind names are not written in code.** A kind's model is found from its class name (`LlmNodeConfig` serves kind `llm`), so the manifest stays the only place kind names appear (design rule 1). The kind-derived names differ from the design's examples in two places: `RouterNodeConfig` for the design's `RuleNodeConfig` and `MultiAgentNodeConfig` for `SwarmNodeConfig`.
- **M-08:** fields holding templates are marked `TemplateField`, and `marked_values()` returns only those, so the resolver (P1-06) never sees an unmarked field. `ExternalAgentNodeConfig.api_key` is a `SecretRef`: it accepts `secret:<name>` (the Node.js runtime's decision C-5), `{{secrets.<name>}}` (what the builder's form suggests) or nothing; any other value fails validation.
- **Engine records** (`models/engine/`): `NodeOutcome`, `NodeRecord`, `StatePatch`, `StateUpdate`, `FlowPatch` (DR-1), `ThreadMessage`, `TimelineEntry`, `DatasetRef`, `Usage`, `InterruptRequest`. Value lists match the database's check constraints. The message role `tool` and the interrupt kind `approval` share a spelling with node types and carry documented rule-1 waivers.
- **Blueprint models** (`models/blueprint/`): frozen `Blueprint`, `NodeSpec`, `EdgeIndex`, `JoinSpec`, `Component`, `BlueprintWarning`, holding no LangGraph objects. `NodeSpec.trace_metadata()` is the node metadata that decision 0001 (SP-4) types spans from.

### P1-03 notes

- **Ported, not rewritten.** `resources/dialects.json` and the operations in `workflow/loader/dialect_operations.py` are a port of the Node.js runtime's `dialects.json` and `dialect-operations.ts`. `DocumentNormalizer` removes layout keys, applies the rules in order, drops ignored config fields without a finding, and drops unknown fields with a `DOCUMENT_UNKNOWN_FIELD` warning. Every rule that fires adds a `DIALECT_RULE` finding (details `rule_id`, `class`, `path`). The stored document is never changed.
- **Four rules added** to the 28 ported ones:
  - `builder.agent-strategy-supported`: an agent strategy outside `agentStrategies` is a `DOCUMENT_INVALID` error.
  - `builder.rule-expression`: a rule `expression` is a `RULE_EXPRESSION_UNSUPPORTED` error. With `rule_expression="parse_simple"`, `{{path}} <op> <literal>` becomes a condition; anything more complex is still an error (D-12).
  - `builder.output-mapping-missing`: an Output node with no mapping is an `OUTPUT_MAPPING_MISSING` warning (D-13).
  - `builder.model-id`: an empty model becomes the catalog default with a `MODEL_DEFAULTED` warning (F-02).
- **Canonical forms** (`models/dialects/`): `ModelRef`, `ToolRef`, `RoutingToolRef`, `OutputMappingEntry`. `StateUpdate` is `path`, `op`, `value`, `role` and `when` (a `ConditionGroup`, now in `models/nodes/base.py`). The normalizer writes plain JSON; consumers validate what they read with these models.
- **Bug fixed in the port, also present in Node.js:** `routingTools` dropped a handoff tool's preset `parameters` and its `title`. Five platform tools have non-empty parameters; one (`get_decision_points` in Intelligent Buying Orchestrator) carries three `{{flow.*}}` templates. `RoutingToolRef` now keeps `title`, `parameters` (a `TemplateField`), `enabled` and `returnDirect`. B-09 now asserts, for every fixture, that every `{{...}}` template in the stored document survives normalization. The Node.js runtime still has the bug.
- **Findings on the real documents:** `MODEL_DEFAULTED` on the 4 templates, `OUTPUT_MAPPING_MISSING` on 9 Output nodes, `RULE_EXPRESSION_UNSUPPORTED` only on the synthetic rule-expression fixture. Builder documents fire alias-class rules only (B-14); every platform document fires platform rules, each listed (F-16).
- **B-15:** `compat_problems()` in the test names any unknown field, unknown configuration key, invalid update or operator without an alias. It is empty for all 45 documents, and a negative test confirms it names all three kinds of problem.

### P1-04 notes

- **One load path** (`workflow/loader/loader.py`): `WorkflowLoader.from_stored()` runs steps 3 to 6 of design 5.1 on any stored document. `load()` (a catalog workflow) and `load_exports()` (sub-flows, one `in` read) both go through it, as does every fixture. Steps:
  - version check: a missing `schemaVersion` is 1.0; the major must be in `settings.documentVersions`, else `DOCUMENT_VERSION_UNSUPPORTED`;
  - normalization with layout stripping (P1-03);
  - validation of the document and of each node's configuration model, giving `DOCUMENT_INVALID` with every problem's path;
  - the revision hash over the canonical JSON.
- **Unknown node types are not a load error.** The loader skips their configuration check and leaves them to the blueprint (P1-05), which applies the manifest's `onUnknownType`.
- **Visibility** (Q2): a workflow is listed when it is root, not disabled, published to a channel in `catalog.runtimeChannels` (`api`, `qi-ui`), and not under `catalog.excludedIdPrefixes` (new setting, `["template-"]`, case F-02). After M1 the caller's groups play no part (decision 0004, below). An invisible workflow and an unknown one both give `WORKFLOW_NOT_FOUND`. Development settings (`includeUnpublished`) drop the channel rule, which is how the product database's 6 root workflows are listed today: none has a publish channel yet.
- **Assumption, resolved at M1:** P1-04 first matched `security.activities[].code` against the `cognito:groups` claim. The reviewer chose one security model, Cognito (decision 0004), so the runtime no longer reads `security`, and every authenticated caller sees the same catalog.
- **Caches:**
  - Documents are cached by `(agenticWorkflowId, updated_at)` (`caches.documents` entries), so loading an unchanged workflow costs one catalog read.
  - Sub-flows read `updated_at` from the builder's `v_agent_summary` (new `AgentRevisionRow`), because they are not catalog rows.
  - The catalog list is cached for `catalog.cacheSeconds`, but `load()` always reads its row fresh.
- **Revision hash** (`workflow/loader/revision.py`): SHA-256 of sorted, compact JSON of the normalized document. Key order and layout (moving a node on the canvas) do not change it; content does.
- **New port** `ports/rows.py` (`RowReader`): `workflow` may not import `integrations` (import contract), so the loader reads rows through this protocol. `GatewayRepository` satisfies it structurally.
- Row models: `CatalogAgentRow` (one row per channel, de-duplicated by the loader), `AgentExportRow` and `AgentRevisionRow` in `models/gateway/catalog.py`.
- Removed an empty `db;C/` directory left at the project root by the earlier Git Bash path conversion.

### P1-05 notes

- **Ported where the engines agree.** These come from the Node.js runtime's `blueprint-builder.ts`, `validation-rules.ts` and `loop-analysis.ts`:
  - the builder's structure (node specs, edges with default handles, entry, containers, reachability, handles, branch checks, child references);
  - the builder-rule evaluator (SW-1 to SW-12, in the builder's wording);
  - Tarjan's algorithm.

  The Node.js loop regions, back edges and body regions are not ported: they exist for the Strands graph engine, and under LangGraph cycles compile as they are (design 6.6). Components only give `loopId`, entries and the `LOOP_NO_EXIT` warning.
- **Added for LangGraph:**
  - fan-in barrier analysis (design 6.5);
  - node-name escaping (SP-9: `:`, `|` and a leading `__` become `<readable>~<sha256[:8]>`, collisions are an error);
  - `JoinSpec` from `properties.joinPolicy`, then the manifest;
  - sub-flow closure with `SUBFLOW_CYCLE` and `SUBFLOW_DEPTH_EXCEEDED` (`workflow/blueprint/subflows.py`, one load per level);
  - `BlueprintCache` by `(workflow ID, revision)` on a shared `runtime/lru.py`, which the loader's document cache now also uses.
- **Fan-in rule as built:** a node is a barrier when two different targets of one parallel fan-out reach it and it has two or more predecessors on those paths. A fan-out is one `(node, handle)` with two or more targets; different handles of one node are exclusive. Design 6.5 says the paths must "not pass an exclusive branch point". The rule here is narrower: the two paths must *diverge* at a parallel fan-out. An exclusive branch further down one path does not remove the barrier, because that branch may be taken, and without `defer` the join would then run once per arriving superstep (test `test_D15_an_exclusive_branch_inside_a_parallel_branch_keeps_the_barrier`). Equal-length parallel branches are also barriers. That is harmless, and it is simpler than proving equal arrival.
- **Errors:** every problem is collected and the build fails once. The code is `PLAN_INVALID`, or the problems' own catalog code when all of them share one (`NODE_TYPE_UNSUPPORTED`, `RULE_EXPRESSION_UNSUPPORTED`). Error finding codes: `ENTRY_INVALID`, `NODE_DUPLICATE`, `EDGE_UNKNOWN_NODE`, `NODE_TYPE_UNSUPPORTED`, `NODE_NAME_COLLISION`, `BUILDER_RULE` (`details.rule_id` = `SW-n`).
- **Warnings:** `NODE_UNREACHABLE`, `HANDLE_NO_EDGE`, `EDGE_UNKNOWN_HANDLE`, `EDGE_IGNORED_NODE`, `NODE_TYPE_SKIPPED`, `LOOP_NO_EXIT`, `NODE_NAME_AMBIGUOUS`, and the info codes `NODE_NAME_ESCAPED` and `JOIN_BARRIER_IN_LOOP`.
- **Design 6.4 point 3 is a warning, not an error.** A manifest handle without an edge (for example an approval with no `rejected` edge) gives `HANDLE_NO_EDGE`, as D-08 specifies for rule branches. The run fails with `ROUTE_UNRESOLVED` only if that handle is taken.
- **Feature flags:** a node type whose manifest entry has `enabledBy` (`script` -> `scriptNode`, off by default) fails with `FEATURE_DISABLED` while the flag is off. Three platform documents, one template and the product database's `facility-operations` contain script nodes; the compatibility and live tests turn the flag on.
- **Builder registry:** `scripts/sync_builder.py` now also writes `resources/builder-registry.json` (`BuilderRegistry` model, loaded into `RuntimeResources.builder`) with:
  - node semantics, the 18 node types' handles, output schemas and default configurations;
  - option sets;
  - the 12 SW rules.

  `SOURCE.json` records the registry's SHA-256, so a hand edit fails `--check` even in CI without the builder checkout. With the checkout, the registry is regenerated and compared. The builder commit moved from `90b5885` to `0b9306f` with no fixture or registry source changed.
- **Manifest:** `defaults.errorHandle` (`error`) and `defaults.handoffHandlePrefix` (`handoff-`) added, so the blueprint names no handle.
- **`.gitattributes`** added: vendored SQL, fixtures and the registry are `-text`. With `core.autocrlf=true` and no attributes, a fresh Windows clone would have converted them to CRLF and failed every hash check.
- **Product database:** `new-workflow` (the builder's invalid example, copied with the real workflows) fails SW-1 at plan time, as in the builder.

### P1-06 notes

- **Ported** from the Node.js runtime:
  - `template-resolver.ts` becomes `workflow/templates/resolver.py`;
  - `conditions.ts` and `rule-branches.ts` become `conditions.py`;
  - `state-writer.ts` and `output-mapping.ts` become `updates.py`.
- **The resolver reads a `TemplateScope`**, plain namespace values, because `RunState` arrives only in P1-08. The node template will build the scope from state. `node_lookup(blueprint, manifest)` names nodes through the blueprint's name index, with each type's output aliases.
- **Path resolution:**
  - Special roots (`nodeOutput`, `runtime.workflowMetaData`, `approval.response`, `human.response`) and aliases (`node`, `variable`) come from `templates.json`.
  - Bare names read `flow` and are reported.
  - A node's own templates see the output being built.
  - Under `toolResults`, names match case-insensitively.
  - Missing paths are `null` when the token stands alone and `""` when embedded, and are reported in `TemplateIssues` for the caller's `templates.missing` policy.
- **Output paths:** `{{nodes.X.output.result}}` tries the output's own `result` field, then the type's output alias (`result` → `text` for `llm`, `agent`, `external_agent`), then `nodePathAliases` (`result` → the whole output, for script and tool nodes). The manifest got `io.output` with those aliases on the three text types. The rest of `io` is P1-07's.
- **`templates.json`** gained `writable` (`flow`, `messages`, `thread`; `system` keys still come from `settings.templates.writableSystemKeys`) and `nodePathAliases`.
- **Updates do not write state.** `UpdateWriter.apply` evaluates a node's updates on a copy-on-write view and returns `UpdateResult`:
  - `flow` writes by top-level key, plus `flow_removed`;
  - `system` writes;
  - messages as `{role, content, thread}`, and cleared threads;
  - warnings and missing paths.

  P1-08 turns the result into a `StatePatch` (design rule 15). Later updates of a node see the earlier ones; the scope is never changed.
- **Invalid updates** (an unknown `op`, a bad shape) fail at plan time: the blueprint now validates every `variableUpdates` and `stateUpdates` entry as `StateUpdate` (`UPDATE_INVALID`, under `PLAN_INVALID`). The writer raises `PLAN_INVALID` too, as a backstop.
- **Error codes:** the catalog has no `CONFIGURATION_INVALID` (the Node.js code), so unknown operators and operations are `PLAN_INVALID`. Writes outside the writable namespaces are `STATE_READ_ONLY`. The blueprint's feature-flag error is now `FEATURE_DISABLED`, which is in the catalog and more precise than `NODE_TYPE_UNSUPPORTED`.
- **B-09:** the one stale reference is `{{nodeOutput.toolResults.fast_track_buying_new_questionnaire.text}}` in the fast-track assistant's `knowledge_agent`. The tool's alias there is `Fast Track Buying New Questionnaire`; case-insensitive matching does not bridge spaces and underscores, so the path is `null` with a missing-path report, and P1-07's static check warns `TEMPLATE_PATH_UNKNOWN`. The quick-pay twin is not stale: its tool's `settings.alias` is `quick_pay_complete_questionnaire`, which `toolResults` is keyed by. (A first version of this test keyed `toolResults` by the `.xlsx` file name and blamed the wrong document; corrected in P1-07.) All 117 `{{nodes.<agent>.toolResults.*}}` references name a node the name index resolves.

### P1-07 notes

- **Ported** from the Node.js runtime's `io/` and `models/io/`, with `jsonschema` in place of Ajv:
  - `models/io/payloads.py` (the 12 `@payload` models);
  - `models/io/node_io.py` (`NodeInput`, `NodeOutput`);
  - `io/schema_registry.py`, `io/output_schemas.py`, `io/port_binder.py`, `io/output_mapper.py`;
  - `io/specs.py` (start-up check and node-type description).

  The 17 `io` blocks and the 6 `schemaExtensions` in `node-runtime.json` are Node's, with schema strings written as `{ "ref": ... }`.
- **Deviations from plan and design:**
  - The template checker is `workflow/blueprint/template_checker.py`, not `io/template_checker.py`: it needs the template resolver, and `io` sits below `workflow` (design 4.3). The port binder takes `resolve` and `read` callables for the same reason.
  - `workflow/loader/paths.py` moved to `runtime/paths.py`, so every layer can use it.
  - Design 8.2 composes four parts into the effective output schema. This build composes three (payload, manifest extensions, the node's own `outputSchema`), as Node.js did. The builder's vendored `outputSchema` is held by the I-02 contract test instead: the runtime payload declares every builder field with an equal or narrower type. Composing it would narrow fields the builder types loosely (Human Input `value` is a string there; form answers are objects).
- **`zonic.text@1` declares `result`** (the parsed answer of an LLM with structured output; absent otherwise), and the llm map has `result: $.result`. Without it, `{{nodes.request_parser.output.result.<field>}}` resolved through the `result` → `text` alias to a string. Agent and external-agent payloads have a real `result` field (Node's design), so the P1-06 interim aliases on those two types are gone; only `llm` keeps `result` → `text`.
- **Stricter than Node.js in two places:**
  - A path below a typed scalar (`rowCount.deeper`) is unknown.
  - The null branch of an optional field (`anyOf [schema, null]`) does not open a path.

  An LLM output stays open, because its map spreads `$.fields`. The I-09 tests therefore use `db_query`, whose `zonic.rows@1` output is closed.
- **Blueprint:** `NodeSpec` carries `io` and its effective `output_schema`. `BlueprintBuilder(resources, schemas=None)` builds a `SchemaRegistry` when none is passed; the app factory (P2-03) will pass the process registry from `build_schema_registry()`, which raises `RESOURCE_INVALID` naming every bad `io` block.
- **Findings:** `TEMPLATE_PATH_UNKNOWN` has severity `settings.io.templatePathUnknown`; as an error the plan fails with that code. `TEMPLATE_BARE_NAME` is `info`. Over the 45 documents the only unknown path is the stale fast-track reference (regression test `test_I09_the_vendored_documents_have_only_the_known_stale_reference`).

### P1-08 notes

- **DR-1 as built** (`engine/state.py`):
  - `flow` and `system` are keyed channels `{values, provenance, conflicts}`. Every write carries `(step, writer)`; per key the highest `(step, writer)` wins. Two writers of one key in one superstep are a conflict: the higher node ID is kept, and the conflict is recorded with every writer.
  - `result` keeps the lowest `(step, writer)` and lists the Outputs it ignored.
  - List namespaces hold an order key `_seq = [step, writer, index]` and stay sorted.
  - `nodes` keeps the highest visit; `visits` and `usage` add.

  Hypothesis tests show each reducer gives the same value under any order of same-step writes. A real two-branch LangGraph run keeps both branches' keys, resolves the shared key, and never raises `InvalidUpdateError`. The step is `config["metadata"]["langgraph_step"]`, as LangGraph 1.2.17 provides it.
- **`system` is a keyed channel too**, not the plain merge of DR-4's text. A plain dict merge is order-dependent when two parallel nodes write the same writable key, which would break the determinism DR-1 asks for. DR-4's enforcement is as planned: `to_update` raises `STATE_READ_ONLY` for keys outside `writableSystemKeys` before the update leaves the node.
- **`FlowPatch`** gained `remove` (keys a state operation deleted) and allows step `-1` (the turn loader).
- **Reading state:** `engine/scope.py` builds the resolver's `TemplateScope` from state:
  - `values` only, never provenance;
  - messages are history then turn messages, every thread.

  A thread `clear` is a marker entry in `turn_messages` that hides that thread's earlier messages; the markers are not messages and are skipped at turn end (P2-04).
- **`engine/patches.py`:** `to_update(patch, step, writer, writable_system_keys, cleared_threads)` stamps every namespace. `flow_patch()` and `update_messages()` turn P1-06's `UpdateResult` into patch parts. Update-written messages are `ThreadMessage(kind="user_input" | "agent_turn")`, visible in chat only on the `messages` thread. Content parts are `{type: text|json}` until the content models arrive (M-06).
- **`engine/context.py`:**
  - `RunContext`, `RunIdentity`, `Deadline` (monotonic, injectable clock);
  - `RunPermits` (run-wide node semaphore);
  - `RunEmitter` (wraps the stream writer, never raises; dropped events are counted, the one documented rule-17 waiver);
  - `RunPorts`. The ports and the governor and hooks fields that later tasks provide are `None` until then.

### P1-09 notes

- **Compiler** (`workflow/compiler/workflow_compiler.py`): one `add_node` per compiled spec, carrying:
  - `defer` from the blueprint's join;
  - a `RetryPolicy` whose `retry_on` accepts only `ZonicError` codes listed in the manifest (none when `maxAttempts` is 1);
  - a `TimeoutPolicy` from the spec, with `set_node_defaults` from `engine.nodeTimeoutSeconds`;
  - `destinations` as `{target: handles}` for `get_graph()`;
  - the trace metadata.

  The only static edge is `START` → entry (G-02). The role decides the checkpointer (DR-3).
- **Node actions** come from a `runner_factory(spec, blueprint)`. P1-09 tests use the scripted test kind (`tests/kinds/scripted_kind.py`), which reads the run only through `runtime.context`. P1-10's `WorkflowNodeRunner` will be the production factory, and its `ErrorPolicyHandler` the `error_handler_factory` (none yet).
- **Routing** (`engine/routing.py`): `*` activates every edge. An activated handle without an edge ends the path when the handle is optional or the node is a sink; otherwise it is `ROUTE_UNRESOLVED` (D-08: only when taken). This refines design 6.4 point 3: a node with no outgoing edge (a last node that is not an Output) simply ends its path.
- **Graph cache** (`workflow/compiler/graph_cache.py`): LRU keyed by workflow, revision, role, a content hash of the manifest, and the tool catalog version (`"0"` until P3-01).
- **Invocation** (`engine/invocation.py`): `thread_id` = run ID, `recursion_limit` from `engine.supersteps` (`childSupersteps` for children), `max_concurrency` per graph, metadata and tags. Documents carry no superstep limit today; their visit and step limits are P1-11's.
- **G-09:** a hypothesis test generates random acyclic documents with parallel fan-outs, rules and joins. The executed-node multiset equals a test-only reference interpreter's (`tests/kinds/reference.py`: supersteps, double triggers run once, barriers wait for an idle step). A forced-failure probe confirmed that the async property test really runs its examples.
- **G-11:** an object-graph walk (`gc.get_referents`) finds no `RunContext` in a compiled graph after runs, and does find one when one is present.
- **F-15 snapshots** (`tests/fixtures/compiled/`, 42 files): each records the entry, and per node its ID, kind, `defer`, retry attempts, timeout, destinations and loop ID. Regenerate with `ZONIC_UPDATE_SNAPSHOTS=1 uv run pytest tests/compat/test_compiled.py`, and review a changed snapshot like code.

### P1-10 notes

- **LangGraph 1.2.17 → 1.2.18** (decision 0002, for M1 review). With 1.2.17 a node's `error_handler` ran, but the handled error was re-raised at the end of any run where the failing task ran in parallel or the custom or messages stream was on. That was every streamed run, so the error policies of design 8.8 did not work. 1.2.18 fixes it in `AsyncBackgroundExecutor.__aexit__`. All 63 spike checks pass on 1.2.18 (restored from `bcd2fde`, run, removed).
- **Node template** (`engine/node_template.py`): `WorkflowNodeRunner` runs the 11 steps of design 8.1. Steps 9 to 11 are shared with the error handler.
  - `NodeContext` is one visit: the spec, the state snapshot, `runtime`, `config`, the superstep from `langgraph_step`, the visit, and template helpers. `config_values()` resolves only `TemplateField`s (C-02).
  - `NodeServices` holds the process-wide resolver, evaluator, schema registry, binder, mapper, writer and an injectable clock.
  - Events go through `runtime.stream_writer`: `node.started`, `node.finished`, and `node.failed` from the error handler.
  - The visit and step limits of step 1 are P1-11's.
- **Template details:**
  - The output is `output.map`, then `outputStructure`, then the effective schema.
  - `writeTo` (from `outputKey` or `outputVariable`) is applied as a `set` before the node's own updates (B-10's "outputs also at the named paths").
  - Missing template paths become one `TEMPLATE_MISSING` warning per visit (the `templates.missing: error` policy per node type is still to come).
- **Kinds:** `engine/kinds/base.py` (`NodeKind` protocol, `BaseKind`), `engine/kinds/registry.py` (`KindRegistry`: manifest kind → factory; an unregistered kind is `NODE_TYPE_UNSUPPORTED` at compile). `NodeOutcome.result` lets the Output kind (P2-02) set the run result.
- **Error policy** (`engine/error_policy.py`):
  - `ErrorPolicyHandler(state, error: NodeError, config, runtime)`. `NodeError` is injected by the parameter's type annotation.
  - The `onNodeError` hook may recover.
  - `fail_run` re-raises with the node's code. `continue` and `route` record the error (node record `failed`, timeline, `errors`) and follow `default` or `error`.
  - `engine/errors.py` maps exceptions to catalog codes: `NodeTimeoutError` → `NODE_TIMEOUT`, `GraphRecursionError` → `LOOP_LIMIT_EXCEEDED`. Retry policies use the same mapping, so `NODE_TIMEOUT` can be listed as retryable.
- **`engine/child.py`:** `invoke_child()` passes the parent's config (callbacks, tags, metadata) with the child superstep limit, and `context=runtime.context` (G-14).
- **`engine/result.py`:** `run_result()` takes the first Output's result and reports the others as `OUTPUT_IGNORED`. Otherwise it applies `settings.result.fallback`: a `last_<kind>_text` name names the kind, so no kind literal appears in code. Otherwise `NO_OUTPUT` (E-18).
- **C-13 so far:** `NodeSpec.observation` (manifest defaults, then the type's entry) is carried as `zonic_observation_type` in the node metadata. Parent and level are verified with the Langfuse exporter (P2-07).
- **C-14:** a `NodeHooks` protocol (`before_node`, `after_node`, `on_node_error`) is read from `RunContext.hooks`. P2-06 provides the real `HookRunner`.
- **Conformance suite** (`tests/conformance/test_kinds.py`): runs on the test kinds of `tests/kinds/harness.py` in one-node and join graphs. Each real kind adds itself to `SUBJECTS` when it arrives (P2-02 onward).
- `catalog_check` reads only the first case ID of a test name, so tests that prove two cases are split (C-06, C-07).

### P1-11 notes

- **Limits** (step 1 of the template, `WorkflowNodeRunner.check_limits`): a visit past `maxVisitsPerNode`, or a run step past `maxSteps`, is `LOOP_LIMIT_EXCEEDED` (`details.limit` names which). Both read the superstep's snapshot plus this visit, so parallel siblings can overrun `maxSteps` by up to `max_concurrency - 1`, as accepted in DR-7. `recursion_limit` stays the backstop, and `GraphRecursionError` maps to the same code.
- **Run-fatal codes** (`LOOP_LIMIT_EXCEEDED`, `RUN_TIMEOUT`, `CANCELLED`) end the run whatever the node's error policy, and skip `onNodeError`. Without this, a `continue` policy could keep a loop alive past its limit.
- **Document limits:** `WorkflowDocument.limits` (`maxVisitsPerNode`, `maxSteps`) is capped by `settings.engine.maxLimits` into `Blueprint.limits`. The builder does not write it yet; the Node.js runtime read the same key. Neither the builder schema nor any fixture has it.
- **`loop.iteration`** is emitted when a loop entry node is visited again, with `loopId` and the iteration. `zonic_loop_id` is on every looped node's metadata.
- **Test kinds:** `PolicyKind` with `answer_after(n)` (agents hand off `n` times, then answer; rules take their default) and `seek_loop()` (every node takes the handle nearest a loop, so only a limit ends the run). The harness seeds `system.userQuery`, which agent prompts bind, as every real run has one.
- **F-06 as built:**
  - All 11 loop documents (8 platform, 3 synthetic) run to completion within their limits.
  - With `seek_loop()`, each platform document ends with `LOOP_LIMIT_EXCEEDED` after at most `maxVisitsPerNode` calls per agent.
  - `all-agents` runs exactly one of its three exclusive sub-flow nodes.

## Checkpoint M1 (2026-10-26): approved

| M1 criterion | Evidence |
|---|---|
| Every vendored builder and platform document loads, normalizes and compiles to a `StateGraph` matching its committed snapshot (F-15, F-16) | `tests/compat/test_compiled.py`: 42 snapshots; `tests/compat/test_dialects.py` F-16 |
| Joins after exclusive branches run once; untaken branches never scheduled (E-03) | `test_E03_exclusive_branches_join_once_and_the_other_branch_never_runs` |
| Joins after unequal parallel branches run once as barriers (E-04) | `test_E04_unequal_parallel_branches_join_once_with_both_outputs`; G-09 property test against a reference interpreter |
| Loops, including the 6-entry loop, run and stop at the limits (E-07 to E-09) | `tests/unit/test_loops.py`, `tests/compat/test_loops.py` |

**Gates:** ruff, ruff format, mypy (strict), import-linter (4 contracts), `gen --check`, `sync_sql --check`, `sync_builder --check`, `catalog_check` (114 cases with tests, 102 due, 22 tasks complete): all pass. 1,140 tests pass; the live suite passes 24 (the batch test is skipped, as before).

**For the reviewer's decision:**

1. **Decision 0002:** LangGraph 1.2.17 → 1.2.18, which fixes `error_handler` in parallel and streamed runs. All 63 spike checks pass on 1.2.18.
2. **Design deviations to accept or reject:**
   - The fan-in rule's handling of exclusive branches after a fork (P1-05).
   - `HANDLE_NO_EDGE` is a warning, not an error, and a sink ends its path (P1-05, P1-09).
   - Three schema parts instead of four in the effective output schema (P1-07).
   - The template checker lives in `workflow/blueprint/` (P1-07).
   - `system` is a keyed channel like `flow` (P1-08).
   - Document `limits` is runtime-only (P1-11).
3. **Assumption to confirm:** `security.activities[].code` is matched against the `cognito:groups` claim for catalog visibility (P1-04).
4. **Bug fixed in the port, also present in Node.js:** `routingTools` dropped handoff tools' preset `parameters` (P1-03).
5. **Still open from earlier:**
   - re-own the 9 workflows to the Docker builder user;
   - the builder's `LIVE_GATEWAY_URL` secret and `cdk.json` production datasource;
   - per-item batch authorization in the gateway;
   - the two Node.js SQL defects.

### M1 review outcome (2026-10-26)

1. **LangGraph 1.2.18:** accepted (decision 0002).
2. **Design deviations:** the reviewer delegated these. All six are accepted, with reasons, in decision 0003, and the design and plan text is amended to match.
3. **Visibility:** one security model, Cognito (decision 0004). Changes:
   - `WorkflowLoader` lists and loads without a group check.
   - `CatalogAgentRow` drops `security` and `activity_codes()`.
   - `WorkflowCaller` drops `groups`.
   - The `004_views.sql` comment is updated. The view itself is unchanged, so nothing is re-applied to the product database.
   - The loader tests now assert that a workflow carrying `security.activities` is listed for every caller.
4. **Still open, carried into Phase 2:**
   - the `routingTools` fix to report to the Node.js project;
   - re-owning the 9 workflows;
   - the builder's `LIVE_GATEWAY_URL` secret and its `cdk.json` production datasource;
   - per-item batch authorization in the gateway;
   - the two Node.js SQL defects.

## Phase 2

| Task | Status | Sanity check |
|---|---|---|
| P2-01 Run driver and event stream | Done | S-02, S-03, S-05, S-11, S-12, S-13, E-17, R-06: 26 tests; v3 and v2 sources publish identical events |
| P2-02 Agent layer and core node kinds | Done | N-01 to N-08, N-12, N-19, M-09, B-02 to B-04, S-09, D-13: 30 tests (N-05's MCP and database tools wait for P3-02) |
| P2-03 API, use cases, app factory and test client | Done | A-01 to A-07, A-09 to A-12, A-14, A-15 (start and stream; interrupt and dataset steps join with P4-02 and P3-05), S-01, S-06 to S-08, S-10, M-06 (events): 33 tests through HTTP with fakes |
| P2-04 `TurnSaver` and per-turn persistence | Done | P-05 to P-09, P-11, P-13, E-19, A-07, A-08 (sync part): 11 tests; the checkpointer conformance suite passes all base capabilities |
| P2-06 Hook framework and core hook types | Done | K-01 to K-07, K-09, K-10, K-12, K-15, K-16: 25 tests |
| P2-07 Langfuse tracer | Done | L-01 to L-05, L-07, L-08, L-10, L-12, L-13, L-16, L-17, X-06: 15 tests on an isolated provider with an in-memory exporter |
| P2-05 Run executor and governors | Done | Q-01 to Q-09, Q-16, R-02, R-04, and Q-15 early: 26 tests. Q-05 runs 150 concurrent streaming runs in process; the k6 budgets are P5-02's |
| P2-08 Gateway span exporter | Done | O-01 to O-06 (exporter side; the SQL side of O-02 to O-04 is P0-09's), L-11: 15 tests. Production now traces through `sp_obs_ingest` |
| P2-09 Builder scenarios | Done (decision 0005) | F-07 plus the coverage gate: 9 tests. 3 of the 32 vendored workflows plan with Phase 2 kinds and run in stream and sync mode with golden streams; `new-workflow` is refused. F-08, F-10, F-17, F-18, B-12 and B-13 need Phase 3 kinds; 0005 proposes moving them to those tasks |

### P2-01 notes

- **Event contract** (`models/events/run_events.py`): the 21 event types and their data are the Node.js runtime's, so one client reads both runtimes. The envelope is `{id, runId, seq, type, ts, nodeId?, path?, truncated?, data}`. `RunEvent` is a discriminated union, and every type is published under `generated/schemas/events/` (22 files). On the hot path the stream builds plain dictionaries from an `EventDraft`; the tests validate every published event against the models.
- **One mapper, two sources** (`engine/streaming/`):
  - `EventMapper` is the only code that reads LangGraph event shapes. `NamespaceMap` turns namespace segments back into builder node IDs (`path` = the enclosing nodes).
  - `V3EventSource` drives `astream_events(version="v3")` with `CustomTransformer`, `ToolCallTransformer` and the per-run `RunEventTransformer` (SP-3). The transformer's `zonic` channel is local (unnamed), so the drafts never enter LangGraph's main log. That differs from the design text's named `StreamChannel("zonic")`.
  - `V2EventSource` is the fallback on `astream(..., subgraphs=True, version="v2")`. It also requests the `tools` mode, which `astream` accepts although `StreamMode` does not list it. The same `StreamToolCallHandler` is then attached, so tool events are identical to v3's, and S-12 compares the two streams exactly.
  - Not requested: the `updates` mode the design lists. No run event reads it.
- **Template events:**
  - `node.finished` now carries `outputSummary` (types and sizes from `run/events/summarizer.py`, never values). The output reaches the mapper by reference and is summarized there.
  - The template emits `state.updated` (changed `flow` keys) and `warning` events.
  - The error handler emits `node.finished` with `status: failed`, `errorCode` and `policy`, instead of the earlier `node.failed`, which is not in the contract. Two Phase 1 tests now assert the contract event. C-09 still asserts that `node.started` and `node.finished` are a node's only `node.*` events and come first and last.
- **Bus** (`run/events/bus.py`):
  - `RunStream` assigns `seq` with no gaps, starting after `start_seq` (`last_event_seq`, DR-9).
  - It caps data at `sse.maxEventBytes` (`truncated: true`) and keeps the last `stream.replayBufferEvents` events.
  - A subscriber behind the buffer gets `stream.gap` and continues; the publisher never waits.
  - `RunEventBus` releases a stream `replayRetentionSeconds` after it closes.
  - Token coalescing is P2-05's.
- **Driver** (`run/driver.py`):
  - Runs the source as an inner task against the run's `Deadline` and an `ActiveRun.cancel()` signal.
  - Maps the end to `succeeded`, `waiting_input`, `drained`, `timed_out` (`RUN_TIMEOUT`), `cancelled` or `failed` (catalog code, `INTERNAL` for unknown codes).
  - Builds the result with `run_result` (`NO_OUTPUT` otherwise).
  - Calls the `TurnFinisher` (P2-04), then publishes warnings and the terminal events: `run.completed`; `interrupt.requested` and `run.waiting`; `run.cancelled`; `run.failed`; none after a drain.
  - A failed save (`ZonicError`) becomes `run.failed` with that code.
  - New setting `runs.cancelGraceSeconds` (5) bounds the wait for a cancelled graph. LangGraph does not wait for a node that ignores cancellation, so in practice the run ends at once.
- **`durability` needs a checkpointer:** without one, LangGraph warns that it has no effect. Root graphs always have the `TurnSaver` (P2-04). Until then the driver tests compile with an `InMemorySaver`.
- **Test kinds** (`tests/kinds/streaming.py`): `TalkKind` streams a scripted model turn, and `ToolAgentKind` runs `create_agent` with two tools through `invoke_child`, until P2-02's real kinds. Two tool calls in one model turn run concurrently in threads, so their order is not fixed in either source. The S-12 scenario calls one tool per turn, and the parallel case is asserted as sets.
- `Clock` and `SystemClock` moved to `runtime/clock.py`, so the run layer can share them.
- `pyproject.toml` ignores `LangChainBetaWarning` in tests (v3 is beta, decision 0001).

### P2-02 notes

- **Agent layer** (`engine/agents/`):
  - **`ModelFactory`:** resolves a `ModelRef` through `models.json` (catalog code, alias, or provider model ID; empty means the default). Anything unknown or disabled is `MODEL_NOT_ALLOWED`. Parameters are bounded by the entry, retries and timeout come from `settings.models`, and the model is built by a per-provider `ModelBuilder` (`init_chat_model` with `bedrock_converse`, `azure_openai`, `anthropic` or `openai`). Azure takes its deployment, API version and endpoint (from the environment field the entry names). Bedrock gets no client timeout or retry count, since botocore configures those (P2-05).
  - **`AgentFactory`:** builds one `create_agent` per node with `state_schema=ZonicAgentState` and `context_schema=RunContext`. The middleware is `TurnLimitMiddleware` (`maxIterations`) and `HandoffMiddleware`.
  - **`AgentServices`:** joins the model factory, the tool provider and the agent factory, and `NodeServices.agents` carries it.
  - **Tool provider:** `ToolProvider` is a port; it returns no tools until P3-02's `ToolFactory`.
- **Kinds** (`engine/kinds/`): `entry`, `llm`, `agent`, `router`, `variable`, `output`, `ignore`; `core_kinds()` names them from their class names. Behavior follows the Node.js kinds: message building, splitting out `system`, the last user message as the input, the answer's declared fields, handoff and routing tools.
  - An LLM node without tools makes one model call (`with_structured_output` when it has a schema). With tools it runs a short agent loop of at most `models.llmMaxTurns` (new setting, 5).
  - The agent graph and the model are built on a node's first visit and kept by its kind for the life of the compiled graph.
- **Structured output** (`structured.py`, M-09):
  - The adapter adds `title` and `description`.
  - It uses `ProviderStrategy` when the catalog entry supports native structured output, else `ToolStrategy`.
  - Neither validates a JSON Schema dictionary, so `check` validates the answer with `jsonschema`: `STRUCTURED_OUTPUT_INVALID`.
  - An LLM node uses the platform's `structuredOutput` even without `responseFormat: json`. The platform documents' variable updates read `structuredOutput.*`; the Node.js LLM kind ignored it there.
- **Handoff** (`handoff.py`): `handoff_to` has an enum of the targets' names. An invalid name fails the call's validation, the model sees the error and may choose again. A routing tool keeps its name and JSON Schema, merges its preset `parameters` (resolved per visit, passed in `ZonicAgentState.zonic_presets`), and records `toolResults[<name>]`. The tools must annotate the injected argument as plain `ToolRuntime`; with `ToolRuntime | None` LangChain does not inject it.
- **`maxIterations`:** `TurnLimitMiddleware` subclasses `ModelCallLimitMiddleware(exit_behavior="end")`. Instead of the parent's extra limit message it records `zonic_limited`, so the answer is the last real model text, `stopReason` is `limit` and the node warns `AGENT_TURN_LIMIT`.
- **Provider errors** (`errors.py`, N-08): only the SDKs' own error classes (botocore, OpenAI, Anthropic, httpx) become `MODEL_UNAVAILABLE`, which the LLM and agent retry policies retry (rule 17: no broad `except`).
- **Turn messages:**
  - An agent's tool calls, tool results and intermediate turns are `internal`, and its answer is a chat message.
  - When a node's own updates also write the same text to the chat thread (the platform agents append `{{nodeOutput.text}}` to `thread.messages`), the template keeps that message once.
- **`NodeOutcome.updates`:** the Variable kind's `updates` go through the template's update step (step 9) before the node's `variableUpdates`. The kind computes its `changed` output with the same writer.
- **Token events at the root** (correction to P2-01): a model called directly by a root node has an empty v3 namespace. The mapper now places tokens by the message metadata's `langgraph_checkpoint_ns`, and `NamespaceMap` skips segments that are not builder nodes (the agent's own `model` and `tools`). A tool called directly by a root node has no such path; P3-06's tool kind reports its own events.
- **`nostream`** is a tag on the model (`tags=["nostream"]`), not on the invocation: an invocation tag would also hide the node's tool events (LangGraph's tools handler checks it too).

### P2-03 notes

- **Contract:** the request, response and error models (`models/api/`), the routes, the `X-Run-Id` and `X-Session-Id` stream headers and the run event types follow the Node.js runtime's API, the Strands design's Section 12, so one client serves both.
  - `generated/openapi.json` is the committed document (A-10). It includes `RunEvent` for the SSE bodies, and every `/v1` route documents `ErrorEnvelope` as its default response. Path parameters are `{runId}` and `{sessionId}`.
  - The API models are not exported as separate schema files: the OpenAPI document publishes them.
  - An event's `ts` is an ISO 8601 string with milliseconds, as in the Node.js contract (a `datetime` field would re-serialize with microseconds and break M-06's round trip).
- **App** (`main.py`): `create_app(deps)` with a lifespan that drains this instance's runs and closes the gateway; CORS from `api.corsOrigins`; API docs only when `api.docsEnabled`. `app_factory` builds from the environment for `uvicorn --factory`, and `create_openapi()` feeds `scripts/gen.py`.
  - `RequestContext` is pure ASGI middleware, so it never buffers a stream: it sets the request ID (`X-Request-ID`, echoed, the envelope's `traceId`) and returns `INPUT_TOO_LARGE` above `api.maxBodyBytes`.
- **Auth** (`api/auth.py`, decision 0004):
  - PyJWT verifies RS256 against the pool's JWKS, fetched with `httpx`, because PyJWT's `PyJWKClient` blocks the loop (rule 13). Keys are cached `auth.jwksCacheSeconds`, and an unknown `kid` refreshes them.
  - The audience is `aud` or, for Cognito access tokens, `client_id`. Issuer and audience come from `OIDC_ISSUER` and `OIDC_AUDIENCE`.
  - No token is `AUTH_REQUIRED`; anything wrong with one is `AUTH_INVALID`.
  - Rate limits are token buckets per user and per tenant: `RATE_LIMITED`, `Retry-After`.
- **Use cases** (`application/`): catalog and node types, sessions (cursor paging on `(created_at, session_id)`, title search, internal messages for `auth.internalMessageRoles`), and runs.
  - **StartRun:** plans and compiles before the run row exists, then calls `sp_run_start`, and drives the run as its own task (`RunRegistry`).
  - **Modes and replays:** `stream` answers with the SSE of the run's stream, and `sync` waits `runs.syncWaitSeconds`, then answers `202` with links. An idempotent replay returns the first run.
  - **Reads:** status (with an `ETag`), the node timeline, and events by `Last-Event-ID` (`stream.gap` and the final event from the row once the buffer is gone).
  - `async` mode is `FEATURE_DISABLED` until P4-04.
- **SSE** (`api/sse.py`): `id`, `event` and `data` lines, and a heartbeat without `id` after `sse.heartbeatSeconds` of silence. A disconnect ends only the response; the run goes on and is saved (S-07).
- **Persistence for now** (`run/state/turn_store.py`): `sp_run_start` with the user message and limits, and `sp_run_finish` with status, result, error, usage, the turn's messages, the timeline and `lastEventSeq`.
  - The root graphs' checkpointer is an `InMemorySaver` whose thread the store deletes after the save.
  - P2-04 replaces the store with `TurnStateStore`: the start batch, session variables, history, retries until the save deadline, and `TurnSaver`.
  - A `waiting_input` or `drained` outcome is not saved yet (P4-02, P4-05).
- **Fakes:**
  - `tests/fakes/runtime_schema.py` ports the Node.js in-memory runtime schema (sessions, runs, messages, session state, feedback) with the SQL's reason tokens through `translate_status`.
  - `InMemoryGateway` gained computed views.
  - `tests/api/app.py` composes the app with a test issuer (an RSA key signing Cognito-shaped access tokens) and scripted models.
- **New settings:**
  - `api.defaultPageSize` (20), `api.maxPageSize` (100) and `api.sessionTitleChars` (80);
  - `auth.internalMessageRoles` (`runtime-support`, `runtime-admin`).
- **Test client:** `scripts/run_client.py` lists the catalog, starts and prints a stream, and continues a session, all through the published API. A-15 runs it against the app with fakes.

### P2-06 notes

- **Framework** (`hooks/`), ported from the Node.js runtime:
  - `HookRegistry`: types by name.
  - `HookResolver`: global bindings from `hooks.json` validated at start-up (`RESOURCE_INVALID` naming the binding's path, K-15); workflow (`runtimeHooks`) and node (`properties.hooks`) bindings limited to `hooks.documentAllowedTypes` (`DOCUMENT_INVALID`, K-10); matching, `disabledHooks`, order then source; cached per revision.
  - `HookRunner`: `when`, blocking and asynchronous execution, timeouts and failure policies, the actions each point allows, the routing checks.
  - `TemplateHooks`: the adapter at the template's, the driver's and the agent middleware's points.
- **Blueprint and document:** the blueprint now carries `runtime_hooks`, and each `NodeSpec` carries `hooks`, `disabled_hooks` and `tags` from the node's `properties`. `WorkflowDocument.runtime_hooks` is a new runtime-only field. `WorkflowRuntime.prepare` resolves the hooks, so a bad document binding fails before the run row exists.
- **`HookBinding` defaults** are the Node.js runtime's: order 100, blocking, `fail_node`, overridable, enabled; the timeout comes from the new `hooks.defaultTimeoutMilliseconds` (5000). `when` is the Rule node's condition group, evaluated with the same evaluator.
- **State changes are results, never side effects.**
  - A hook returns `HookResult` (`patch`, `handles`, `updates`, `code`, `delay_ms`, `message`). `setVariables` returns state operations that the template applies at step 9, together with the node's own, through `NodeOutcome.updates`, because under LangGraph a node's writes are its returned update.
  - At `beforeRun` the driver applies them to the turn's `flow`.
- **Template protocol changes:**
  - `HookDecision` gains `updates` and `warnings`.
  - `on_node_error(ctx, error, attempt)` returns `ErrorDecision` (`recovered` or `retry_after`). `ErrorPolicyHandler` retries the visit up to `hooks.maxRetriesPerNode`, under the node's timeout.
  - A hook's `fail_run` is a `ZonicError` detail, and the error handler treats it like a run-fatal code.
  - An output a hook gives (`skip`, `recover`, or a changed `afterNode` output) is `NodeOutcome.mapped`: it is output data and is revalidated, not mapped again.
- **`HookMiddleware`** sits first in the agent stack:
  - `beforeModelCall` `cancel` answers in place of the model;
  - `afterModelCall` `retry` calls once more;
  - `beforeToolCall` `cancel` returns the hook's message as the tool's error result;
  - `afterToolCall` may replace the result.
  At these points hooks read the call, not run templates (the agent state is not the run state).
- **Driver:**
  - `beforeRun` may reject the run or set variables.
  - `afterRun` may transform the result before the save; a failing `afterRun` fails the run.
  - `onRunError` adds warnings only.
- **New catalog codes:** `HOOK_TIMEOUT`, `HOOK_WARNING`, `HOOK_IGNORED`, `HOOK_NOT_OVERRIDABLE`. The three warning codes have status 200.
- **Left to P3-10:** the other hook types (`mask`, `guardrail`, `approval`, `cache`, `rateLimit`, `webhook`, `mcpTool`, `annotate`), the swarm and interrupt points, and hook observations (P2-07).

### P2-07 notes

- **Tracer** (`integrations/langfuse/provider.py`): the runtime's own `TracerProvider`, never the global one, and a Langfuse client with:
  - `span_exporter=` the exporter it is given (the gateway exporter of P2-08; an in-memory one in tests), behind `SamplingExporter`;
  - `mask=` the `Masker`, `mask_otel_spans=` the `Enricher`, and `should_export_span=keep_span`.

  The client gets a per-tracer public key and a placeholder server address (`config/env.NO_LANGFUSE_SERVER`) that is never contacted. A run's `LangfuseRunTrace` opens the root observation with the run ID as trace ID, under `propagate_attributes` (trace name, session, user, tags, metadata), and gives the run its callbacks and event bridge.
- **Port** (`ports/tracer.py`): `RunTracer`, `RunTrace`, `TraceInfo`, and `NoopTracer` (L-16). The run service begins a trace per run, its callbacks go into the invocation config, `RunContext.tracer` holds it, and the driver runs inside `trace.active()` and ends it with the outcome. `AppDeps` takes a tracer; production uses `NoopTracer` until P2-08's gateway exporter exists.
- **SP-4 applied:**
  - The callback handler's own types are not trusted. `ZonicLangfuseCallbackHandler` marks node visit spans (the children of the graph's root run, error handlers included) with `zonic_node_span` and `zonic_visit` (from the state's visit counts).
  - The `Enricher` retypes them from `zonic_observation_type` (L-02, L-03).
  - It unwraps the node's `Command` output to the node record's output, captured per node type (DR-5, L-17).
  - Model and tool spans follow their node type's capture rule (L-10), and the node input, which is the run state, is a summary at most.
  - The agent's `model` and `tools` wrapper spans are dropped. Re-parenting their children is P2-08's job (SP-4).
- **Levels:** a node whose error policy handles a failure (`continue`, `route`) ends its observation `WARNING` with the code, while `fail_run` keeps `ERROR` (L-07). The error handler's run is marked as the failed node's (`zonic_error_handler`), and its failed record sets `WARNING` (L-17).
- **Events:** `NodeContext.trace` adds `event` observations under the node's observation through `ObservationBridge`, which finds the node's run from its config's callback manager and makes its span current, because a Langfuse event takes its parent from the context. The template sends `loop.iteration` (L-04), warnings, and `route` with the handles when a node activates something other than `default` (L-08).
- **Sampling** (L-12): Langfuse's own `sample_rate` decides when a trace starts, before the outcome is known, so it stays 1.0. `SamplingExporter` holds a trace's spans until the run's root span arrives, then keeps the trace by `observability.sampleRate` (by trace ID) or because an `ERROR` span is in it (`alwaysTraceErrors`). The buffers are bounded and locked, since export runs on the processor's thread.
- **Masking** (X-06): sensitive keys, the patterns (e-mail, card numbers, bearer tokens) and field caps are applied to every input, output and metadata value. JSON text is masked as JSON. The run stream carries only summaries (P2-01).
- **Logging policy** (`observability/logging_policy.py`, L-13): one handler at `WARNING`; records inside a run become `event` observations on its trace (through a context variable the trace sets), and records outside a run are dropped. `app_factory` installs it at start-up.

### Phase 2 on the real gateway (2026-10-27)

- **Default gateway:** `config/env.py` now defaults `gateway_base_url` to the deployed Lambda gateway (tripleq-development) instead of `127.0.0.1:8600`. `.env` already pointed there; other environments set `ZONIC_GATEWAY_BASE_URL`.
- **Live suite:** `tests/live/test_runtime_live.py` (opt-in, `uv run pytest -m live`) runs the app with the real `HttpGateway` and the production tracer. Only the model is scripted and the token is the test issuer's. It runs the live catalog's `test-agent-workflow`.
  - **Pass:** a new-session turn in sync mode, saved and read back (run, node timeline, messages newest first); a streamed turn; the trace ingested through `sp_obs_ingest`, with `v_obs_trace` and one node observation per visit in `v_obs_observation`.
  - Whole live suite before the fix: 27 passed, 1 skipped (the batch probe), 1 xfail.
- **Blocker found:** a second turn on a session fails with `500 DATA_REQUEST_INVALID`.
  - The cause: the deployed gateway refuses `/v1/batch` to `zonic-agent-runtime` with `403 AuthorizationError`, because the gateway has no per-item datasource authorization. A warm turn starts with one sequential batch (`v_session_state`, `v_message`, `sp_run_start`: P2-04, case P-05).
  - The fakes allowed the batch, so the offline suite could not see this. The test is a strict `xfail` and flips when the batch is allowed.
- **Fixed (option A, 2026-10-27):** the gateway already checks every batch item against the caller's own rule (`security/gate.py`), so `ZonicPostgresqlServer_FastAPI/postgresqlmodule/code/config/pgfoundation.lambda.yaml` now grants `batch` to `zonic-agent-runtime` (datasources `agentruntime`, `agentobservability`, `batch`).
  - Redeployed with `deploy/aws-lambda/deploy.sh` to account 001634074733: same Function URL, credentials kept from Secrets Manager, all five datasources healthy. The gateway's contract and service tests pass (180).
  - Live suite: 30 passed. The warm turn now works and reads its history; the transactional batch works; a batch item for a datasource the caller lacks (`neondb_dev`) is still refused with `403`.
  - The config change is uncommitted in the gateway repository.
- **Rows written:** live runs leave rows in the product database (`agent_runtime` sessions, runs, messages; `agent_observability` traces), all owned by `live-langgraph-verify`, tenant `live-test`.

### P2-09 notes

- **Framework** (`tests/scenarios/builder/`):
  - A scenario names a vendored fixture and, for each turn, the user's text, the scripted model turns and the expected path of node names.
  - Each scenario runs through the app in stream mode and again in sync mode. The sync path comes from `GET /v1/runs/{id}/nodes`. Both take the path and give the same result.
  - The stream matches a golden file in `tests/fixtures/scenarios/`, with IDs, timestamps, durations, revision hashes and UUIDs removed. Regenerate with `ZONIC_UPDATE_SNAPSHOTS=1`, as for the compiled-graph snapshots. This is the repository's convention rather than syrupy, which the plan names.
  - Fixtures are published under a UUID5 of their path, because catalog IDs are UUIDs and a template's ID is a slug.
  - Every catalog model is enabled and the script-node feature is on, as for F-15.
- **Runs now:**
  - `question_answering_with_tools` (one answer);
  - `test-agent-workflow` in both dialects, over two turns on one session. Its `DBQuery_1` is reported `NODE_UNREACHABLE`, never compiled and never in an event (F-07).
  - `new-workflow` is refused with `422 PLAN_INVALID` before any gateway call.
- **Coverage gate:** for every vendored workflow it computes the compiled node kinds. It fails when:
  - a workflow that runs with today's kinds has no scenario;
  - a workflow cannot be planned and has no refused scenario;
  - a kind's task (`WAITING`) is complete and the workflow still has no scenario.

  Every manifest node type appears in some fixture.
- **Waiting:** 28 of the 32 vendored workflows need a kind built in Phase 3 or 4 (decision 0005 has the table). F-08, F-10, F-17, F-18, B-12 and B-13 therefore cannot pass with real kinds yet.
  - Decision 0005 proposes moving them to P3-03, P3-04, P3-06, P3-07, P3-08 and P3-09, and amending M2. Running them now with stand-in kinds is the alternative.
  - Accepted with the M2 approval (2026-10-27): the plan and `tests/catalog.yaml` are amended and P2-09 is complete.
- **Test app:** `FakeApp.publish` counts a template wrapper's nodes from its `document` member.

### P2-08 notes

- **Production tracer** (`integrations/langfuse/tracing.py`): `GatewayTracing.create` builds `LangfuseTracer` over `GatewaySpanExporter` when `observability.enabled` and `exporters` is `["gateway"]`. Any other exporter is `RESOURCE_INVALID` at start-up: the Langfuse OTLP exporter is not built.
  - `AppDeps.from_environment` uses it, so `NoopTracer` is now only for tests and tracing off.
  - `start()` binds the exporter to the app's loop. `aclose()` flushes from a worker thread before the gateway closes.
- **Exporter** (`gateway_exporter.py`): runs on the span processor's thread and never on the loop, so runs never wait for tracing.
  - Records join a queue of `export.maxQueueSize`; when it is full, the oldest are dropped and counted (`dropped`, `zonic_trace_records_dropped`) (O-06).
  - Batches hold at most `maxObservationsPerCall` records and `maxBytesPerCall` bytes. A single record above the byte cap goes alone (O-05).
  - On `DATA_UNAVAILABLE` a batch is retried `retryAttempts` times with exponential backoff. After that it stays queued for the next export, and `/readyz` reports `checks.tracing: degraded` (L-11).
  - Any other refusal drops that batch and counts it, so a bad record cannot block the queue.
  - **Ingest bridge:** `GatewayIngest` reaches the async `GatewayPort` from the exporter thread with `run_coroutine_threadsafe` and a timeout. Called on the loop thread it raises, because it could never complete there. Async code must flush through `asyncio.to_thread`, as `GatewayTracing.flush` and `aclose` do.
  - **No `DEBUG` log line for drops:** case O-06 asks for a `DEBUG` count, but the logging policy drops records below `WARNING`. The count is the gauge instead.
- **Mapper** (`record_mapper.py`, O-01):
  - **IDs and types:** span ID → `observationId`, and trace ID → `traceId`, so a resent batch inserts nothing new (O-02). The root's parent, the propagated context, is not sent. Unknown observation types become `span`.
  - **Node columns:** the node metadata fills `nodeId`, `nodeType`, `nodeName`, `nodeKind` and `visit` and is left out of `metadata`, along with the keys every span repeats from the trace (run, workflow, revision, thread, checkpoint namespaces). The blueprint now adds `zonic_node_name` when a node's name differs from its ID.
  - **`gen_ai.*`:** these attributes fill model, usage and parameters where the Langfuse ones are missing (`genaiAttributeMap`).
  - **Cost:** sent only when the span carries `cost_details`; otherwise `sp_obs_ingest` prices the usage (O-04).
  - **Size:** an input or output above `limits.maxObservationBytes` becomes its summary, with `metadata.truncated`.
  - **Trace header:** from the root span. It carries `status` (the run status, now on the root as `zonic_status`) and `tenantId` (now in `TraceInfo.metadata`).
  - **UUID columns:** run, session and workflow are sent only when they are UUIDs (the procedure casts them) and are kept in `metadata` otherwise.
- **Re-parenting (SP-4):** the agent graph's `model` and `tools` wrapper spans were dropped by `should_export_span`, which left their model and tool calls pointing at parents that were never exported.
  - `SamplingExporter` already holds a whole trace until its root ends, so it now drops the wrappers there (`tidy`) and gives their children the wrapper's parent. Langfuse's default span filter applies instead of `keep_span`.
  - A model call now hangs under its agent node with no dangling parent, checked on a real traced run.
- **Models** (`models/observability`): `ObsTraceRecord`, `ObsObservationRecord`, `ObsScoreRecord` (published under `generated/schemas/observability/`) and `ObsIngestArgs` (the `sp_obs_ingest` call, idempotent). On the hot path the exporter builds dictionaries, and the tests validate every record against the models.
- **Flaky test seen once:** `test_Q05_150_concurrent_streaming_runs…` failed once in 11 full runs on this machine and passed in the other 10 and alone. Its assertions now report what failed, and the event-loop lag p99 is the likely one. The budget itself is P5-02's.

### P2-05 notes

- **Admission** (`run/executor/executor.py`): `RunExecutor` admits at most `runs.maxConcurrentRuns` runs per process. One more gets `429 RATE_LIMITED` with `Retry-After: 1`, and running runs are unaffected (R-04). An overloaded process gets `503 OVERLOADED` (Q-15, tested early).
  - The permit is taken before any gateway call. A start that fails gives it back, and so does an idempotent replay. A started run holds it until its task ends.
  - The caps per session, user and tenant stay in `sp_run_start`, across instances.
- **Pressure** (`runtime/pressure.py`): event-loop lag is sampled every `pressure.sampleIntervalMilliseconds`. RSS comes from `/proc/self/statm` on Linux and is 0 on Windows.
  - Above `maxEventLoopLagMilliseconds` or `maxRssMegabytes` the process is overloaded. `/readyz` then answers `200` with `status: degraded` and `checks.pressure`, since running runs continue. `HealthStatus.status` gained `degraded`.
  - Active runs are bounded by admission, not by a pressure threshold.
- **Model governors** (`engine/agents/governor.py`): one per catalog model, shared by every run. Each has an in-flight cap and request and token buckets from `quota`. A call still queued at `maxQueueWaitMilliseconds`, or one the buckets cannot serve before then, is `MODEL_BUSY`, which the LLM retry policy retries (R-02 sees 7 refusals in a burst of 8 runs and no hang).
  - Direct LLM calls take a permit in `LlmKind`, and agent loops take one in `GovernorMiddleware`. The token count is LangChain's approximation of the messages.
- **Tool permits:** `ToolPermitMiddleware` bounds each node's concurrent tool calls to `tools.maxConcurrentPerTurn` (Q-07: ten calls, four at once). The permits live in the run's `RunPermits`, never in the shared agent graph.
- **Node permits** (Q-06): the run-wide pool now has a test with four containers, each fanning out to four leaves, under `maxConcurrencyPerRun` 3. Containers (`holds_permit = False`) hold no permit, so nesting cannot deadlock.
- **Coalescing** (`run/events/bus.py`, Q-04): with `sse.coalesceMilliseconds` (16) and `coalesceBytes` (1024), the `RunStream` joins `token.delta` and `reasoning.delta` before they get a `seq`.
  - A node's first delta goes out at once. Later ones are joined until the window ends, the bytes collect, or any other event is published. Joined text always goes out before the next event, so order and gapless `seq` hold for every subscriber.
  - A window of 0 turns coalescing off.
- **Subscriber back-pressure:** each SSE response reads the stream through a queue of `stream.subscriberQueueEvents`. A slow reader falls behind the replay buffer and gets `stream.gap`; the run never waits.
  - **Defect found and fixed:** `_pump` used to `await queue.put(_DONE)` in `finally`. A pump cancelled while its queue was full (the client went away) would have hung forever. The test fails on the old code.
- **Pools** (`integrations/http/pools.py`):
  - `BedrockClients`: one boto3 `bedrock-runtime` client per region, passed to every `ChatBedrockConverse` (Q-09). botocore sets `max_pool_connections` = `models.executorThreads`, the read timeout and `standard` retries.
  - `install_blocking_executor`: makes a `ThreadPoolExecutor` of `models.executorThreads` (160) the loop's default executor at app start. LangChain's `run_in_executor(None, …)` uses it, so boto3 calls are bounded (Q-09 checks 100 calls on 8 threads).
  - `HttpPools`: one `httpx.AsyncClient` per upstream. The JWKS client now comes from it and is closed with the app; it was never closed before. The gateway keeps its own client.
- **CPU pool** (`runtime/process_pool.py`): `CpuPool` runs work on payloads of `cpu.offloadBytes` or more in a spawn-started `ProcessPoolExecutor` of `cpu.workers`, and smaller payloads inline (Q-16, with the masker). Nothing on the run path calls it yet: its first user is P2-08's exporter, which masks large payloads. The `time.sleep` half of Q-16 is the existing AST rule 13, now with its own test.
- **Metrics** (`runtime/metrics.py`, `api/internal.py`): `/metrics` on the internal app serves gauges read at scrape time:
  - `zonic_active_runs`, `zonic_streams_open`, `zonic_node_permits_in_use`;
  - `zonic_event_loop_lag_milliseconds` and its p99, `zonic_overloaded`;
  - `zonic_model_queue_depth` and `zonic_model_in_flight` by `model`.
  - "Queued runs seen" waits for the async claimer (P4-05).
- **Serving:** the Dockerfile ran `uvicorn zonic_runtime.main:app`, which does not exist, so the image could not have started.
  - It now runs `python -m zonic_runtime.server`: one process and one event loop (uvloop off Windows) serving the public app on `ZONIC_PORT` (8000) and the internal app on `api.internalPort` (9000). Both share one `AppDeps`.
  - Only the public server takes signals; its exit stops the internal one. Its lifespan calls `AppDeps.start()` (pressure monitor, blocking executor) and `aclose()`.
  - `ZONIC_HOST` and `ZONIC_PORT` are new `EnvironmentModel` fields.
- **Not yet:** `Q-01` to `Q-09` are also P5-02's (k6 on the target task), so the catalog check counts them as due only then. Budgets such as the p99 lag of 50 ms are not asserted here; Q-05 asserts only that the process is not overloaded.

### P2-04 notes

- **`TurnSaver`** (`engine/persistence/turn_saver.py`) is an `InMemorySaver` with one buffer per run.
  - `attach(run, bundle?)` before the run and `detach(run)` after its save. Detaching exports the latest `state.checkpointsKeptPerNamespace` (2) checkpoints per namespace, with their pending writes and blobs, and frees the thread.
  - A write to a thread that is not attached is `CHECKPOINT_THREAD_UNKNOWN`. The conformance suite runs with `strict=False` and passes every base capability (P-07).
  - Serialization is `JsonPlusSerializer` without pickle fallback, or `EncryptedSerializer` with the `CHECKPOINT_AES_KEY` hex key when `state.encryption.enabled`.
  - Under `durability="exit"` there is one `aput` per run whatever the node count (P-06).
- **`CheckpointBundle`** (`codec.py`): format `langgraph-jsonplus-zstd-v1` and `langgraphVersion` are recorded, and each payload is zstd-compressed and base64-encoded (P-09). P-08 resumes a detached bundle, taken through JSON, in a fresh saver and graph. The `runtime_checkpoint` row itself (`sp_interrupt_create`) is P4-02's.
- **`TurnStateStore`** (`run/state/turn_store.py`) replaces P2-03's minimal store.
  - **Start, existing session:** one `sequential` batch of `v_session_state`, `v_message` (newest `history.maxMessages`) and `sp_run_start`.
  - **Start, new session:** `sp_run_start` alone.
  - **End:** one `sp_run_finish`. It carries the turn's messages, the answer message (added unless a chat message of the turn already says it), the declared variables with `expectedVersion`, the timeline and `lastEventSeq`.
  - **Failures at the end:** the save is retried on `DATA_UNAVAILABLE` until `state.saveDeadlineMilliseconds`, then `STATE_UNAVAILABLE` (P-11). A version conflict is retried once over the latest variables, then saved without them, with `STATE_CONFLICT`.
- **Warm turns** (P-05): exactly the start batch and `sp_run_finish`.
  - `WorkflowLoader.load_cached` finds the workflow in the cached catalog (`catalog.cacheSeconds`) and falls back to the filtered read for one it does not list.
  - The run service caches each session's workflow by owner; `sp_run_start` still checks the owner.
- **Session variables** (P-13, E-19): declared variables persist unless `persist: false` (new `VariableDefinition.persist`), plus `persistence.flow` keys. A turn's `flow` is the declared defaults overlaid with the saved values. History is grouped by thread; the chat thread keeps only chat-visible messages. Node records and visit counts start empty each turn.
- **Template scope:** every message in `{{messages}}` and `{{thread.<name>}}` now reads as `{role, content, thread}`, both history and this turn's, where before this turn's were raw `ThreadMessage` records. Two P1-08 tests were updated to read `content`.
- **Persistence for now:** a `waiting_input` or `drained` outcome is not saved yet (P4-02, P4-05).


## Phase 3

Started 2026-10-27 after the M2 approval (decision 0005 accepted with it). Wave order: 10 (P3-03, P3-07, P3-08), 11 (P3-01, P3-09), 12 (P3-02, P3-10, P3-12), 13 (P3-04, P3-06, P3-11), 14 (P3-05); stop at M3.

| Task | Status | Sanity check |
|---|---|---|
| P3-03 Egress guard and HTTP node | Done | N-14, X-02, X-03: 29 tests with a fixed resolver and `FakeUpstream`; 2 live tests over real DNS and TLS |
| P3-07 Script node | Done | N-17: 10 tests; the wrapper runs for real under local `node` and `python`, the Piston API is a fake (no sandbox service is configured) |
| P3-08 View node and content parts | Done | N-18, B-11, M-06: 25 tests; fsh-facility-agent-v2 scenarios (both dialects, two branches) join the golden streams (F-08 part); live suite 32 passed |
| P3-01 MCP integration | Done | T-01, T-02, T-06, T-08, G-08: 15 tests over the real `McpPool` and `MCPAdapter` with an in-process FastMCP server; 2 live tests against the real Zonic MCP server run locally |
| P3-09 Sub-flows | Done | N-11, G-05, S-04 and L-06 (sub-flow parts), F-10: 13 tests through the app; the vendored `all-agents` supervisor runs into its `fsh-facility-agent-v2` child in both dialects |
| P3-02 Tool factory | Done | N-05 (tools part), T-03 to T-05, T-07, T-09: 21 tests with the real `McpPool` over the in-process FastMCP server; 7 new builder scenarios (fsh-energy-agent in three copies, test-relationship in two, the calculator turn) |
| P3-10 Remaining hook types | Done | K-08, K-14: 25 tests (webhook over the real `EgressGuard` with `FakeUpstream`, a masked agent traced through `LangfuseTracer` to a fake ingest); offline 1,391 passed; live suite 32 passed, 2 skipped |
| P3-12 Recorded responses | Done; decision 0006 (recordings vendored from the Node.js runtime) | F-09: 5 tests; the 25 Node.js recordings found by this runtime's keys; fsh-energy-agent (recorded open-data copy, and the Zonic copy through template aliases) runs on the recordings with no unrecorded call; offline 1,395 passed; live 32 passed, 2 skipped |
| P3-04 Database node | Done, with P3-05 (wave 14) brought forward | N-13, X-01 (datasets part), F-08: 7 N-13 and X-01 tests; 11 new builder scenarios (fsh-facility-agent and fsh-risk-agent in three copies, all-agents in two, complex-agent) with goldens; the open-data SQL turns answered by the recordings |
| P3-05 Datasets | Done | P-12, B-11: 50,000 rows and a 5 MB body kept in full in both stores and paged; the grid names its dataset; live: an NYC Open Data response kept as a dataset, saved by the transactional turn-end batch on the deployed gateway and paged back for its owner only; offline 1,421 passed; live 33 passed, 2 skipped |
| P3-06 Tool, external agent and guardrail kinds | Done | N-10, N-15, N-16, B-05, F-17, F-18, B-12, B-13: 30 tests; the 9 platform workflows join the builder scenarios with goldens (the orchestrator over two turns into its quick-pay child; seven sub-agents through their loops twice; the catalogue's script nodes run under local `node`); offline 1,459 passed; live 33 passed, 2 skipped |
| P3-11 Trace and usage APIs, feedback, maintenance | Done | L-14, L-15, O-07, O-08: 9 tests; live: the trace tree, feedback scored on the trace and model usage on the deployed gateway, and a maintenance pass (partitions, rollups) whose rollups match the raw rows; offline 1,480 passed; live 35 passed, 2 skipped |

### P3-03 notes

- **Guard** (`integrations/egress/guard.py`, port `ports/egress.py`): checks every request and every followed redirect.
  - **Allow list:** the scheme, the host (exact, or `*.suffix`, which does not match the bare domain) and the port.
  - **Resolved addresses:** with `blockPrivateRanges`, every resolved address must be public. Private, loopback, link-local (metadata), multicast, reserved, unspecified and IPv4-mapped private addresses are `EGRESS_DENIED`, as is a host that does not resolve.
  - **Pinning:** the connection goes to the checked address. The URL carries the IP, while `Host` and the TLS server name (`sni_hostname`) carry the host, so there is no second DNS lookup to rebind and the certificate is still verified against the host. The live test proves this on `httpbin.org`.
  - **Redirects:** followed only with `followRedirects` (at most 5), each checked again; otherwise a 3xx is returned unfollowed.
  - **Limits:** `maxResponseBytes` is read as a stream and stops with `EGRESS_DENIED` (reason `maxResponseBytes`; the catalog has no dedicated code). The timeout is the request's, capped by `timeoutSeconds`. Environment proxies are ignored (`trust_env=False`).
  - **Failures:** a 5xx, or a failed connection (reason `connection`), is `UPSTREAM_5XX` with the method; a slow call is `TIMEOUT`.
- **Kind `http`** (`engine/kinds/io_kinds.py`):
  - **Request:** method, list headers (`key`/`value`) and body (text as is, other values as JSON).
  - **Output:** `status`, `headers`, `body` text and `json` when the body is JSON. A 4xx is an output, not an error.
  - **Timeout:** the request gets 90% of the node timeout, so a slow answer is the retryable `TIMEOUT` and not the outer `NODE_TIMEOUT`. Before this they raced.
  - **Retries:** `retry_policy` now honors the manifest's `methods`, so `TIMEOUT` and `UPSTREAM_5XX` retry for GET and HEAD only and a POST is never repeated (N-14).
- **URL encoding** (X-03): `TemplateResolver.resolve` takes an `encode` callback for values embedded in a longer string. The HTTP kind percent-encodes every value placed after the URL's authority, so `a/../admin?x=1` stays one path segment. A template that is the whole base URL is used as it is. If a `beforeNode` hook changed the URL input, the hook's value is used.
- **Wiring:**
  - **`ServicePorts`:** a new group on `NodeServices` holds process-wide adapters (egress now; sandbox, MCP and cache later). `RunContext.ports` stays for owner-bound ports. This deviates from design 7.1's list, which put egress in `RunPorts`; egress holds nothing owner-bound.
  - **Defaults:** `AppDeps.build` defaults egress to the guard over `egress.json` and closes it with the app. Tests pass fakes through `Harness(ports=...)` or `AppDeps.build(ports=...)`.

### P3-07 notes

- **Port and adapter:** `ports/sandbox.py` and `integrations/sandbox/piston.py` (`POST {ZONIC_SANDBOX_URL}/api/v2/execute`). Built to match the Node.js runtime so both run a script the same way:
  - the same wrapper (`input` holds the inputs, passed as one JSON string literal, never as code), the same `__ZONIC_RESULT__` marker and the same failure reasons;
  - the same `SANDBOX_ERROR` code (502, not retryable), added to `error-catalog.json`;
  - the same `sandbox` settings block (`runTimeoutMilliseconds` 3000, `maxOutputBytes`, `languages` javascript and python).
- **Behaviour:**
  - The run timeout sent is the smaller of the node's and the setting.
  - A script Piston kills (`signal`) is `SANDBOX_ERROR` with `timeout_ms`, and so are a non-zero exit, a missing or non-JSON result, an unknown language, and an unreachable or failing service.
- **Kind `sandbox`** (`engine/kinds/io_kinds.py`):
  - The `inputs` mapping list becomes a record, each value converted to its `type`.
  - A returned object is the output, and any other value becomes `{value}`.
  - With `features.scriptNode` off, planning refuses the node with `FEATURE_DISABLED`. With the flag on but no `ZONIC_SANDBOX_URL`, the node fails with `FEATURE_DISABLED` (N-17).
- **Plan Q5** (Piston for production) stays as recommended: the adapter ships, and the node stays off by feature flag.
- **Not verified live:** there is no Piston service in any environment yet. A live test needs `ZONIC_SANDBOX_URL`.

### P3-08 notes

- **Content parts** (`models/content`): `markdown`, `table`, `grid`, `chart`, `card`, `json`, `file`, `html`, `citation`, `error` and `form`, discriminated by `type` and published under `generated/schemas/content/`. The shapes are the Node.js runtime's, so one client reads both runtimes. Every part round trips as its own class with identical JSON (M-06).
- **Contract changes for the UI owner's review** (the `generated/` snapshot changed):
  - `content.part` events now carry a typed `part`.
  - `DatasetRef` takes the Node.js wire shape (`store`, `key`, optional `datasetId`, `kind`, `rowCount`, `columns`, `sizeBytes`, `sha256`, `expiresAt`); `nodeId`, `visit` and `preview` remain as optional extras. Nothing produced dataset refs before this.
  - Message text parts are `markdown`, the design's type, instead of `text`. Readers still accept `text`, so saved rows keep working (the live suite reads them).
- **Kind `view`** (`engine/kinds/io_kinds.py`, helpers in `io/view_parts.py`), after the Node.js runtime's:
  - **`html_template`:** the template is sanitized into an `html` part. Template values are HTML-escaped by the port binding.
  - **`template_config` `grid`:** a `grid` part over the dataset `cacheDataKey` names, or a `table` of inline rows capped at `content.inlineRowLimit`.
  - **`chart`:** a `chart` part whose option is checked (top-level keys on `content.chartOptionKeys`; no functions or function text such as `function (p) {…}` or `=>`), plus an `html` part from `html_data`.
  - **Failures:** another view type, or a bad option, is `CONTENT_INVALID` (N-18). Missing data warns `view.data-missing`.
  - **Delivery:** each part is emitted as `content.part`, kept in the node record (`NodeRecord.parts`) and added after the text in the turn's answer message, in completion order, so a reopened session shows it.
- **Sanitizer** (`nh3` 0.3.7, a new dependency): only `content.htmlAllowedTags` and `htmlAllowedAttributes` stay.
  - Removed with their content: `script`, `style`, `iframe`, `object`, `embed`, `noscript`, `template`.
  - Also removed: event handlers, `javascript:` and every non-`https`/`mailto` link, images other than inline `data:image`, CSS that loads or runs something, and comments (B-11).
- **Port encoding:** `PortBinding.encode` (`uriComponent`, `html`; encoders in `io/encoders.py`), as the Node.js manifest has it.
  - The HTTP `url` port and the view `html` port declare it.
  - The URL encoding of P3-03 moved there from the HTTP kind, which now reads its `url` input as the binder resolved it.
- **Scenario gate:** configured agent and LLM tools count as waiting for the tool factory (P3-02), and a scenario may name what it knowingly runs without (`waits`). fsh-energy-agent therefore waits for P3-02 (its LLM nodes call MCP tools), while fsh-facility-agent-v2 runs now.
- **Not yet:** a grid's `datasetRef.datasetId` is filled once P3-05 registers datasets by cache key.

### P3-01 notes

- **Pool** (`integrations/mcp/pool.py`), following SP-7:
  - **Held clients:** one `fastmcp` client per server of `mcp-servers.json`, created and entered on first use, then held for the process lifetime inside `langchain.mcp.MCPAdapter`. That adapter is beta; its warning is silenced at import, and it stays behind the pool.
  - **Concurrency:** at most `maxInFlight` calls per server. 25 concurrent calls from 25 runs share 1 connection (T-06).
  - **Endpoints:** from the catalog and the environment (`env:mcp_base_url`), never from a document. `authSecretRef: env:<field>` is sent as a bearer token.
  - **Tool catalog version:** a `tools/list_changed` notification marks the list stale, and a list that differs on the next `tools()` bumps `version`.
    - The version is part of the compiled-graph cache key (`WorkflowRuntime.prepare`, G-08).
    - The in-process FastMCP does not send that notification when a tool is added, so the test delivers it through the client's own handler. The handler survives the adapter's clone of the client.
  - **Failures:** an unreachable server, a missing URL or a closed pool is `TOOL_UNAVAILABLE` (T-01: connection refused fails at once, as `RuntimeError` from `fastmcp`).
- **`execute`** (`integrations/mcp/execute.py`, models `models/mcp`):
  - **Contract:** the server's camelCase shape (`ZonicMCPServer_FastAPI app/models/execution.py`) and the Node.js runtime's.
  - **Parsing:** the reply is read from the result's structured content or its text JSON.
  - **Flow updates:** the server's `meta.flowUpdates`, then each successful tool's `output.data.variable` bound to its value (T-02).
  - **Partial results:** per-tool errors come back in the results (`partial`).
  - **Failures:** a protocol error or a reply that is not the contract is `TOOL_FAILED`, added to the catalog (502, not retryable) for parity with the Node.js runtime.
- **Owner-bound** (T-08): `RunMcp` in `RunContext.ports.mcp`, made per run by the run service, stamps the run's identity (`runId`, `ownerSub`, `sessionId`, `tenantId`) into `context.system` and makes the request ID `runId:<key>`, so a retried call is the same request. The pool holds nothing of a run.
- **App:** `AppDeps.from_environment` builds the pool, the lifespan's `warm()` lists the tools at start-up (a server that is down is retried by the first call), and `aclose` closes it.
- **Real server:** the live tests ran against `ZonicMCPServer_FastAPI` started locally (port 8301, `FUNCTION_ALLOWLIST=statistics`, no database).
  - They showed that every tool type, functions included, answers in the `{is_cached, data: {variable, value}}` envelope. The fake now does the same.
  - The database and API tools wait for D-7 and P3-12's recordings.
- **Flaky test seen again:** one full run of five had one failure that did not recur in three reruns, most likely `test_Q05_150_concurrent…` as before (P2-05 notes).

### P3-09 notes

- **Kind `subflow`** (`engine/kinds/subflow_kind.py`, after the Node.js runtime's): a container that holds no node permit.
  - **Running the child:** it opens the child through `ServicePorts.subflows` and runs the child graph through `invoke_child`, so callbacks, the checkpoint namespace, `RunContext`, the deadline and the permits reach it.
  - **What the child starts from:** the mapped `inputs` (read by its Start node as interface inputs), the parent's `system` and history, and `flow` per `subflow.inheritFlow` (`none`, the child's declared variables, or `all`).
  - **Output:** the child's result (its Output node, else the fallback chain) is the node's output.
  - **Joining the parent's turn:** the child's chat messages and visits join it (`TimelineEntry.path`, node IDs as on events), and so does its usage.
  - **Limits** (N-11): the call chain travels in the child's `configurable` (`zonic_lineage`). A workflow already on it is `SUBFLOW_CYCLE`; a chain deeper than `subflow.maxDepth` is `SUBFLOW_DEPTH_EXCEEDED`; a child that does not exist is `WORKFLOW_NOT_FOUND`. With decision 0004 every stored workflow is visible to a caller with a valid token.
- **Opener** (`application/subflows.py`): bound to `WorkflowRuntime`, which owns the loader and the blueprint and graph caches.
  - **Loading:** the first child a parent revision opens loads all of its static children with one `in` read (`v_agent_summary`, then `v_agent_export` on a cache miss), kept `catalog.cacheSeconds` (F-10: one summary read for the supervisor's three children). A templated or missing child is read alone.
  - **Graphs:** child graphs come from the graph cache in the `child` role, compiled without a checkpointer, with the current tool catalog version (G-05).
- **Nesting on events, timeline and trace** (S-04, L-06):
  - **Registry:** `RunContext.children` (`ChildBlueprints`, per run) records each child blueprint the sub-flow kind opens, by (parent workflow, node).
  - **Events:** `NamespaceMap` follows it, so a grandchild's tokens are `nodeId: llm`, `path: child/grand`. A sub-flow whose child is not open yet is not cached as unknown.
  - **Trace:** the Langfuse handler now treats a child graph run inside a sub-flow node visit as a graph run, so the child's node visits are marked, numbered and typed like the root's, nested under the sub-flow node's observation.
- **Result fallback:** a sub-flow node's text result now counts as an answer, and a child's own timeline entries (those with a `path`) are skipped. `all-agents` has an Output without a mapping and a supervisor that only hands off, so before this it ended `NO_OUTPUT`.
- **Scenario gate:**
  - **Children:** it now follows sub-flow children transitively (same dialect folder first). `all-agents`, `test-relationship` and `django/complex-agent` therefore wait for their children's tools, HTTP and database nodes, and a child no fixture holds is `missing-child`.
  - **Fixture gap:** `platform/intelligent-buying-orchestrator` has one (`exec_category_agnostic`, `025421e9-…`, never vendored), recorded in `FIXTURE_GAPS`. Its F-17 scenario has to avoid that branch, or the child has to be vendored.
- **Code fix found while testing:** `ctx.config_values()` has snake_case field names; the kind first read `agenticWorkflowId` and found nothing.

### Q-05 flake fixed (2026-10-27)

- **Symptom:** `test_Q05_150_concurrent…` failed in 6 of 8 full-suite runs with an event-loop lag "p99" of 200 to 208 ms against the 200 ms limit.
- **Cause:** under 150 concurrent runs the 20 ms sampler got only about 19 samples, so the p99 was just the single largest one: one pause (garbage collection or the burst of 150 starts) of 70 to 170 ms alone, a little more under full-suite heap pressure. It was not the graph compile, which takes 2 ms.
- **The same weakness was in production:** `PressureMonitor.overloaded()` compared only the latest sample, so one GC pause would have shed new runs with `503 OVERLOADED`.
- **Fix:**
  - `overloaded()` now compares the 90th percentile of the last 10 samples (`recent_lag_milliseconds`). One slow sample does not trip it; sustained lag does within a few samples, as a new Q-15 test checks.
  - Q-05 asserts the same p90 and reports p99 in its message. The p99 budget of 50 ms stays P5-02's (k6 on the target task).
  - Six full runs after the fix all passed (1,340).

### Checkpoint M3 (2026-10-27)

Phase 3 is built: P3-01 to P3-12, 43 tasks complete in all. P3-05 was brought forward with P3-04; decision 0006 vendors the recordings.

| M3 asks | Status |
|---|---|
| All builder fixtures, with tools and sub-flows, run against the fakes with recorded responses (F-08, F-09, F-10) | 29 builder scenarios over 33 vendored documents, stream and sync, with golden streams. The open-data SQL turns and fsh-energy-agent run on the recordings. `exec_category_agnostic` (the orchestrator's child) is still not vendored (`FIXTURE_GAPS`) |
| The platform workflows run their multi-turn scripts (F-17, F-18) | The orchestrator over two turns into its quick-pay child; seven sub-agents through their loops twice; the catalogue assistant |
| Database and HTTP results kept in full in both stores (P-12) | 50,000 rows and a 5 MB body in the `database` and `cache` stores, paged through `/v1/datasets`. Live: an NYC Open Data response kept and paged back on the deployed gateway |
| One open-data fixture against the real MCP server locally once D-7 is settled | Not yet. D-7 is open: the MCP server needs the `sample_open_data` connection. The MCP live tests skip without `ZONIC_MCP_BASE_URL` |

**Gates:** ruff, ruff format, mypy (220 files), import-linter (4 contracts), `gen --check`, `sync_builder --check`, `sync_recordings --check` and `catalog_check` (241 cases with tests, 223 due) all pass. `sync_sql --check` still fails until the Node.js repository commits its `db/observability/005_procedures.sql` change. 1,480 offline tests pass; the live suite passes 35 and skips 2.

**For review:**
- decision 0006;
- the order change (P3-05 with P3-04);
- the parity gaps closed in P3-06 (`requestInputs`, `mcp-resource`, the script output hint, structured output errors);
- the v3 streaming defect fixed in P3-10;
- the live checks that wrote to the product database: rows of owner `live-langgraph-verify` and tenant `live-test`, one feedback score, partitions ahead and refreshed rollups.

### P3-11 notes

- **`TraceService`** (`application/observability.py`, after the Node.js `trace-service.ts`):
  - `GET /v1/runs/{id}/trace` reads the trace, its observations (nested by parent, each level in start order) and its scores in one gateway batch, for the owner only;
  - `GET /v1/observability/usage` returns the daily node rollups, or with `groupBy=model` the caller's tenant's model usage (`from` and `to`, else `observability.usageDefaultDays`);
  - `GET /v1/runs/{id}/trace-link` returns the trace's URL on the Langfuse server, `NOT_FOUND` when none is configured.

  All three need a role in `observability.traceRoles`; otherwise `FORBIDDEN` (L-15). Another user's run is the same 404 as a missing one.
- **`FeedbackService`:** `POST /v1/messages/{id}/feedback` stores the rating through `sp_feedback_save`, which checks ownership (a foreign message is 404). It then upserts a `NUMERIC` trace score named `observability.feedbackScoreName`, with a score ID derived from the message and the user, so a new rating replaces the old one. The score is best effort: a run without a trace keeps its rating (L-14).
- **`MaintenanceService`:** every `maintenance.intervalMinutes`, on every instance, it calls `sp_obs_partition_ensure`, `sp_obs_retention_purge`, `sp_obs_refresh_rollups` and `sp_retention_purge`. A failing step does not stop the others, and the last report is kept (O-07). Only the server's deps start it (`AppDeps.maintain`), never a test app's. The vendored `sp_retention_purge` removes expired checkpoints and datasets and archives idle sessions; it does not expire interrupts, which the plan row mentions.
- **O-08:** `FanOutExporter` sends the same spans to the gateway exporter and to an OTLP exporter on the Langfuse server (`observability.exporters` with `langfuse`, plus `ZONIC_LANGFUSE_*`). The gateway's result is the result; the Langfuse side's failures are counted and never raised. `langfuse` without `gateway`, or without its keys, stops start-up (`RESOURCE_INVALID`).
- **Test fakes:** `ObsStore` gained the trace, observation and score views (with the trace's user and tenant), the daily rollups, `sp_obs_score_upsert` and the maintenance procedures. `RuntimeSchema` gained `sp_retention_purge`.
- **Live:** the retention purges are not run against the deployed gateway, because they delete product data; they are tested offline. The live pass runs the additive steps and checks the model rollup of tenant `live-test` against its raw observations.

### P3-06 notes

- **Kinds** (`engine/kinds/integration_kinds.py`, after the Node.js `tool-node.ts`, `external-agent-node.ts` and `guardrail-node.ts`):
  - **`tool`:** the tool comes from the agents' `ToolFactory` (`ToolProvider.create`) and is called directly, with a `ToolRuntime` over the run, and no model call. A server tool's text answer is parsed as JSON. With `pass` and `fail` edges, success activates `pass` (plus the default edge when connected) and a tool error activates `fail`; without a `fail` edge a tool error fails the node with `TOOL_FAILED`.
  - **`external_agent`:** A2A `message/send` over JSON-RPC by default, or `protocol: http`, through the egress guard. The API key is a secret reference. `UPSTREAM_5XX` is retried `retries` times (`externalAgents.retryBackoffMilliseconds`). Failures are `EXTERNAL_AGENT_ERROR`, new in the catalog.
  - **`guardrail`:** the policy model (`guardrails.model`, else the catalog default) asked for a structured `GuardrailDecision`, or a Bedrock guardrail when the node names `guardrailId`. `pass`, `fail`, or `warn` (passes with `guardrail.flagged`). Blocked content without a `fail` edge is `GUARDRAIL_BLOCKED`.
- **Policy model and Bedrock:** `engine/agents/policy_guard.py` (`PolicyGuard`) also backs the `guardrail` hook's `policy_model` provider. That provider failed at load until now; it now works whenever agent services exist. `integrations/guardrails/bedrock.py` (`ApplyGuardrail` on the `bedrock-runtime` client) is wired when `guardrails.bedrockRegion` is set. Its live call is untested, since no guardrail is provisioned.
- **Parity gaps found and closed for the platform workflows:**
  - **`requestInputs`** (`dialects.json`): a root run's interface inputs come from the request (`message` and `query` from `text`), as in Node.js. Platform start nodes read `interface.inputs.message`, and without it the agents' prompts were missing.
  - **`mcp-resource` tools** (a reference file of an MCP server): `McpPort.resource` and `McpPool.resource` (`read_resource`) back a tool with no arguments.
  - **`platform.script-output-hint`** (`setValue`, a new dialect operation) and a per-node `outputValidation`: the platform's script output schema is a hint, so mismatches are warnings.
  - **`StructuredOutputValidationError`** (native structured output that is not JSON) is now `STRUCTURED_OUTPUT_INVALID`. It was `INTERNAL`.
- **Platform scenarios** (`tests/scenarios/builder/platform.py`, after the Node.js `platform-loops.ts`):
  - **Responders:** a scenario may carry a `responder` factory, a fresh stateful model per run that answers by the tools each call offers. Its structured answers fill required fields from the offered schema (the structured tool, or `response_format`).
  - **Fakes:** `ServerToolsMcp` answers servers' own tools by MCP name, and script nodes run on `FakePiston`.
  - **The orchestrator:** turn 1 routes on `flow.current_state` from `get_next_questions` into the quick-pay sub-flow; turn 2 goes straight there from the saved state.
- **Not yet:** `FIXTURE_GAPS` still lists the orchestrator's `exec_category_agnostic` child, which was never vendored (H-9 in the Node.js project); the scenario's path avoids it.

### P3-04 and P3-05 notes

- **Order:** P3-05 (wave 14) was built with P3-04. The database node's dataset reference and `/v1/datasets` need the store, and P3-05 depends on nothing else. Wave 13's P3-06 and P3-11 follow.
- **`mcp_execute` kind** (`engine/kinds/database_kind.py`, after the Node.js `mcp-execute-node.ts`):
  - the query runs as one database tool of MCP `execute` (`db_query_tool`): the script, `connection` (a registered name, never a connection string, D-7), `databaseType`, `maxRows`, `queryTimeout`, and only the parameters the query references;
  - **in the `cache` store** the server caches the rows and returns a summary. A result above `io.maxResolvedBytes` is described from that summary and never read; a smaller one is read back from the cache.
  - **in the `database` store** the rows come inline.
  - **the output** is a preview (`io.inlineRows`, `io.inlineBytes`), the count, the columns and the `datasetRef`. Later templates read every row while the run holds them (`PendingDatasets.held`, overlaid on the node's output in the template scope).
  - **failures:** an unreachable server is `TOOL_UNAVAILABLE`, a failed query `TOOL_FAILED`, a vanished cache entry `DATA_UNAVAILABLE`. Never empty rows.
- **Datasets** (`ports/datasets.py`, `run/state/dataset_store.py`, after the Node.js `dataset-store.ts`):
  - `keep`, `register`, `describe`, `adopt`; chunking by `datasets.chunkBytes`. The `cache` store falls back to `database` without a cache service.
  - Each run gets a `RunDatasets` (`RunPorts.datasets`) with its pending metadata and chunks.
  - The HTTP kind keeps the whole response as an `http_response` dataset, with a body preview.
  - An agent tool's cached result is registered as a dataset.
  - A grid over a key this turn registered gets its `datasetId` (B-11).
- **Cache service:** `integrations/cache/http_cache_store.py` speaks the MCP server's `keys/<key>` protocol (`ZONIC_CACHE_API_URL`, `datasets.cacheTimeoutMilliseconds`). The `cache` and `rateLimit` hooks now share the port.
- **Turn end:** the metadata goes in `sp_run_finish` (chunk 0). When there are `database`-store chunks, `sp_dataset_save` and `sp_run_finish` go in one transactional batch, so both commit or neither does.
- **`GET /v1/datasets/{datasetId}`** (`application/datasets.py`, after the Node.js `dataset-service.ts`):
  - the metadata is read with the owner filter, so another user's dataset, a missing one and a malformed ID are the same 404 `DATASET_NOT_FOUND` (X-01, new in the catalog);
  - then the cache or the joined chunks are read;
  - paging uses `api.defaultPageSize` and `maxPageSize`, `sort` is `field[:desc]` in natural order, and `filter` is a JSON object of contains-terms.
- **Test fakes:** `RuntimeSchema` gained `v_dataset`, `sp_dataset_save` and the chunk-0 insert of `sp_run_finish`, after the vendored SQL. `FakeCacheStore` is the cache service in memory. The test app's dataset store uses the fake clock, so expiry times repeat.
- **Scenarios:** the facility and risk agents (data analysis in every copy; the open-data SQL turns against the recordings, F-09), `all-agents` and `complex-agent` (the supervisor answers, then hands off into the fsh-risk-agent sub-flow). The F-07 comparison of stream and sync results now ignores generated IDs (a dataset reference in an answer).
- **Settings:** `io.columnSampleRows`, `datasets.cacheTimeoutMilliseconds`.

### P3-12 notes

- **Decision 0006:** the recordings are the Node.js runtime's (`npm run record:fixtures`), vendored byte for byte by `scripts/sync_recordings.py`, with a commit and SHA-256 per file in `SOURCE.json`. `--check` is in CI. This project has no recorder of its own. One would need a PostgreSQL driver (rule 5), the DB Query kind (P3-04) and new live access to `sample_open_data`, and two recorders would give the runtimes different data.
- **Recorded documents:** a recording's key includes the hash of the tool's request template, so the documents the recordings were made against are vendored too (`tests/fixtures/recordings/documents/`). This project's own `open-data/fsh-energy-agent.json`, exported from agent-build-langgraph, is an older copy with 4 of the 5 recorded templates.
- **Ported exactly:**
  - `tests/fakes/recordings.py`: canonical JSON with JavaScript number text, 16-character hashes, file names, `Recordings`, `template_aliases`;
  - `integrations/mcp/tool_parameters.py`: `resolve_tool_parameters`, `resolve_server_tokens`.

  A test checks that every vendored recording is found by the key this runtime computes.
- **Replay:**
  - `FakeMcpServer` answers each tool from `answers`, else from the recordings, with the Node.js fake's envelopes. A `saveToCache` tool's rows go into `cache` and the flow gets the key.
  - It logs every call with its resolved parameters (`tool_calls`).
  - `FakeEgress` (an `EgressPort` over the allow list) replays the HTTP recordings.
  - An unrecorded call fails the test in both fakes.
- **F-09:** `fsh-energy-agent` runs with the Node.js scripted model over the red hook context and takes the recorded path. The database tools take `userID` 1809 from the workflow's variable and the period and facility from the keywords, and the bills kept for the flow are 1,787 rows. The Zonic copy's T-SQL tools borrow the PostgreSQL recordings through template aliases.
- **Waits for P3-04:** the `fsh-facility-agent` and `fsh-risk-agent` HTTP scenarios (their DB Query node needs the kind to plan). Their three HTTP recordings are replayed by a `FakeEgress` test now.
- **Scenario goldens (F-08)** keep their scripted MCP answers; F-09 is the recordings' test.

### P3-10 notes

- **Hook types** (`hooks/integration.py`, ported from the Node.js runtime's `hooks/types/integration.ts`; registered with the core types by `WorkflowRuntime` through `HookPorts`):
  - **`mask`:** keys (any depth) and patterns (`settings.hooks.patterns` names or `/source/flags`) in the input, the output and its content parts, or the result.
  - **`guardrail`:** `patterns` works now. `policy_model` and `bedrock` need the guardrail ports of P3-06; until then such a binding fails at load ("not configured"). `block` fails with `GUARDRAIL_BLOCKED`, `warn` warns `guardrail.flagged`, `redact` replaces the flagged fields. At `beforeRun` it checks the user's message.
  - **`cache`:** keys per scope (`session`, `user`, `tenant`), the resolved `key` or the bound input; a hit skips the node with the cached output and handles. The cache service arrives with P3-05; until then entries live in this process (an LRU of `cacheMaxEntries`).
  - **`rateLimit`:** a sliding window per scope in this process; a fixed window shared through the cache service once it exists.
  - **`webhook`:** asynchronous by default (the type decides its mode: `HookRunner._mode`). The payload is masked with `webhookMaskPatterns` and `maskKeys`, signed `x-zonic-signature: sha256=<HMAC>` when `secretRef` names a secret, and sent through the egress guard. An endpoint not in `settings.hooks.endpoints` or not on the allow list stops start-up (K-08). Blocking, `{"action": "fail"}` vetoes.
  - **`mcpTool`:** calls a server's tool with the resolved `inputMapping` and sets the value (`mcp_content`, the Node.js `mcpContent`) at the `outputMapping` targets.
  - **`annotate`:** tags and metadata (`zonic_annotation`) on the node's observation, or the run's at run-level points. A score becomes an `ObsScoreRecord` that the gateway exporter sends with its next batch.
- **Hook observations:** a `span` per blocking execution and an `event` per asynchronous dispatch, under the node's observation (`hook:<bindingId>`).
- **Run-level hooks** now get the run's tracer, so `annotate` works at `afterRun`, and warnings that hooks raise there reach the outcome.
- **Stream masking:** the event mapper masks the token and reasoning deltas of nodes with blocking `afterNode` `mask` bindings (`HookRunner.stream_mask`, as in Node.js). It holds back `streamMaskHoldbackChars` and flushes before `node.finished`. A match longer than the holdback can stream in part, as in Node.js.
- **`EgressPort.allows(url)`:** checks scheme, host and port without DNS, for the load-time check. `EgressGuard.check` uses the same static checks.
- **Defect found and fixed (v3 streaming):** LangGraph builds a `RunEventTransformer` for every subgraph mux (`factory(scope)`), and they all shared the run's mapper. Each event nested under a subgraph was therefore mapped once per enclosing mux. Only the root projection is drained, so the extra drafts were discarded, but the mapper's state was hit more than once. The stream masker exposed it (text doubled). Child-scope transformers now do nothing. The other state this affected was tool start times (a second pop, so no duration).
- **Parity:** `afterRun` allows only `continue` in both runtimes, so a `guardrail` `block` there is `HOOK_RESULT_INVALID`, not a block.
- **Not yet:** masking of content parts is implemented but has no test of its own. Stream masking looks up bindings on the root blueprint only, as Node.js does, so a sub-flow child's own `mask` bindings mask its state but not its stream.

### P3-02 notes

- **`ToolFactory`** (`engine/agents/tool_factory.py`, after the Node.js runtime's) is now the default `ToolProvider` (`NoTools` remains for tests that want none). From the canonical `ToolRef`s the normalizer already makes, it builds:
  - **`database`, `api`, `function`** (the builder's MCP configured tools): tools that call MCP `execute` with the configured tool, the model's input, and the visit's `flow` and `system` as context.
    - Secrets are resolved at call time.
    - The model's input schema is the parameters no runtime source fills; `payloadType: from_llm` takes any object.
    - A cached result goes back to the model as `{saved, variable, key}`. The flow updates go back to the run.
    - A per-tool error is an error result the model sees, not a failed node.
  - **`mcp`, `agentcore_gateway`:** a server's own tool, reached only when called, so a server that is down or unknown is a tool error.
  - **`strands_builtin`:** `calculator` (no `eval`) and `current_time`, when on `tools.builtinAllowList`; any other name is `TOOL_NOT_ALLOWED` (T-05).
- **Tool rules:**
  - Every tool validates the model's input against its schema first. An invalid input is an error result the model can correct, and it never reaches the server (T-03).
  - Errors use `handle_tool_error`.
- **Run data only through `ToolRuntime`:**
  - **Agent state:** the kind seeds `zonic_flow`, `zonic_system` and `zonic_visit`. Tools return `Command(update=…)` with the flow updates (`zonic_flow_updates`, merged, also into `zonic_flow` so later tools see them), and the kind writes them as `flow.<key>` updates (N-05).
  - **Ports:** `RunContext.ports` carries the owner-bound MCP port (P3-01) and the new `secrets` port.
- **Idempotency key** (DR-2, T-09):
  - **Numbering:** `ToolPermitMiddleware` numbers a visit's calls in issue order in `RunContext.tool_calls`, and the kind restarts the count at each attempt.
  - **Request ID:** the MCP request ID is `runId:nodeId:visit:callIndex`. A re-executed visit whose model asks again in the same order sends the same keys although the call IDs differ.
- **Secrets** (T-04):
  - **References:** `ports/secrets.py` resolves `secret:<name>` and `{{secrets.<name>}}`. `EnvironmentSecrets` reads `ZONIC_SECRET_<NAME>`; AWS Secrets Manager is P5-04.
  - **Failures:** a missing secret is `TOOL_FAILED` naming the reference.
  - **Containment:** the value reaches only the MCP request, never events or state, which the test checks.
  - **Literal credentials:** a literal credential in tool settings (the Node.js key set, plus credential headers) fails the document with `DOCUMENT_INVALID` at load.
- **T-07:** six calls with `maxConcurrentPerTurn` 2 run at most two at once, and each result pairs with its call ID. Cancelling the run cancels the call on the server (in process).
- **Sub-flow answers:** a sub-flow node's output now also carries the child's last chat answer as `text` when its result has none. Without it, `test-relationship` (whose child's Output maps `messages`) ended `NO_OUTPUT`.
- **Scenarios:**
  - **Gate:** a requirement that is not a node kind (configured tools) counts as built once its task is complete, and a scenario's `waits` must go once its task is complete.
  - **New scenarios:** fsh-energy-agent (open-data, zonic, zonic-versioned: keywords, the agent's handoff with `analysisType`, the `analyse_energy_performance` function through the MCP fake, `Rule_1`'s variance branch, the chart view) and `test-relationship` (the agent hands off into the fsh-energy-agent sub-flow; a second turn answers directly).
  - **Runner:** it gained MCP answers per scenario, published children, nested visit names, and the timeline compared in completion order.
- **Not yet:** a cached result is not registered as a dataset (P3-05). `mcp-resource` tools are not supported (no fixture has one). The built-in `http_request` is not implemented (not on the allow list).

## Phase 4

Started 2026-10-27 after the M3 approval. Order: P4-01, P4-02, P4-03, P4-04, P4-05; stop at M4.

| Task | Status | Sanity check |
|---|---|---|
| P4-01 Swarm subgraph | Done | G-06, W-01 to W-13, K-13, L-06, F-11, F-12: 27 tests through the app and the harness; the Customer Support Swarm joins the builder scenarios with a golden stream; offline 1,507 passed. Live with P4-02: the live catalog's facility-operations Swarm hands off on the deployed gateway |
| P4-02 Interrupts and resume | Done | N-20, M-10, H-01 to H-05, H-07 to H-11, L-09, F-13: 17 tests; Approval Flow and the Facility Operations Swarm (all six paths of Section 20.5) join the scenarios with golden streams; offline 1,528 passed. Live: facility-operations pauses at publish_approval with one `sp_interrupt_create` and resumes through `sp_interrupt_answer` on the deployed gateway; live 36 passed, 2 skipped |
| P4-03 Tool approvals and the approval hook | Done | H-06, K-11, W-11 (through the app): 9 tests; offline 1,537 passed. Live: a gated `current_time` call of the facility-operations coordinator pauses the run and goes on once approved, on the deployed gateway; live 37 passed, 2 skipped |
| P4-04 Asynchronous mode and cancellation | Done | A-08 (async), A-13, H-12, E-17 (cancel through the API): 6 tests; offline 1,544 passed. Live: an `async` run answers 202 and succeeds, and a waiting facility-operations run is cancelled with its interrupts and checkpoint, on the deployed gateway; live 39 passed, 2 skipped |
| P4-05 Durable asynchronous runs, drain and handover | Done | Q-10 to Q-15, R-03, P-10, P-14, the completion callback: 13 tests with two instances on one fake gateway; offline 1,557 passed. Live: an async run drained on one instance is claimed and finished by a second, and the first is fenced off, on the deployed gateway; live 40 passed, 2 skipped |

### P4-01 notes

- **The subgraph** (`engine/agents/swarm/`) follows design 8.6 and the SP-6 result:
  - `builder.py`: `SwarmState`, one node per member (the member's graph name and trace metadata, `TimeoutPolicy(nodeTimeoutSeconds)`), `START` to the entry member, compiled once per Swarm spec. Each member node runs the member's `create_agent` graph through `invoke_child`.
  - `handoff_tools.py`: `transfer_to_<member>(message, context)` for every other member. It checks the step limit `min(maxHandoffs + 1, maxIterations)` and repetitive-handoff detection, then returns `Command(goto, graph=Command.PARENT)` with the model's message (this call only) and its acknowledgement, so the shared history never has a dangling call.
  - `member_context.py`: `shared` puts the task, the handoffs, the other members and the last message (the lines of `settings.swarm.sharedContext`) before the handoff pairs; `handoff` gives the last message and its pair only.
- **Kind `multi_agent`** (`engine/kinds/swarm_kind.py`, pattern `swarm` from the manifest's `options`): an LLM member is an agent with no tools of its own (it keeps its transfers); members' own inputs (system prompt, configured messages) are bound at the Swarm's visit against the run state. Output `{status, text, finalAgent, nodeHistory, executionTimeMs}`, with the members' usage, turn messages (the final answer for the chat, the rest internal) and flow changes.
- **The Swarm owns its deadline.** A new `owns_deadline` kind flag makes the compiler give the node no run timeout: with the node's `TimeoutPolicy` set from `executionTimeoutSeconds` (the manifest's `timeoutSecondsFrom`), LangGraph's watchdog would always fire before the kind's `asyncio.timeout`, so the error would be `NODE_TIMEOUT` instead of `SWARM_TIMEOUT`. A member past its own timeout fails the Swarm with `NODE_TIMEOUT` and the member's name.
- **Events and traces.** The event mapper treats a member's namespace segment as the Swarm node's, with `member` on `token.delta`, `tool.started` and `tool.finished` (Node.js's shape); `swarm.*` events written inside the subgraph keep the Swarm's own path. The Swarm registers its members as its child blueprint, so each member step is a node observation (typed by the member's type, visits counted per member) under the Swarm's, and each handoff an event observation under the member that handed off.
- **Hooks:** `beforeMemberCall` on the member (a `cancel` fails the Swarm with `SWARM_MEMBER_CANCELLED`); `onHandoff` on the Swarm node.
- **Contract and resources:**
  - `SwarmMemberData` gained `status`, `durationMs` and `usage`; `SwarmHandoffData` `message`; tool events `member`; `NodeSpec.description` (a member's description is in the other members' transfer tools).
  - Settings `swarm.maxHandoffs`, `maxIterations`, `sharedContext`, `handoffAcknowledgement`. Manifest: the Swarm's default `timeoutSeconds` 900.
  - Catalog: `SWARM_MEMBER_CANCELLED`, `SWARM_DETECTION_OFF` (the warning when only one detection setting is given).
- **Matches the builder:** an `entryPoint` given by member name is refused at planning (`PLAN_INVALID`), as the builder's rules do; the kind takes the member's node ID.
- **F-12** runs the Facility Operations template's Swarm node and five members alone: the whole template also compiles its approval and human-input nodes, whose `interrupt` kind is P4-02's. Its full-path scenario (Section 20.5) and F-13 come with P4-02; the helpers for it are in `scenarios.py`.
- **Live:** the live catalog has no Customer Support Swarm. Its one Swarm workflow, `facility-operations`, also has the approval, so it could not compile before P4-02. It ran live with P4-02 (see there).

### P4-02 notes

- **Kind `interrupt`** (`engine/kinds/interrupt_kind.py`): Approval and Human Input.
  - The node template calls `interrupt(request, response_schema=AnswerModel)` before anything else, so a pausing visit has no side effect and no `node.started`: the node starts once, when answered, with `resumed: true` (H-11).
  - The request binds the prompt port only: the manifest's `options.form`, the answer's JSON Schema, the expiry (`timeoutHours`, else `interrupts.defaultExpiryHours`).
  - Answer models (M-10): Approval `{approved, comment}` (the runtime adds `decidedBy`; `expired` comes from an expiry); Human Input `{value}`, typed by `inputType` through the manifest's new `options.valueSchemas` (Node.js's set) and optional when `required` is false. LangGraph validates the resume value with them; an invalid one is `INTERRUPT_ANSWER_INVALID`.
  - The response is `{{approval.response}}` / `{{human.response}}` (the decision, or the value) and is written to `responseVariable` when that names a state path. An Approval follows `approved` or `rejected`.
- **The pause** (`run/state/checkpoint_store.py`, `TurnStateStore.finish`): one `sp_interrupt_create` with the interrupt rows, the checkpoint row and the turn's messages so far, retried like the turn end.
  - The checkpoint body is the detached `TurnSaver` bundle plus the documents the run compiled (`RunContext.documents`), the row IDs mapped to LangGraph's, the seq of the pause's last event, and the turn's facts (the state it started from, the persisted keys, how many messages are saved).
  - A resumed turn saves only the messages after those.
- **The resume** (`application/resume_run.py`, `RunService.resume`):
  - The answer is checked against the stored `answerSchema` (400), then by `onResume` hooks, then recorded with `sp_interrupt_answer` (409 already answered, 410 expired, 404 another user's run).
  - The run moves to the next attempt with `sp_run_transition` (the fencing token of its saves). Its graph is compiled from the stored root document, and its sub-flows open the children it paused with (`SubflowPort.open(..., documents)`, H-09).
  - The buffer is restored, the payload is `Command(resume={id: answer})`, and the stream continues the run's seq (H-05). Any instance can resume (H-04).
  - Routes `GET /v1/runs/{id}/interrupts` and `POST /v1/runs/{id}/interrupts/{interruptId}` (`answer`, `mode`) answer as `POST /v1/runs` does (stream, sync, or 202).
- **Decision 0007:** with the vendored SQL, interrupt rows are written per pause and a run resumes with each answer (SP-8's subset rule), because `sp_interrupt_create` is idempotent per interrupt ID and no view returns answers. A row an earlier pause left pending answers `404`.
- **Expiry** (`InterruptExpiry`): every `interrupts.expiryCheckSeconds` (new setting, 60), on the server's instances only, the current pause's interrupts past `expiresAt` are handled through `sp_run_transition`. With `onExpiry: cancel`, the run is cancelled with `INTERRUPT_EXPIRED`. With `reject`, an Approval resumes as rejected (H-03).
- **Hooks:** `onInterrupt` (run level, at a pause, before its save) and `onResume` (before the answer is recorded; `fail` rejects it). Neither the trace nor the hooks run anything before `interrupt()`.
- **Traces (L-09):** the resumed turn's trace is the same (trace ID = run ID), with `interrupt.requested` at the pause and `interrupt.answered` at the resume, and the resumed nodes join it.
- **Parity fix found on the Facility Operations template:** an LLM node with `responseFormat: json` and no schema (the builder's request parsers) now has the answer's JSON object as `result` and as its fields, as the Node.js runtime does. A text answer warns `LLM_NOT_JSON` (new catalog code). No existing golden changed.
- **Scenarios:**
  - The runner answers a turn's interrupts in order, in both modes.
  - Approval Flow covers approved and rejected.
  - The Facility Operations Swarm covers all six paths of Section 20.5: SEVERITY 3; SEVERITY 8 approved; SEVERITY 8 rejected into `reviewer_feedback`; no SEVERITY line, reviewed; the quick answer; refused.
  - No vendored workflow waits for a kind any more.
- **Live on the deployed gateway** (owner `live-langgraph-verify`, tenant `live-test`):
  - The run: the live catalog's `facility-operations` hands off from the coordinator to the report writer, pauses at `publish_approval` (one interrupt row, a `pause` checkpoint at attempt 1, the run `waiting_input`), resumes on the answer, and is `published`; the checkpoint is gone and a second answer is 409.
  - What is local: the model is scripted, the script node runs under the local interpreter, the members' MCP tools are a local fake, and the weather is the real Open-Meteo call.
  - **Finding:** that live document names `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, which `resources/models.json` does not list, so the deployed runtime would refuse the workflow with `MODEL_NOT_ALLOWED`. The live test maps the ID to `claude-sonnet-4-6` in its own resources only. Adding Sonnet 4.5 to the catalog, or changing the document, is a decision for the product.
- **Not yet:** H-12 (cancel a waiting run) and `async` resumes are P4-04's. Tool approvals and the `approval` hook are P4-03's.

### P4-03 notes

- **Tool approvals** (`engine/agents/middleware/approval.py`): LangChain's `HumanInTheLoopMiddleware` in `per_call` mode.
  - It covers the node's tools that `settings.tools.requireApproval` names, with the decisions `approve`, `edit` and `reject` (`AgentFactory` now takes the list and the expiry from the settings).
  - Each gated call pauses the run with one interrupt as it starts. Its value is LangChain's `ToolApprovalRequest` with what the pause saves (kind `tool_approval`, the node, the prompt with the tool and its arguments, the expiry); its schema is LangChain's decision schema, which the pause stores as the answer schema.
  - Approved, the tool runs; edited, it runs with the reviewer's arguments (same tool, its argument types); rejected, it never runs and the model reads why (H-06). A decision outside the schema is 400.
  - It is the outermost middleware, so hooks and permits run only once a call goes ahead.
  - **Note:** it extends `HumanInTheLoopMiddleware._tool_approval_request`, a private method of LangChain 1.4. An upgrade must recheck it.
- **Hook type `approval`** (`hooks/builtin.py`, config `ApprovalHookConfig`; Node.js's, extended to tool calls as the plan asks):
  - The request is asked before anything runs: at a node, by the template's step 2 (before its own interrupt, a kind's or none), for the bindings whose `when` holds; at a tool call, by `HookMiddleware` before the `beforeToolCall` hooks.
  - The hook then applies the answer. Approved, the node or the call goes on. A rejected node is skipped with `rejectedOutput` and `rejectedHandles` (or fails, `onReject: fail`); a rejected tool call is cancelled with `message` as its result (K-11).
  - Like any other interrupt, an approval request is answered with `ApprovalAnswer` (now in `engine/approvals.py`), so the API adds `decidedBy`.
- **W-11 through the app:** with `current_time` gated, the Customer Support Swarm's tech_support member pauses mid-step. On approval the same member goes on: only its turn after the tool's result calls the model.
- **Live on the deployed gateway:** in a second facility-operations run, the coordinator's gated `current_time` call wrote a `tool_approval` interrupt row; approved, the coordinator handed to the writer and the run finished (SEVERITY 3, no review).

### P4-04 notes

- **`async`** answers `202` with `links.self` and `links.events` at once. The turn runs on this instance, as a `sync` turn nobody waits for, and so does a resume with `mode: async`. `GET /v1/runs/{id}` answers with an `ETag` (`304` while unchanged) and `Retry-After` while the run is queued or running (A-08).
  - The run starts `running`, here, without the queue. P4-05 makes `async` runs durable: queued, claimed by any instance, leased, drained and handed over.
  - A `sync` turn past `runs.syncWaitSeconds` already answered `202` (P2-04).
- **`POST /v1/runs/{id}/cancel`** answers `202` with the run (the Node.js contract). A finished run is returned as it is; another user's run is 404.
  - **A run this instance drives:** its task is cancelled, and the turn is saved as `cancelled` within `runs.cancelGraceSeconds`. The request waits that long, so the answer shows the final status (E-17).
  - **Any other unfinished run** (queued, waiting, or driven elsewhere): `sp_run_transition` to `cancelled`, which cancels its pending interrupts and deletes its checkpoint (H-12). A run another instance drives keeps running there until its turn-end save is refused (the run has finished); stopping its task across instances is P4-05's.
- **A-13** needed no change: `/readyz` answers 503 `not_ready` naming the gateway, and stays 200 with `tracing: degraded` while exports fail.

### P4-05 notes

- **Instances and leases** (`run/executor/instance.py`, `leases.py`):
  - A process is a `RunInstance`: an ID, and the internal address other instances reach it at (`api.internalAddress`, else this host and `api.internalPort`, built in `config/env.py`).
  - With durable runs, every run started, resumed or claimed here holds the instance's lease. One `sp_run_heartbeat` renews them every `asyncRuns.leaseSeconds / 3`. A run it reports lost is stopped here without saving (`ActiveRun.stopped`): the newer attempt would refuse its writes anyway.
- **The queue** (`run/executor/claimer.py`):
  - An `async` run that finds no free permit (`RATE_LIMITED` or `OVERLOADED`) is stored `queued`, with the caller's snapshot and its callback, after its plan is checked; `202` at once (Q-10, Q-15).
  - `RunClaimer` calls `sp_run_claim` while permits are free: at most `claimBatch` runs, round-robin across tenants (Q-11), and running runs whose lease expired. It claims every `claimIntervalMilliseconds` while runs come, every `idleClaimIntervalMilliseconds` while none do.
  - A claimed run with a checkpoint of kind `async` continues from it with `None` input, so its completed supersteps do not run again. Otherwise it runs its turn again from the session's saved state. A `stream` or `sync` run lost with its instance fails with `SHUTDOWN` (`runs.onInstanceLoss`; Q-12).
- **Fencing:** every write of a run carries its attempt: the finish, the pause, the checkpoint, the dataset chunks (P-10). After a takeover, the old owner's writes are `RUN_LEASE_LOST` (Q-13).
- **Drain** (`RunService.drain`, at `AppDeps.aclose`):
  - The claimer stops. Each `async` run gets `RunControl.request_drain`; LangGraph stops it at the next superstep boundary (`GraphDrained`). Its checkpoint is saved with `sp_checkpoint_save` and it is released to the queue with `sp_run_release` (R-03).
  - `stream` and `sync` runs get `shutdown.drainSeconds` to finish, then fail with `SHUTDOWN`; waiting runs are not touched.
  - **Differs from the plan's wording:** the vendored `sp_checkpoint_save` writes kind `async`, not `drain`, so a drained run's checkpoint has kind `async`, like write-behind's. The claim treats both the same.
- **`durability="async"`** for every `async` turn (SP-10: under `exit`, a child graph's work since its node started would be lost on a drain). With `state.asyncCheckpoints`, write-behind saves the latest checkpoint at most every `checkpointIntervalSeconds`, and only when it changed; nodes never wait for it (P-14). It is off by default.
- **Completion callbacks** (`run/callbacks.py`): a run started with `options.callback` (`{url, secretRef}`, new in the request) posts its summary when it ends: run, session, status, result and error code, never its messages. The post goes through the egress guard, signed in `asyncRuns.callbackSignatureHeader` with HMAC-SHA256 under the named secret, and is retried `callbackRetryAttempts` times with backoff (new settings: the header, timeout and backoff).
- **Events across instances** (Q-14): `GET /v1/runs/{id}/events` for a run another instance drives (its lease names another address) proxies that instance's `/internal/runs/{id}/events` (new on the internal app), after `Last-Event-ID`. The events come through as they are: seq continues and nothing is duplicated.
- **Not covered:** a run that resumed from an interrupt and then lost its instance has only its pause checkpoint, whose answers no vendored view returns (decision 0007). Such a run runs its turn again from the saved state and asks again. The cross-instance proxy and the callbacks are tested offline only: no second process or callback endpoint is reachable from the live suite.

### Checkpoint M4 (2026-10-27)

Phase 4 is built: P4-01 to P4-05, 48 tasks complete in all. Decision 0007 (interrupt rows per pause) is new.

| M4 asks | Status |
|---|---|
| The Customer Support Swarm runs with scripted handoffs (F-11) | Triage hands off to billing, which answers; a golden stream in stream and sync mode; its limits (11 steps, window 6, 2 unique) checked |
| The Facility Operations Swarm runs its full path (Section 20.5) with an LLM member, an approval that pauses and resumes across requests, and the rejected branch through Human Input (F-12, F-13) | All six paths as one scenario with a golden stream: SEVERITY 3; SEVERITY 8 approved; SEVERITY 8 rejected into `reviewer_feedback`; no SEVERITY line, reviewed; the quick answer; refused. Live: the live catalog's facility-operations pauses at `publish_approval` and resumes on the deployed gateway |
| An interrupt inside a sub-flow and one inside a loop resume without repeating completed nodes (H-07, H-08, H-11) | The child's LLM does not run again; the loop's approval resumes its own visit; nothing before `interrupt()` has a side effect |
| An `async` run drained on one instance finishes on another without repeating completed supersteps (R-03) | Offline and live: the drained run is checkpointed after its last superstep, released, claimed by the second instance and finished there with only the nodes left |
| Fencing rejects the old owner (Q-13) | Offline and live: the old attempt's `sp_run_finish` is `RUN_LEASE_LOST`, and its heartbeat reports the run lost |

**Gates:** ruff, ruff format, mypy (236 files), import-linter (4 contracts), `gen --check`, `sync_builder --check`, `sync_recordings --check` and `catalog_check` (284 cases with tests, 269 due) all pass. `sync_sql --check` still fails until the Node.js repository commits its SQL changes (now `db/observability/001_schema_create.sql` and `005_procedures.sql`, and runtime `004_views.sql`, `005_procedures.sql`, `006_grants.sql`). 1,557 offline tests pass; the live suite passes 40 and skips 2.

**For review:**
- decision 0007 (interrupt rows per pause; resume with each answer);
- the parity fix for `responseFormat: json` LLM nodes without a schema (P4-02), and the `owns_deadline` kind flag (P4-01);
- the live finding: the live facility-operations document names Sonnet 4.5, which `resources/models.json` does not list (the deployed runtime would refuse it with `MODEL_NOT_ALLOWED`);
- the live checks that wrote to the product database: runs of owner `live-langgraph-verify` and tenant `live-test` that paused, resumed, were cancelled, and one drained and claimed by a second instance. The claim ran only after a read showed that run was the only claimable one; none is left queued or running.
