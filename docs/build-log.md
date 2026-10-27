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
