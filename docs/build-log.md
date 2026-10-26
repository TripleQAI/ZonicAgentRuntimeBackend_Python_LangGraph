# Build Log

Progress against `docs/zonic-agent-runtime-langgraph-implementation-plan.md`, with the sanity checks run after each task and every deviation from the plan.

## Phase 0

| Task | Status | Sanity check |
|---|---|---|
| P0-01 Server scaffold | Done | `uv sync`; ruff, mypy, import-linter clean |
| P0-02 Base models and markers | Done | mypy strict |
| P0-03 Resource models | Done | M-01, M-02 |
| P0-04 Resource files and overlays | Done | M-01, M-02 in dev, test, prod |
| P0-05 Settings loading | Done | M-01, M-02 |
| P0-06 Generated schemas and principle tests | Done | M-03, X-08; `scripts.gen --check` |
| P0-07 Gateway port and adapter | Done | P-03, P-04, M-07 |
| P0-08 `agent_runtime` SQL and gateway registration | Done (D-2 request outstanding) | P-01, P-02, P-15: 56 SQL tests pass on Neon (PostgreSQL 18.6), on top of the builder's real `agent_builder` scripts |
| P0-09 `agent_observability` SQL | Not started | |
| P0-10 LangGraph spikes SP-1 to SP-10 | Not started | |
| P0-11 ScriptedChatModel and fakes | Not started | |

All gates at the end of P0-08: ruff check and format, mypy (61 files), import-linter (4 contracts kept), `scripts.gen --check` (12 files), `scripts.sync_sql --check` (4 runtime and 6 builder files), `scripts.catalog_check` (296 cases, 10 with tests, none missing), pytest (65 offline tests passed, 56 SQL tests passed).

### P0-08: where the SQL comes from

| Files | Source | Owner |
|---|---|---|
| `db/runtime/001_schema.sql`, `002_functions_triggers.sql`, `003_indexes.sql`, `005_procedures.sql` | `ZonicAgentRuntimeServer_Nodejs/db/runtime`, commit `5c75833`, byte for byte (`db/runtime/SOURCE.json`) | Node.js runtime project; refresh with `uv run python -m scripts.sync_sql runtime`, never edit here |
| `db/runtime/004_views.sql`, `006_grants.sql`, `run_all.sql`, `999_drop_all.sql` | Written here | This project |
| `tests/fixtures/builder_db/` (builder `001` to `005`, `009_api_views.sql`) | `ZonicAgentBuilderServer_FastAPI/db`, commit `2a14e4a`, byte for byte | Builder project; installed only inside the SQL tests' rolled-back transaction |

Both runtimes use one `agent_runtime` schema, so the tables and the 14 procedures are shared rather than written twice. CI runs `scripts.sync_sql --check`, which fails when a vendored file is edited or its source changes. The source repositories are never modified.

Also written: `deploy/pgfoundation.agentruntime.yaml`, `scripts/init_db.sh`, `scripts/init_db.ps1`, `scripts/sync_sql.py`, `tests/sql/`.

Open: decision D-2 (gateway caller and datasource registration) is not yet requested.

### P0-08: how the plan's deltas are met without changing the shared procedures

| Plan item | How it is met |
|---|---|
| DR-8 checkpoint `format`, `langgraph_version` | Stored in the checkpoint `body` (`format`, `langgraphVersion`) and exposed as columns of `v_checkpoint` |
| DR-8 `kind = 'drain'` | A drain saves through `sp_checkpoint_save`, recorded as `kind = 'async'`; `sp_run_release` keeps the row (P-15 test) |
| DR-8 `sp_run_claim` returning `has_checkpoint` | `v_run.has_checkpoint`; the claimer reads it after claiming (one extra view read per claim batch) |
| DR-9 `last_event_seq` | `body.lastEventSeq`, written by the TurnSaver with every pause and drain checkpoint, exposed on `v_run` and `v_checkpoint`; a finished run needs none |
| LangGraph interrupt IDs | `runtime_interrupt.interrupt_id` is `uuid`; LangGraph's 32-hex `Interrupt.id` casts to it unchanged (P-15 test) |

### Deviations

| Item | Plan | Built | Reason |
|---|---|---|---|
| SQL test database | Assumption 5: a `runtime_test` branch; production out of scope | Neon project `agent-build-langgraph`, branch `production` | The user's decision (2026-10-25). The branch was empty. Every SQL test runs in one transaction that ends in ROLLBACK, and the branch was confirmed unchanged afterwards. |
| `agent_runtime` tables and procedures | Written in P0-08 | Vendored from the Node.js runtime | The user's existing schema; one shared `agent_runtime` schema for both runtimes. |
| `v_agent_catalog` | Node.js 004: `channels_text` LIKE filter and `security->'groups'` | One row per channel; `security` returned whole | The builder saves `publishChannels` as objects (`{refId, code, name}`), which the Node.js view turns into JSON text, so its LIKE filter never matches a builder workflow; builder `security` holds `activities`, not `groups`. Reported for the Node.js project. |
| Error mapping | `002` header prefixes (`23R01 run_in_progress`, ...) | The Node.js `fn_raise` reason tokens (`run_not_found`, `stale_attempt`, `already_finished`, ...) mapped in `http_gateway.py` | The procedures raise the bare token as the message; the gateway passes it through. |
| Error catalog | 63 codes | 64 codes: `RUN_LEASE_LOST` added | `stale_attempt` (fencing) needs its own code. |
| Object counts | 8 tables, 9 views | 9 tables (including `schema_migrations`), 9 views, 14 procedures | As vendored. |
| PostgreSQL version | 17 in CI | Neon runs 18.6 | The scripts pass on both; CI keeps 17 as the floor. |

### Running the SQL tests locally

Put `ZONIC_SQL_TEST_DSN=...` in the git-ignored `.env`, then run `uv run pytest -m sql`. psql runs in the `postgres:17` Docker image and gets the DSN through its environment.
