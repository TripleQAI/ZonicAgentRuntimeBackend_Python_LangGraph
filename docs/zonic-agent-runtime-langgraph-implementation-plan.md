**INSITEINTELLIGENCE.COM**

---

# Zonic Agent Runtime on LangGraph: Implementation Plan

Task-level plan for building the backend of the LangGraph runtime design in `ZonicAgentRuntimeBackend_Python_LangGraph` with Claude Code: a review of the design, then what to build, in which files, in what order, and which tests prove each step. The runtime UI is deferred

| Item | Detail |
|---|---|
| Status | Draft for review, revision 2. Revision 2 removes the runtime UI from the plan (Section 13). No code has been written. |
| Design | [Zonic Agent Runtime on LangGraph: Target Architecture Design](zonic-agent-runtime-langgraph-architecture-design.md), revision 2 |
| Scope | Backend only. The runtime UI (`ZonicAgentRuntimeUI_NextJS`) is deferred and will be planned separately; this plan delivers the API, streaming and content-part contract it will use, published from `generated/` (design Section 3.6) |
| Target repository | `ZonicAgentRuntimeBackend_Python_LangGraph` (initial commit and README only; `docs/` not yet committed) |
| Dependencies | `ZonicPostgresqlServer_FastAPI` (gateway), `ZonicMCPServer_FastAPI` (tools and the cache service), `Strands-visual-builder` (workflow documents, node registry), `ZonicAgentBuilderServer_FastAPI` (schema 1.0 documents) |
| Test data | Workflow documents from `Strands-visual-builder` at commit `24e29ee` (21 distinct documents in two dialects), the open test data in schema `sample_open_data` on the Neon dev branch, and public keyless APIs (Section 20) |
| Audience | Claude Code, which implements the tasks, and the engineers and architect who review its work |

---

## 1. Summary

The plan has two parts. Section 2 reviews the design and lists 14 findings: none blocks the build, three are high and change how specific tasks are built, and all are folded into the task rows here with new catalog cases. Sections 3 to 20 turn the design into 54 backend tasks in six phases. Each task names the files it creates, the tasks it depends on, and the sanity catalog cases (design Section 19.2, the plan additions in Section 2, and the fixture cases F-01 to F-18 in Section 20.3) whose tests prove it.

| Phase | Builds | Ends when (checkpoint) |
|---|---|---|
| 0. Foundations | Scaffold with uv and Python 3.13, CI with the principle checks, Pydantic model base, settings and resources, gateway client, both database schemas, `ScriptedChatModel`, the ten LangGraph verification spikes | M0: CI green, schemas applied, spike decisions recorded |
| 1. Workflow model and compiler | Document and node models, dialect normalizer, loader, blueprint, templates, node input and output, `RunState` and `RunContext`, the compiler with `Command` routing and barrier joins, the node template with error policies, loops and limits | M1: every builder and platform document compiles to a `StateGraph` and runs with test kinds |
| 2. Execution, state and API | Run driver and event stream, agent layer and core node kinds, FastAPI app, `TurnSaver` and per-turn persistence, run executor and governors, hooks, Langfuse tracing stored in PostgreSQL, builder scenarios | M2: builder workflows without tools run end to end in stream and sync modes |
| 3. Integrations and data | MCP tools, HTTP and database nodes, datasets, remaining node kinds, views, sub-flows, remaining hook types, trace APIs, recordings | M3: every fixture runs against recorded responses |
| 4. Swarm, interrupts and durable runs | Swarm subgraph, interrupts and resume, tool approvals, `async` mode, durable runs with leases, drain and handover | M4: both Swarm templates and all interrupts run; an `async` run drains on one instance and finishes on another |
| 5. Hardening and release | Security, performance validation, live sanity, dev deployment, documentation, release gates | M5: the backend is ready for the dev environment |

**Deferred: the runtime UI.** The Strands plan's UI tasks (U0-01 to U0-04, U5-01 to U5-11) are not part of this plan (Section 13). The backend is complete and testable without them: the client contract is generated and snapshot-tested from the first API task, and a developer test client (`scripts/run_client.py`, task P2-03) drives every flow from a terminal. Until the UI ships, end users stay on the Django runtime (design question Q11).

Compared with the Strands implementation plan, two tasks disappear (the activation gate and the loop orchestrator are LangGraph behavior here), the 15 UI tasks are deferred, and four tasks appear: `ScriptedChatModel` as a Phase 0 fake, because six spikes need it; `RunState` and its reducers; the error-policy handler on LangGraph's `error_handler`; and `TurnSaver` with LangGraph's checkpointer conformance suite.

---

## 2. Design Review

### 2.1 Verdict

The design is buildable as written. Its central choices are well supported by the LangGraph documentation and remove the riskiest parts of the Strands design: no activation gate, no loop orchestrator, and no custom snapshot store. Those were the Strands design's risk R11 and its spikes (1) and (3). The two areas that carry real build risk are both covered by named spikes with fallbacks:

- **Persistence and resume through `TurnSaver`:** spikes SP-2 and SP-10.
- **Streaming and tracing of graphs invoked inside nodes:** spikes SP-3 and SP-4.

The review found no contradiction with the Strands design's contracts (API, events, schemas, dialects, hooks).

### 2.2 Findings

Severity: **High** changes how a task is built, and building it the obvious way would be wrong. **Medium** fills a gap the design leaves. **Low** is wording or a default. Every finding is resolved in the task named; "design revision 2" lists what the design's next revision should state.

| ID | Severity | Finding | Resolution in this plan | New cases |
|---|---|---|---|---|
| DR-1 | High | Design 7.1 gives `flow` a reducer that "warns on same-key writes in one step". LangGraph reducers are pairwise `(left, right)` and do not know the superstep or the writer, so the warning cannot be produced as described. The documentation also says the order of updates from a parallel superstep is not guaranteed, so "last write wins" is nondeterministic. The same applies to `result: first_output_wins`. | The `flow` channel stores `{values, provenance: {key: [step, writer]}, conflicts}`. The template writes `FlowPatch` objects carrying the writer node ID and the superstep (`config["metadata"]["langgraph_step"]`). The reducer detects same-step writes from different writers and resolves them deterministically by node ID order, records the conflict, and the resolver reads only `values`. `result` uses the same provenance (lowest step wins, then lowest node ID). Task P1-08. | E-21 |
| DR-2 | High | Design 8.5 sets the tool idempotency key to `runId:nodeId:visit:toolCallId`. Tool call IDs are generated by the model, so a re-executed node (after an interrupt, a retry or a restarted turn) gets new IDs and the key never matches. That defeats the at-least-once mitigation in design 9.5 and 20.3. | The key is `runId:nodeId:visit:callIndex`, where `callIndex` counts tool calls within the node visit in issue order, kept by `ToolPermitMiddleware`. The same key goes to MCP `execute`, HTTP nodes and callbacks. Task P3-02. | T-09 |
| DR-3 | High | Design 6.8 keys the compiled-graph cache by revision, manifest version and tool catalog version. A workflow can be both a catalog root (compiled with `TurnSaver`) and another workflow's sub-flow (compiled with `checkpointer=None`). With that key, whichever is compiled first would be reused in the wrong role. | The cache key adds the role (`root` or `child`). `WorkflowCompiler.compile(blueprint, role=...)`. Task P1-09. | G-13 |
| DR-4 | Medium | Design 7.1's `merge_writable_system` reducer cannot see which keys are writable or who wrote them. | Enforced when the template builds the `StatePatch`: writes to `system` outside `settings.templates.writableSystemKeys` fail with `STATE_READ_ONLY` before the update leaves the node. The reducer is a plain merge. Task P1-08. | E-22 |
| DR-5 | Medium | Design 12 says the callback handler enriches node observations "from the node record the template returned". A node returns a `Command`, and an error-handler run is a separate callback run. | The handler unwraps `Command.update` to read the `NodeRecord`, and attributes error-handler runs to the failed node's observation through `ObservationBridge`. Task P2-07. | L-17 |
| DR-6 | Medium | Child graphs (sub-flows, agents, swarms) are invoked inside nodes. Callbacks, the checkpoint namespace, `RunContext` and the deadline must reach them, but the design states this only in passing (7.2). | One helper `invoke_child(graph, input, runtime, config)` passes `config` (callbacks, tags, metadata, `recursion_limit`) and `context=runtime.context`. It is the only allowed way to invoke a compiled graph from a kind (new principle rule 18). Tasks P1-10, P3-09. | G-14 |
| DR-7 | Medium | Visit and step limits read `visits` from the superstep's snapshot, so parallel siblings in one superstep can overrun `maxSteps` by up to `max_concurrency - 1`. | Accepted and documented: the check uses snapshot plus one; `recursion_limit` stays the hard backstop. Task P1-11. | E-08 (extended) |
| DR-8 | Medium | Design 15.2 says the `agent_runtime` schema is "unchanged except the checkpoint row". The build also needs: the `format`, `langgraph_version` and `kind = 'drain'` columns; `sp_run_release` that keeps a drained checkpoint; `sp_run_claim` returning whether a checkpoint exists; and a gateway datasource registration file like the builder's `deploy/pgfoundation.agentbuilder.yaml`. | All in P0-08: `deploy/pgfoundation.agentruntime.yaml` (datasources `agentruntime` and `agentobservability`, raw SQL off) and the SQL deltas. | P-15 |
| DR-9 | Medium | Event `seq` must continue across a resume (design case H-05), but nothing persists the last `seq` of a paused or drained run. | `runtime_run.last_event_seq` is written by `sp_interrupt_create`, `sp_checkpoint_save` and `sp_run_finish`; `ResumeRun` continues from it. Tasks P0-08, P2-01. | S-13 |
| DR-10 | Medium | Design 8.1 step 2 calls `interrupt()` before input binding, but the interrupt's prompt is itself a port (`prompt`, `message`). | Interrupt kinds bind only their prompt port before `interrupt()`; prompt binding is pure template resolution, with no secret resolution and no dataset reads. Everything else is bound after the answer. Task P4-02. | H-11 (extended) |
| DR-11 | Medium | Design 10 maps the event namespace "to the builder node ID" without the rule. In LangGraph's protocol events the namespace is `["<node>:<task id>", ...]` from the root, and a child graph invoked inside a node adds its own segments. | The first segment's name is the root node's LangGraph name (mapped back through the blueprint's name index); later segments build `path` (sub-flow node, swarm node, member, agent's internal `model` or `tools` node, which are dropped from `path`). Task P2-01. | S-04 (extended) |
| DR-12 | Medium | The Strands plan's fixture cases F-01 to F-18 are not in the design's catalog, and F-08 and F-15 use Strands wording (skipped nodes, Strands graph snapshots). | Carried into this plan (Section 20.3) with LangGraph wording: untaken branches "are never scheduled", and compiled snapshots record LangGraph nodes, `defer` flags, policies and destinations. | F-01 to F-18 |
| DR-13 | Low | The Django runtime runs an unpinned LangGraph. The Strands plan observed a default recursion limit of 10,007 under 1.2.17, while the documentation states 1000 since 1.0.6. | The runtime never relies on the default: `recursion_limit` is always set from settings (G-12). Spike SP-1 records the installed default. | None |
| DR-14 | Low | Python 3.14 is installed locally and uv is 0.8.8 (August 2025); the design targets 3.13. | P0-01 pins `requires-python = ">=3.13,<3.14"`, runs `uv python install 3.13`, and upgrades uv; the Docker image is `python:3.13-slim`. | None |

**The next design revision** (revision 2 only deferred the UI) should state DR-1 to DR-11 in the design text, add the new cases (E-21, E-22, G-13, G-14, L-17, P-15, S-13, T-09) to its catalog, and add F-01 to F-18. Until then, this plan's Section 2 is the reference for those points (Section 3, assumption 1).

---

## 3. Inputs and Assumptions

1. The design, revision 2, is the baseline, as amended by the findings in Section 2. "Design Section N" refers to it; plain "Section N" refers to this plan. Where this plan and the design disagree outside Section 2, the design wins; report the difference instead of choosing silently.
2. Python 3.13 managed by uv; LangGraph pinned to one 1.2 minor (at least 1.2.12, for interrupt `response_schema`), LangChain to one 1.4 minor with the `mcp` extra, `langchain-aws`, `langchain-openai`, `langchain-anthropic` and `langfuse` 5.x each pinned to one minor (design AD-15). Exact pins are recorded by P0-10.
3. The gateway (pgfoundation) is consumed as a service. Registering the runtime's caller subject and datasources is a configuration change in that project, owned by its maintainers (decision D-2).
4. Workflows come from the Strands builder schema (`agent_builder`, documents 2.0, 18 node types), from InSite platform imports stored unchanged (`agent.json` and `sub_agents.json` are byte-identical in `Strands-visual-builder` and `ZonicAgentBuilderServer_FastAPI`), and from schema 1.0 documents of `ZonicAgentBuilderServer_FastAPI`. The builder registry files are vendored at a pinned revision with a sync check (P1-05).
5. Neon branches: a new `runtime_test` branch for SQL and repository tests, `dev` for the dev environment. Production is out of scope.
6. The Zonic MCP server findings F1 to F3 are fixed by that project before the runtime goes live against it (D-6).
7. Test workflows are vendored into `tests/fixtures/workflows/builder/` with `SOURCE.json` (source path, commit, SHA-256 per file) and refreshed only by the sync script (Section 20.7). Dependency repositories are never modified by this work.
8. Every test runs offline with fakes (`ScriptedChatModel`, `InMemoryGateway`, an in-process FastMCP server, `FakeEgress`, a fake cache service) unless it is marked `sql` or `live`, which need credentials and are opt-in.
9. The runtime UI is out of scope (design Section 3.6). No task creates or changes `ZonicAgentRuntimeUI_NextJS`. Every client-facing behavior is proved by API tests, the published-contract tests (A-10, A-14) and the developer test client (A-15); checkpoint demonstrations use the test client.

---

## 4. Starting Point

| Area | Finding | Consequence for the plan |
|---|---|---|
| Server repository | Initial commit, README only; `docs/` (design and its HTML) untracked | P0-01 scaffolds everything and commits `docs/` first |
| UI repository | `ZonicAgentRuntimeUI_NextJS`: initial commit, README only | Not touched by this plan (deferred, Section 13) |
| Local tools | Python 3.14.7 and uv 0.8.8 installed | DR-14 |
| Gateway | Lambda deployment, raw SQL off, about 30 concurrent operations account-wide, offers `fetch-since`, batches and transactional batches; one caller (`zonic-agent-builder`); the builder registers its datasource in its own `deploy/` YAML | D-2; P0-08 writes `deploy/pgfoundation.agentruntime.yaml` the same way |
| Builder schema | `v_agent_export` returns the full document; no publish step; ownership not enforced on reads | P0-08 adds `agent_runtime.v_agent_catalog` with the Q2 visibility rule |
| LangGraph | Features the design uses are recent: `TimeoutPolicy`, `error_handler`, `set_node_defaults`, `RunControl` (1.2), event streaming v3 (1.2), interrupt `response_schema` (1.2.12); a checkpointer conformance suite (`langgraph-checkpoint-conformance`) exists | P0-10 spikes SP-1 to SP-10 before P1-09; P2-04 runs the conformance suite in CI |
| LangChain | `create_agent` with middleware; `langchain.mcp.MCPAdapter` beta in 1.4 (FastMCP-based, supports in-process servers, which makes MCP tests offline) | P0-10 SP-7; P3-01 uses the in-process server as the MCP fake |
| Langfuse | Python SDK v5, OpenTelemetry-based, accepts `tracer_provider`, `should_export_span`, `mask_otel_spans`, `sample_rate` | P2-07 and P2-08 need no Langfuse server |
| Django runtime | Unpinned LangGraph; six workflow test files (`all-agents`, `complex-agent`, `energy-agent`, `fsh-*`) | P1-01 vendors a Django fixture only when it covers a case the builder fixtures do not |
| Builder workflow topology | 16 of 21 workflows join branches (up to 12 incoming edges); 8 contain loops, one with 6 entries | Covered natively by LangGraph; P1-09 and P1-11 prove it on every fixture |
| Builder UI | `ZonicAgentBuilderUI_NextJS` writes free-text Rule `expression`s and has no Output mapping field | P1-03 implements design 5.2's `RULE_EXPRESSION_UNSUPPORTED` and `OUTPUT_MAPPING_MISSING`; Q9 |

---

## 5. Decisions and External Prerequisites

These need people or teams outside the code. Claude Code never makes them; it records the assumption it builds on in the pull request.

| ID | Decision | Blocks | Recommendation | Until decided |
|---|---|---|---|---|
| D-2 | Register gateway caller `zonic-agent-runtime` and the datasources of `deploy/pgfoundation.agentruntime.yaml` on `neondb_dev` and the test branch | SQL and live tests (P0-08 onwards) | Ask the gateway maintainers for the change and a redeploy | `InMemoryGateway`; SQL scripts through a local gateway on the test branch |
| D-3 | Runtime hosting | P5-04 | ECS Fargate, one uvicorn worker per task at 1 vCPU and 2 GB, ALB idle timeout above the SSE heartbeat gap, stickiness on `runId` as an optimization (design Q7) | Generic `Dockerfile`; no infrastructure code |
| D-4 | Exact pins for LangGraph, LangChain, provider packages and Langfuse | P0-10 onwards | The newest minors that pass SP-1 to SP-10 | Latest minors at P0-01, re-pinned by P0-10 |
| D-5 | Fallbacks if a spike fails | P1-09, P2-01, P2-04, P2-07, P4-01, P4-02, P4-05 | The fallback named in design 20.1 for that spike | Stop at M0 for the decision |
| D-6 | MCP server findings F1 to F3 fixed | P5-06 | Owned by the MCP server team; release gate | In-process FastMCP fake; live MCP tests opt-in |
| D-7 | How the MCP server reaches the open test data | P3-04, P3-12 | PostgreSQL connections registered by name (`sample_open_data`) with the read-only role | Recordings made directly as `sample_open_data_reader` |
| D-8 | Neon plan for `agent_observability` | P2-08 in shared environments | Test branch for development; confirm plan limits before shared use | Test branch |
| D-9 | Gateway capacity for the runtime | P5-02, P5-04 | Confirm the Lambda concurrency for the runtime's caller, or use the gateway's container deployment | Measure in P5-02 against a local gateway |
| Q1 | Identity provider | P2-03 in shared environments | `strands-visual-builder-users-docker` (`us-east-1_lDBxGGZYh`, tripleq-development) for dev and the test client; issuer and audience in settings | Local OIDC test issuer |
| Q2 | Publishing workflows to end users | P1-04 | Visible when root, enabled, and `publishChannels` contains the runtime channel from settings | Build the recommendation |
| Q3 | Approved model providers | Live runs of the 9 platform workflows (P5-03) | Bedrock and Azure OpenAI; OpenAI and Anthropic present in the catalog, disabled | All providers in `ModelFactory`; only Bedrock enabled; platform workflows on `ScriptedChatModel` |
| Q5 | Script node sandbox | P3-07 | Ship the Piston adapter, keep the node disabled by feature flag | Build the recommendation |
| Q6 | Langfuse server copy | None | No server; the runtime trace viewer covers support | Optional exporter off |
| Q8 | Builder form for hooks | None (a builder change, deferred with the UI work) | Runtime configuration only | Hooks from `resources/hooks.json` and documents only |
| Q9 | Structured Rule conditions and an Output mapping form in `ZonicAgentBuilderUI_NextJS` | None for the runtime | Builder change; `parse_simple` off by default | `RULE_EXPRESSION_UNSUPPORTED` |
| Q10 | Checkpoint encryption and its key service | P2-04 in shared environments | `EncryptedSerializer` on in shared environments, AES key from Secrets Manager | Off locally; on with a test key in CI |
| Q11 | When end users switch from the Django runtime, given the UI is deferred | P5-06 (switch only) | Switch when the runtime UI ships; no interim client | The backend is released to dev for API use and the test client only |

Design question Q4 (importing Django chat history) is not needed: the runtime starts with empty history.

---

## 6. Rules for Building with Claude Code

### 6.1 Repository setup

1. P0-01 adds a short `CLAUDE.md` that points to the design and this plan, lists the commands of Section 18, and restates the rules below and design Section 16.4. It links to the documents instead of copying them.
2. The design and this plan live in `docs/` of the server repository. The UI repository is not touched (Section 13).

### 6.2 How to build one task

1. **Start only when its dependencies are done** (Section 7): merged, with their catalog cases passing.
2. **Read first:** the task row, the design sections it names, the Section 2 findings it names, and the definitions of the cases in its "Proves" column.
3. **Contracts first:** a task that changes the API, events, content parts, resource files or node input and output changes the Pydantic models first, then regenerates `generated/` (`uv run python -m scripts.gen`).
4. **Fakes with ports:** every port gets its fake in the same task (gateway, MCP, egress, sandbox, secrets, cache service, clock, IDs, model).
5. **Tests prove the task:** every case ID in "Proves" has at least one test whose name contains the ID (for example `test_E03_exclusive_branches_join_once`), so `scripts/catalog_check.py` finds it. A test is never weakened, skipped or deleted to make a task pass; if a case cannot pass as written, stop and report.
6. **Gates:** run every gate of Section 18 marked "every pull request", and fix every failure.
7. **One branch and pull request per task:** `feat/<task-id>-<short-name>`, title `<task-id>: <summary>`, a description listing the cases proved, the design sections followed and any assumption for an open decision.
8. **Scope:** build what the row names; report a missing piece no task covers instead of adding it.

### 6.3 When to stop and ask

When a decision in Section 5 blocks the task beyond its "Until decided" column; the design and this plan disagree outside Section 2; a case contradicts the design; a dependency project would need a change; a spike fails (D-5); a vendored fixture has a construct no dialect rule covers (B-15); or LangGraph behaves differently from what the design relies on in a way no spike covered.

### 6.4 Parallel work

Tasks in one wave do not depend on each other and may be built in parallel, each in its own git worktree and Claude Code session. Merge one at a time and rerun the gates after each merge.

### 6.5 Never

Commit to dependency projects; read `os.environ` outside `src/zonic_runtime/config/env.py`; add node type, handle, model, URL, limit or SQL literals to `src/`; call `print` or `logging` outside `observability/logging_policy.py`; add scheduling code outside LangGraph; write state to the database during a turn; invoke a compiled graph from a kind except through `invoke_child`; catch `Exception` broadly in `engine/`; store secrets in documents, fixtures or tests (design Section 16.4, rule 18 from DR-6).

---

## 7. Execution Order

### 7.1 Dependencies

A task may start when every task in its "Depends on" column is done; "Phases 1 to 4" means every task of those phases. No task depends on a later phase. A case that needs a later task is added to the earlier task's tests when the later task lands; the earlier task does not wait.

### 7.2 Waves

| Wave | Tasks |
|---|---|
| 1 | P0-01 Server scaffold; P0-08 `agent_runtime` SQL and gateway registration |
| 2 | P0-02 CI; P0-03 Model base; P0-06 Principle checks; P0-09 `agent_observability` SQL; P0-11 `ScriptedChatModel` and core fakes |
| 3 | P0-04 Environment and settings; P0-07 Gateway client; P0-10 LangGraph spikes; P1-01 Document models and fixtures |
| 4 | P0-05 Resource models and files; P1-02 Node configuration and engine record models |
| 5 | P1-03 Dialect normalizer; P1-05 Blueprint; P1-06 Templates and conditions |
| 6 | P1-04 Loader and catalog; P1-07 Node input and output contract |
| 7 | P1-08 `RunState`, reducers and `RunContext` |
| 8 | P1-09 Compiler, routing and graph cache |
| 9 | P1-10 Node template, kind registry and error policy |
| 10 | P1-11 Loops and limits; P2-01 Run driver and event stream; P2-02 Agent layer and core node kinds; P3-03 Egress guard and HTTP node; P3-07 Script node; P3-08 View node and content parts |
| 11 | P2-03 API, use cases, app factory and test client; P2-06 Hook framework; P2-07 Langfuse tracer; P3-01 MCP integration; P3-09 Sub-flows |
| 12 | P2-04 `TurnSaver` and per-turn persistence; P2-05 Run executor and governors; P2-08 Gateway span exporter; P2-09 Builder scenarios; P3-02 Tool factory; P3-10 Remaining hook types; P3-12 Recorded responses |
| 13 | P3-04 Database node; P3-06 Tool, external agent and guardrail kinds; P3-11 Trace and usage APIs; P4-01 Swarm subgraph; P4-02 Interrupts and resume |
| 14 | P3-05 Datasets; P4-03 Tool approvals and the approval hook; P4-04 Asynchronous mode and cancellation |
| 15 | P4-05 Durable asynchronous runs, drain and handover |
| 16 | P5-01 Security pass; P5-02 Performance validation; P5-03 Live sanity; P5-05 Documentation |
| 17 | P5-04 Deployment to dev |
| 18 | P5-06 Release gates |

### 7.3 Checkpoints

Each phase ends at a checkpoint (M0 to M5): every task merged, every case in their "Proves" columns passing in CI (except cases waiting for a later task), and the checkpoint's own checks passing. Stop at each checkpoint for the architect's review.

---

## 8. Phase 0: Foundations

**Goal.** Repository, tooling, CI with every principle check, the Pydantic model base, settings and resources, the gateway client, both schemas on the test branch, the core fakes, and the spikes that confirm the LangGraph behavior the design relies on.

| ID | Task | Files and changes | Depends on | Proves |
|---|---|---|---|---|
| P0-01 | Server scaffold | Commit `docs/` first. `pyproject.toml` (uv; `requires-python = ">=3.13,<3.14"`; dependencies pinned per D-4; dependency groups `dev`, `test`); `uv.lock`; `src/zonic_runtime/` package skeleton matching design 18.1 (empty modules with docstrings); `ruff.toml` (rules `E,F,I,UP,B,SIM,RUF,T20,ASYNC,TID` with `banned-api` for `requests`, `logging` outside the policy module, database drivers, `langgraph.checkpoint.postgres`); `mypy` strict; `pytest.ini` (asyncio mode auto, markers `sql`, `live`, `load`); `.importlinter` (layer contracts of design 4.3); `Dockerfile` (`python:3.13-slim`, uv, one uvicorn worker with uvloop); `CLAUDE.md`; `docs/decisions/` | None | M-03 (tooling part) |
| P0-02 | Server CI | `.github/workflows/ci.yml`: `uv sync --frozen`, ruff check and format check, mypy, pytest with coverage thresholds, `lint-imports`, generated-file check, catalog check; nightly job for `sql` and opt-in `live` tests | P0-01 | Section 18 |
| P0-03 | Model base | `src/zonic_runtime/models/base.py` (`ZonicModel`: camelCase aliases, `populate_by_name`, `extra` policy per family); `models/markers.py` (`TemplateField`, `SecretRef`, `Column` as `Annotated` metadata); `models/gateway_meta.py` (`@gateway_view`, `@gateway_procedure` storing relation, owner column and INOUT placeholder count); `models/errors.py` (validation error paths); `scripts/gen.py` writing `generated/` (OpenAPI placeholder, JSON Schemas of every exported model) and `--check` mode | P0-01 | M-03 |
| P0-04 | Environment and settings | `config/env.py` (`EnvironmentModel` on pydantic-settings; the only `os.environ` reader); `config/settings.py` (`resources/settings*.json` with environment overlay into `RuntimeSettings`); `config/resources.py` (generic resource loader); the single start-up failure message to stderr and non-zero exit; AST test for the single environment reader | P0-03 | X-08 (environment part), M-02 (part) |
| P0-05 | Resource models and files | Models `NodeRuntimeManifest` (with `retry`, `timeoutSeconds`, `idleTimeoutSeconds`, `joinPolicy`, `streamTokens`, `observation`, `io`), `ModelCatalog` (with `quota`), `McpServerCatalog`, `EgressPolicy`, `ObservabilityConfig`, `HookBindings`, `ErrorCatalog` (code, HTTP status, message key, retryable), `TemplateNamespaces`, `RunStateMachine`, `DialectRules`; files `resources/node-runtime.json` (all 18 types), `models.json`, `mcp-servers.json`, `egress.json`, `observability.json`, `hooks.json`, `error-catalog.json`, `templates.json`, `run-states.json`, `settings.json`, `settings.dev.json`, `settings.prod.json` | P0-03, P0-04 | M-01, M-02 |
| P0-06 | Principle checks | `tests/principles/` AST tests for design rules 1 to 17 and rule 18 (DR-6): no node type, handle, model ID, URL, port, timeout or limit literals in `src/`; no SQL keywords; no driver imports; no `print` or `logging` outside the policy module; no `os.environ` outside `env.py`; no blocking calls (`requests`, `time.sleep`, sync file I/O after start-up); no `except Exception` or bare `except` in `engine/`; no module-level mutable containers in `engine/`, `workflow/`, `run/`; no `.ainvoke`/`.astream` on compiled graphs in `engine/kinds/` except through `invoke_child`; `.importlinter` contract that `engine.kinds` may not import `workflow.blueprint.analysis`; `scripts/catalog_check.py` with `tests/catalog.yaml` (design catalog, Section 2 additions, F-01 to F-18) | P0-01 | X-08 |
| P0-07 | Gateway client | `ports/gateway.py` (Protocol); `integrations/gateway/http_gateway.py` (httpx `AsyncClient`; `view`, `procedure`, `function`, `batch`, `fetch_since`; JSON text arguments; trailing INOUT nulls from `@gateway_procedure`; read retries on 503 and transport errors with backoff; procedures never retried except those marked idempotent; error translation to the catalog, `owner_mismatch` to not found); `tests/fakes/in_memory_gateway.py` (view filter semantics, procedure contracts, batch modes, `SKIP LOCKED` claim semantics); `integrations/gateway/repository.py` (generic repository from row and argument models, owner filter always added); repository test suite parameterized over both gateways | P0-03 | P-03, P-04, M-07 |
| P0-08 | `agent_runtime` SQL and gateway registration | `db/runtime/001_schema.sql` to `006_grants.sql`, `run_all.sql`, `999_drop_all.sql`, `schema_migrations` (8 tables, 9 views, 14 procedures, triggers, indexes including the one-active-run partial index and the queue indexes); DR-8 deltas: `runtime_checkpoint.format`, `langgraph_version`, `kind` in (`pause`, `drain`, `async`); `runtime_run.last_event_seq` (DR-9); `sp_run_release` keeping drained checkpoints; `sp_run_claim` returning `has_checkpoint`; `deploy/pgfoundation.agentruntime.yaml` (datasources `agentruntime` and `agentobservability`, `${env:...}` DSNs, raw SQL off, pool and timeouts); `scripts/init_db.ps1` and `init_db.sh` (Docker `psql`); request D-2 | None | P-01, P-02, P-15 (SQL tests) |
| P0-09 | `agent_observability` SQL | `db/observability/001_schema_create.sql` to `006_grants.sql`, `run_all.sql`, `999_drop_all.sql` (5 tables, monthly partitions, 6 views, 2 materialized views, 6 procedures including idempotent `sp_obs_ingest`) | P0-08 | O-02 to O-04, O-07 (SQL tests) |
| P0-10 | LangGraph verification spikes | `spikes/` (deleted after M0), one script and a short report per spike SP-1 to SP-10 of design 20.1: (1) `Command` routing joins, same-superstep double trigger, `defer` with `Command`, superstep overhead with `durability="exit"`, installed default recursion limit (DR-13); (2) a minimal `TurnSaver` with interrupts inside a child graph and inside a `create_agent` graph invoked in nodes, resumed in a new process from a serialized bundle; `error_handler` signature with `Runtime`; (3) `astream_events(version="v3")` tokens, tools and custom events from graphs invoked inside nodes, with `context`, `durability` and `control`; namespaces (DR-11); (4) Langfuse v5 `CallbackHandler` parenting under `propagate_attributes`, observation typing, enrichment by unwrapping `Command` (DR-5), export to an in-memory span exporter on an isolated provider; (5) `ChatBedrockConverse` async behavior under 150 concurrent fake-latency streams (threads, pool); (6) Swarm handoffs with `Command.PARENT` and an interrupt inside a member; (7) `MCPAdapter` with an in-process FastMCP server, pooling, cancellation; (8) resuming a subset of pending interrupts; (9) node name constraints; (10) `RunControl` drain and resume in another process. Decision records in `docs/decisions/` with the pins (D-4) and any fallback (D-5) | P0-01, P0-11 | Spike reports |
| P0-11 | `ScriptedChatModel` and core fakes | `tests/fakes/scripted_chat_model.py` (a LangChain `BaseChatModel`: scripted turns of text chunks, tool calls, structured outputs, errors, delays and usage; `astream` and `ainvoke`; records every request for assertions; `bind_tools` support); `tests/fakes/clock.py`, `ids.py`; self-tests | P0-01 | Self-tests |

**Checkpoint M0.** CI green with every principle check active; both schemas apply to `runtime_test`; the repository suite passes on both gateways; every spike has a decision record, and every failed spike has a decided fallback (D-5) before P1-09 starts.

---

## 9. Phase 1: Workflow Model and Compiler

**Goal.** Turn any builder document of either dialect into a compiled LangGraph `StateGraph` and run it with test kinds: document and node models, normalizer, loader, blueprint, templates, node input and output, run state and context, the compiler with `Command` routing and barrier joins, the node template with error policies, loops and limits.

| ID | Task | Files and changes | Depends on | Proves |
|---|---|---|---|---|
| P1-01 | Document models and fixtures | `models/document/` (`WorkflowDocument`, `WorkflowNode`, `WorkflowEdge`, `VariableDefinition`, `SystemParam`; unknown keys kept and reported); `tests/fixtures/workflows/builder/` vendored per Section 20.1 with `SOURCE.json`; `tests/fixtures/workflows/synthetic/` (`external_agent`, each routing pattern, parallel fan-in of unequal lengths, nested loops, a supervisor loop, an approval inside a loop, a sub-flow chain, Rule `expression` branches); contract test against the vendored `workflow-document-v2.schema.json` | P0-03 | M-04, F-01, F-02 |
| P1-02 | Node configuration and engine record models | `models/nodes/` (`NodeConfigBase` and one model per kind, 17); `models/engine/` (`NodeOutcome`, `NodeRecord`, `StatePatch`, `StateUpdate`, `FlowPatch` (DR-1), `ThreadMessage`, `TimelineEntry`, `DatasetRef`, `Usage`, `InterruptRequest`); `models/blueprint/` (`Blueprint`, `NodeSpec`, `EdgeIndex`, `JoinSpec`, `Component`, `BlueprintWarning`); `TemplateField` and `SecretRef` marks | P1-01 | M-05, M-08 |
| P1-03 | Dialect normalizer | `resources/dialects.json`; `workflow/loader/normalizer.py` (`DocumentNormalizer`, data-driven rules of design 5.2: models, `promptTemplate`, strategies, `structuredOutput`, routing tools and handoff edges to `handoff-<target>`, tool nodes and agent tools to `ToolRef`, node-level `variableUpdates` to `StateUpdate`, rule shapes, `r-else` and `isDefault`, operator aliases, the three `outputMapping` shapes, `outputKey` and `outputVariable`, template aliases, writable `system` keys; the Rule `expression` rule with `parse_simple`; `OUTPUT_MAPPING_MISSING`); a blueprint note per rule fired; `tests/compat/` over every vendored document | P1-01, P1-02 | B-01 to B-11, B-14, B-15, D-12, D-13, F-02, F-16 |
| P1-04 | Loader and catalog | `workflow/loader/` (catalog read from `v_agent_catalog`, export read from `v_agent_export`, version check, layout stripping, revision hash; `CatalogAgentRow`, `AgentExportRow`); document cache by `(agenticWorkflowId, updated_at)` | P0-07, P1-01, P1-03 | D-01, D-02, D-03, B-16, A-01 (data part), F-02 |
| P1-05 | Blueprint | `workflow/blueprint/`: node specs; entry; reachable set; edge index by `(node, handle)` with handles from the vendored builder registry and `node_semantics.json`; name index; fan-in analysis (barrier selection, `joinPolicy` overrides); strongly connected components (Tarjan) for loop IDs; swarm rules SW-1 to SW-12; child references and `SUBFLOW_CYCLE`; node-name escaping (SP-9); warnings; LRU cache; `scripts/sync_builder.py` with a drift check (also refreshes fixtures and `SOURCE.json`, Section 20.7) | P1-02 | D-04 to D-11, D-14, D-15, F-01, F-03, B-01 |
| P1-06 | Templates and conditions | `workflow/templates/resolver.py` (namespaces and aliases from `templates.json`, typed exact tokens, embedded JSON, missing-value policy, `nodes.<name or id>` through the name index, script and tool `result` paths); `conditions.py` (type-aware evaluator, operator aliases); update operations accepting `op` and `operation` | P1-02 | E-11, E-12, N-09, F-04, F-05, B-08, B-09 |
| P1-07 | Node input and output contract | `models/io/` (`PortSpec`, `PortBinding`, `OutputSpec`, `NodeIoSpec`, `SchemaRef`, `NodeInput`, `NodeOutput`, `@payload` and the 12 built-in payload models); `io/schema_registry.py` (versions, `allOf` composition, compiled validators with the `jsonschema` package, cached by hash); `io/port_binder.py`; `io/output_mapper.py`; `workflow/blueprint/template_checker.py` (it needs the template resolver, decision 0003); `io` blocks for all 18 types in `node-runtime.json`; contract test against the vendored builder `outputSchema`s | P1-02, P1-05 | I-01 to I-11, B-10 |
| P1-08 | `RunState`, reducers and `RunContext` | `engine/state.py` (`RunState` `TypedDict`; reducers for every namespace; the `flow` channel with provenance and deterministic conflict resolution, DR-1; `result` with provenance); `engine/patches.py` (`StatePatch` to a LangGraph update; `system` write enforcement, DR-4); `engine/context.py` (`RunContext`, `RunIdentity`, `RunPorts`, `RunPermits`, `RunEmitter`); property tests (hypothesis) that the reducers are deterministic under any order of same-step writes | P1-06, P1-07 | E-10, E-21, E-22, B-06, B-07 |
| P1-09 | Compiler, routing and graph cache | `workflow/compiler/workflow_compiler.py` (`StateGraph(RunState, context_schema=RunContext)`; `set_node_defaults`; `add_node` with `defer`, `retry_policy`, `timeout`, `error_handler`, `destinations`, `metadata`; the single `START` edge; `compile(checkpointer=..., name=...)` by role, DR-3); `engine/routing.py` (handles to `Command(goto=...)` from the edge index, optional handles, `ROUTE_UNRESOLVED`); `workflow/compiler/graph_cache.py` (LRU keyed by workflow, revision, role, manifest version, tool catalog version); a scripted test kind (`tests/kinds/scripted_kind.py`) until P2-02; compiled snapshots of every fixture in `tests/fixtures/compiled/` (node set, `defer` flags, policies, destinations); property tests against a test-only reference interpreter of the builder's routing semantics | P0-10, P1-05, P1-08 | E-01 to E-05, E-20, G-01 to G-04, G-07, G-09 to G-13, D-09, F-15, B-01 |
| P1-10 | Node template, kind registry and error policy | `engine/node_template.py` (`WorkflowNodeRunner` and `NodeContext`, the 11 steps of design 8.1, events through `runtime.stream_writer`); `engine/kinds/base.py` (`NodeKind` protocol: `execute`, `interrupts_first`, `holds_permit`); `engine/kinds/registry.py` (manifest kinds to classes); `engine/error_policy.py` (`ErrorPolicyHandler` for `fail_run`, `continue`, `route`; `RetryPolicy` with `retry_on` by retryable catalog code; `TimeoutPolicy` from the manifest; `NodeTimeoutError` to `NODE_TIMEOUT`); `engine/child.py` (`invoke_child`, DR-6); conformance suite `tests/conformance/` (C-01 to C-16), each kind run in a compiled one-node graph and a join graph | P1-07, P1-09 | C-01 to C-16, E-13 to E-16, E-18, G-14 |
| P1-11 | Loops and limits | Visit and step limits in `NodeContext` (snapshot plus one, DR-7); `recursion_limit` from settings and document limits up to `maxLimits`; `GraphRecursionError` to `LOOP_LIMIT_EXCEEDED`; `loop.iteration` events and loop IDs from the blueprint's components; tests on the 8 platform loop documents (including the 14-node loop with 6 entries) and the synthetic loops, with scripted test kinds | P1-10 | E-06 to E-09, F-06 |

**Checkpoint M1.** Every vendored builder and platform document loads, normalizes and compiles to a `StateGraph` matching its committed snapshot (F-15, F-16); with test kinds, joins after exclusive branches run once and untaken branches are never scheduled (E-03); joins after unequal parallel branches run once as barriers (E-04); loops, including the 6-entry loop, run and stop at the limits (E-07 to E-09).

---

## 10. Phase 2: Execution, State and API

**Goal.** Run compiled workflows end to end: run driver and event stream, agent layer and core node kinds, the FastAPI app with stream and sync modes, `TurnSaver` and per-turn persistence through the gateway, run executor and governors, hooks, Langfuse tracing in PostgreSQL, and the builder scenarios.

| ID | Task | Files and changes | Depends on | Proves |
|---|---|---|---|---|
| P2-01 | Run driver and event stream | `models/events/` (one model per event type); `engine/streaming/transformer.py` (`RunEventTransformer`, a `StreamTransformer` with the required stream modes; namespace mapping, DR-11); `engine/streaming/v2_mapper.py` (the fallback on `astream(..., subgraphs=True, version="v2")`, behind the same interface); `run/driver.py` (`astream_events` with `config`, `context`, `durability`, `control`; deadline with `asyncio.timeout`; exception mapping to `cancelled`, `timed_out`, `LOOP_LIMIT_EXCEEDED`, `NODE_TIMEOUT` or the recorded code; result from the Output kind or the fallback chain; `seq` continuing from `last_event_seq`, DR-9); `run/events/` (bus, per-run replay buffer with retention, summarizer with size caps) | P1-10 | S-02, S-03, S-05, S-11, S-12, S-13, E-17, R-06 |
| P2-02 | Agent layer and core node kinds | `engine/agents/model_factory.py` (`ModelRef` through `models.json` to `init_chat_model` with `bedrock_converse`, `azure_openai`, `anthropic`, `openai`; parameters bounded by the catalog; `nostream` tagging); `agent_factory.py` (`create_agent` with `system_prompt`, `response_format`, `state_schema=ZonicAgentState`, `context_schema=RunContext`, `name`; built on first use and cached with the compiled graph); `middleware/` (`HandoffMiddleware`, `UsageMiddleware`, `ModelCallLimitMiddleware` mapping for `maxIterations`); handoff tool and platform routing tools with `toolResults`; structured-output adapter (`ProviderStrategy` or `ToolStrategy`, adding `title` and `description`); history port with `trim_messages`; kinds `entry`, `llm`, `agent`, `router`, `variable`, `output`, `ignore`; conversion of agent messages into `turn_messages` | P0-11, P1-03, P1-10 | N-01 to N-08, N-12, N-19, M-09, B-02 to B-04, S-09, D-13 |
| P2-03 | API, use cases, app factory and test client | `main.py` (`create_app(deps)`, lifespan: pools, gateway check, graph cache warm-up option; interactive API docs only when development settings enable them); `api/deps.py`, `auth.py` (PyJWT with a cached JWKS client, issuer, audience, claims from settings), owner scope, `errors.py` (envelope, catalog), request ID, CORS, rate limit (token bucket per user and tenant); `application/start_run.py` (stream and sync); routes catalog, sessions, runs, events (SSE writer with heartbeat, `Last-Event-ID`, coalescing), meta, health; the published client contract in `generated/` (`openapi.json`, `schemas/events/`, `schemas/content/`, `schemas/interrupts/`) with a snapshot test; `scripts/run_client.py` (the developer test client of design Section 3.6: test token or OIDC device login, start a run, print the event stream, answer interrupts from the terminal, page a dataset), grown by later tasks as their endpoints land | P1-04, P2-01 | A-01 to A-07, A-09 to A-12, A-14, A-15, S-01, S-06 to S-08, S-10, M-06 (API part) |
| P2-04 | `TurnSaver` and per-turn persistence | `engine/persistence/turn_saver.py` (`BaseCheckpointSaver` over per-thread buffers keyed by namespace; `attach` and `detach`; `JsonPlusSerializer`, optional `EncryptedSerializer`, Q10); `engine/persistence/codec.py` (`CheckpointBundle` to zstd, base64 and back, with `format` and `langgraph_version`); CI job running `langgraph-checkpoint-conformance` against `TurnSaver`; repositories for sessions, messages, runs, session state, checkpoints; `run/state/turn_store.py` (`TurnStateStore`: the `sequential` start batch, `TurnSave`, idempotent retries until the save deadline, `STATE_UNAVAILABLE`, `run.completed` only after the save, `last_event_seq`); `history_reader.py` (`fetch-since`); declared session variables; compact node timeline; `GET /v1/runs/{id}/nodes`; one active run per session | P0-07, P0-08, P1-10, P2-03 | P-05 to P-09, P-11, P-13, E-19, A-07, A-08 (sync part) |
| P2-05 | Run executor and governors | `run/executor/` (`RunExecutor` admission per process, tenant, user and session; `429` with `Retry-After`; run-wide node permit pool with container release); `engine/agents/governor.py` (`GovernedChatModel`: in-flight cap and token buckets per model ID from `quota`, queue deadline, `MODEL_BUSY`) and `middleware/governor.py`, `middleware/tool_permits.py`; `integrations/http/pools.py` (one `httpx.AsyncClient` per upstream, one shared boto3 Bedrock client per region with `max_pool_connections`, bounded executors per provider per SP-5); token coalescing and subscriber back-pressure; `runtime/pressure.py` (event-loop lag, RSS, active runs; `503 OVERLOADED`, readiness `degraded`); `runtime/metrics.py` (`/metrics` on the internal port); `runtime/process_pool.py` | P2-01, P2-02, P2-03 | Q-01 to Q-09, Q-16, R-02, R-04 |
| P2-06 | Hook framework and core hook types | `models/hooks/`; `hooks/registry.py`, `resolver.py` (global, workflow and node sources, matching, `when`, order, `overridable`, `documentAllowedTypes`, cached per blueprint), `runner.py` (blocking and asynchronous, timeouts, failure policies, allowed actions per point); hook points in the template (`beforeNode`, `afterNode`), `ErrorPolicyHandler` (`onNodeError`) and the driver (`beforeRun`, `afterRun`, `onRunError`); `middleware/hooks.py` (`HookMiddleware` on `wrap_model_call` and `wrap_tool_call`); hook types `setVariables`, `transform`, `validate`, `retry`, `fallback` | P1-10, P2-02 | K-01 to K-07, K-09, K-10, K-12, K-15, K-16 |
| P2-07 | Langfuse tracer | `integrations/langfuse/provider.py` (isolated tracer provider, `BatchSpanProcessor`, `Langfuse(tracer_provider=..., should_export_span=..., mask_otel_spans=..., sample_rate=...)`); `callback_handler.py` (`ZonicLangfuseCallbackHandler`: observation types from the manifest, node metadata, enrichment on chain end by unwrapping `Command`, error-handler attribution, DR-5); `observation_bridge.py`; `masker.py`; `propagate_attributes` around each run; `observability/logging_policy.py`; `NoopTracer`; in-memory span exporter fixture | P1-10, P2-01 | L-01 to L-05, L-07, L-08, L-10, L-12, L-13, L-16, L-17, X-06 |
| P2-08 | Gateway span exporter | `integrations/langfuse/gateway_exporter.py` (OpenTelemetry `SpanExporter`, batches split by count and bytes, idempotent retry, bounded queue, drop reporting, flush on shutdown); `record_mapper.py` (Langfuse attributes and `gen_ai.*` attributes, rules in `observability.json`); models `ObsTraceRecord`, `ObsObservationRecord`, `ObsScoreRecord` | P0-09, P2-07 | O-01 to O-06, L-11 |
| P2-09 | Builder scenarios | `tests/scenarios/builder/`: one scenario per vendored workflow in both dialects (Section 20.4) with a `ScriptedChatModel` script per turn, fake tool, MCP and HTTP responses, and a golden event stream (syrupy; volatile fields removed); multi-turn scenarios for `agent.json` and the sub-agents' loops; a coverage test that fails when a vendored workflow has no scenario or a node type no fixture; Swarm and interrupt scenarios added by P4-01 and P4-02 | P1-03, P1-11, P2-02, P2-03 | F-07, F-08, F-10, F-17, F-18, B-12, B-13 |

**Checkpoint M2.** Every builder fixture without tools runs in stream and sync modes against the scripted model (F-07, F-08); the platform workflows run their multi-turn scripts (F-17, F-18); a turn makes two gateway calls and `TurnSaver` none (P-05); `TurnSaver` passes the conformance suite (P-07); each run's trace appears in `agent_observability` on the test branch; group Q mechanisms pass with fakes; `scripts/run_client.py` streams a builder fixture end to end against the app (A-15); the UI owner's first review of `generated/` is done (Section 13).

---

## 11. Phase 3: Integrations and Data

**Goal.** Tools through the MCP server, database and HTTP nodes with full results kept as datasets, the remaining node kinds, views and content parts, sub-flows, the remaining hook types, the trace and usage APIs, and recorded responses.

| ID | Task | Files and changes | Depends on | Proves |
|---|---|---|---|---|
| P3-01 | MCP integration | `ports/mcp.py`; `integrations/mcp/pool.py` (`MCPAdapter` clients per server from `mcp-servers.json`, listed at start-up, tools-changed handling bumping the tool catalog version, closed on shutdown; `langchain-mcp-adapters` fallback per SP-7); `integrations/mcp/execute.py` (the `execute` tool client, envelope parsing, flow updates); `tests/fakes/mcp_server.py` (in-process FastMCP server answering from the P3-12 recordings, failing the test on an unrecorded call) | P2-02 | T-01, T-02, T-06, T-08, G-08 |
| P3-02 | Tool factory | `engine/agents/tool_factory.py` (MCP tools; builder `database`, `api`, `function` rows as structured tools over `execute`; built-in allow list; `agentcore_gateway`; `secretRef` resolution; owner-bound access through `ToolRuntime.context`); idempotency key `runId:nodeId:visit:callIndex` (DR-2) in `ToolPermitMiddleware` | P3-01 | N-05 (tools part), T-03 to T-05, T-07, T-09 |
| P3-03 | Egress guard and HTTP node | `integrations/egress/` (scheme, host and port allow list; private, loopback and link-local blocking after DNS resolution; redirects off; URL encoding of templated values); kind `http` (list headers, body, timeout, manifest retry for allowed methods) | P1-10 | N-14, X-02, X-03 |
| P3-04 | Database node | Kind `mcp_execute` for `databaseType` `mssql` and `postgresql` (connection name passed, never a connection string, D-7); `DatasetRef` bound to session and owner; route `/v1/datasets/{ref}` with paging, sorting, filters | P3-02 | N-13, X-01 (datasets part) |
| P3-05 | Datasets | `run/state/dataset_store.py` with the cache-service adapter (MCP `execute` with `saveToCache` and TTL for database work; `PUT keys/<key>` for HTTP responses) and the PostgreSQL adapter (`sp_dataset_save` chunks in a `transactional` turn-end batch); inline previews; template resolution of references up to `maxResolvedBytes`; per-type store from the manifest | P2-04, P3-01, P3-03, P3-04 | P-12, B-11 |
| P3-06 | Tool, external agent and guardrail kinds | Kinds `tool` (direct invocation, `pass` and `fail`), `external_agent` (A2A client or HTTP through egress), `guardrail` (Bedrock Guardrails or a policy model; `pass`, `fail`, `warn`) | P3-02, P3-03 | N-10, N-15, N-16, B-05 |
| P3-07 | Script node | `ports/sandbox.py`; Piston and disabled adapters; kind `sandbox` behind `features.scriptNode` | P1-10 | N-17 |
| P3-08 | View node and content parts | `models/content/` (discriminated `ContentPart` subclasses); kind `view` (grid, chart, table from `templateConfig` with `cacheDataKey`; ECharts option allow list; the `html` part with escaped values and server-side sanitizing) | P1-10 | N-18, M-06, B-11 |
| P3-09 | Sub-flows | Kind `subflow`: child document through the loader and blueprint cache, child graph from the cache in the `child` role (DR-3), invoked through `invoke_child` (DR-6); `input` mapping, `inheritFlow` policy, output and variable mapping, visibility, depth, `SUBFLOW_CYCLE`; nested `path` on events and observations | P2-02 | N-11, G-05, S-04, L-06 (sub-flow part) |
| P3-10 | Remaining hook types | `mask`, `guardrail`, `cache` (cache service, scoped keys), `rateLimit`, `webhook` (allow-listed, masked, asynchronous by default), `mcpTool`, `annotate`; hook observations | P2-06, P3-01, P3-03 | K-08, K-14 |
| P3-11 | Trace and usage APIs, feedback, maintenance | Routes `/v1/runs/{id}/trace`, `/trace-link`, `/v1/observability/usage`, `/v1/messages/{id}/feedback`; maintenance schedule calling `sp_obs_partition_ensure`, `sp_obs_retention_purge`, `sp_obs_refresh_rollups`, `sp_retention_purge` (interrupt expiry included) | P2-04, P2-08 | L-14, L-15, O-07, O-08 |
| P3-12 | Recorded responses | `scripts/record_fixtures.py` (opt-in, live): database tools and DB Query nodes of the open-data fixtures against `sample_open_data` as `sample_open_data_reader` with Section 20.4's parameters; public endpoints through the egress guard; writes `tests/fixtures/recordings/<workflow>/<tool>.<parameter hash>.json`; `FakeEgress` replays HTTP recordings | P3-01, P3-03 | F-09 |

**Checkpoint M3.** All builder fixtures, including tools and sub-flows, run against the fakes with recorded responses (F-09, F-10); database and HTTP results are kept in full in both stores (P-12); one open-data fixture runs against the real MCP server locally once D-7 is settled.

---

## 12. Phase 4: Swarm, Interrupts and Durable Asynchronous Runs

**Goal.** Swarm containers as handoff subgraphs, human input, approvals and tool approvals as LangGraph interrupts, the `async` mode with cancellation, and durable `async` runs with claims, leases, fencing, drain and handover.

| ID | Task | Files and changes | Depends on | Proves |
|---|---|---|---|---|
| P4-01 | Swarm subgraph | `engine/agents/swarm/builder.py` (`SwarmState`; one member node per member, built by `AgentFactory`, LLM members as agents without tools; `transfer_to_<member>` tools returning `Command(goto=..., graph=Command.PARENT)` with the paired handoff messages; `START` routing to the entry point; end when a member answers without a handoff; `TimeoutPolicy` per member; compiled once per swarm spec); `handoff_tools.py` (step limit, repetitive-handoff detection); `member_context.py` (`shared` and `handoff` context modes); kind `multi_agent` with pattern `swarm` (task port, `asyncio.timeout`, error codes, result mapping, usage aggregation, swarm events); `beforeMemberCall` and `onHandoff` hook points | P0-10, P2-02, P2-06, P3-02 | W-01 to W-13, G-06, L-06, F-11, F-12, K-13 |
| P4-02 | Interrupts and resume | Kind `interrupt` (prompt port bound before `interrupt()`, DR-10; answer models generated from the node configuration with `create_model`; `responseVariable`; `approved` and `rejected`); `run/state/checkpoint_store.py` (pause through one `sp_interrupt_create` with the detached `TurnSaver` bundle, the documents, the interrupts and the turn's messages); `application/resume_run.py` (answer validation against the stored schema; graph from the stored revision; restored buffer; `Command(resume={id: answer})`; subset rule per SP-8); routes `GET` and `POST /v1/runs/{id}/interrupts...`; expiry with `onExpiry`; `onInterrupt` and `onResume` hook points; interrupt scenarios for Approval Flow and the Facility Operations Swarm | P2-04, P2-06, P3-09 | H-01 to H-05, H-07 to H-11, N-20, L-09, F-13, M-10 |
| P4-03 | Tool approvals and the approval hook | `HumanInTheLoopMiddleware(interrupt_mode="per_call")` for tools in `settings.tools.requireApproval`; decisions approve, edit, reject mapped to the interrupt answer model; hook type `approval` (template step 2 and `wrap_tool_call`) | P4-02 | H-06, K-11 |
| P4-04 | Asynchronous mode and cancellation | Mode `async` (`202` with links; `ETag` and `Retry-After` on `GET /v1/runs/{id}`); `sync` falling back to `202` after `syncWaitSeconds`; `POST /v1/runs/{id}/cancel` for running, queued and waiting runs (task cancellation, `sp_run_finish` with `cancelled`, interrupts cancelled) | P2-03, P4-02 | A-08, A-13, H-12, E-17 (cancel part) |
| P4-05 | Durable asynchronous runs, drain and handover | `run/executor/claimer.py` (`RunClaimer`, adaptive interval, local start when capacity is free, round-robin across tenants); leases with one `sp_run_heartbeat` per instance; `attempt` fencing on every write; `RunControl` per run and graceful drain (`request_drain`, `GraphDrained`, `sp_checkpoint_save` with `kind = 'drain'`, `sp_run_release`); resume of claimed runs with a checkpoint through `astream_events(None, config)`; write-behind checkpoints for `async` runs (`durability="async"`, coalesced flush at `checkpointIntervalSeconds`); `settings.runs.onInstanceLoss`; signed completion callbacks through egress; cross-instance SSE proxy (`/internal/runs/{id}/events`) | P2-04, P2-05, P4-02, P4-04 | Q-10 to Q-15, R-03, P-10, P-14 |

**Checkpoint M4.** The Customer Support Swarm runs with scripted handoffs, and the Facility Operations Swarm runs its full path (Section 20.5) with an LLM member, an approval that pauses and resumes across requests, and the rejected branch through Human Input (F-11 to F-13); an interrupt inside a sub-flow and one inside a loop resume without repeating completed nodes (H-07, H-08, H-11); an `async` run drained on one instance finishes on another without repeating completed supersteps (R-03); fencing rejects the old owner (Q-13).

---

## 13. Deferred: Runtime UI

The runtime UI is not part of this plan (design Section 3.6). It will be planned separately once the backend reaches checkpoint M2, when the contract is stable enough to build against.

| Deferred from the Strands plan | What this plan delivers instead |
|---|---|
| U0-01 to U0-04: UI scaffold, authentication, API client generation, shell | The published contract in `generated/` (OpenAPI, event, content-part and interrupt-answer schemas), snapshot-tested (A-10, A-14); OIDC on the server (P2-03) |
| U5-01 to U5-10: catalog, stream client, chat thread, renderers, session sidebar, interrupt forms, run timeline and trace viewer, message actions, accessibility, end-to-end tests | The endpoints behind each of them (catalog, sessions, runs, events, interrupts, datasets, trace, feedback), proved by API tests; `scripts/run_client.py` for interactive runs, interrupts and dataset paging (A-15) |
| U5-11 (optional): the builder's hooks form (Q8) | Hooks bound from `resources/hooks.json` and documents (P2-06, P3-10) |
| Sanity group U (U-01 to U-14) | Group A cases A-10, A-14 and A-15 |

**Hand-over to the UI work.** At checkpoint M2, and again before the dev release (P5-06), the UI owner reviews `generated/` and the test client's flows; contract changes after that review go through a reviewed pull request that updates the snapshot, under `/v1` unless they break compatibility.

---

## 14. Phase 5: Hardening and Release

| ID | Task | Files and changes | Depends on | Proves |
|---|---|---|---|---|
| P5-01 | Security pass | Group X suite; response headers; request size limits; CORS allow list from settings; dependency audit (`pip-audit`) | Phases 1 to 4 | X-01 to X-08 |
| P5-02 | Performance validation | `tests/load/` (k6 on the target 1 vCPU, 2 GB task with one worker: run start, compile on miss, template and superstep overhead, token latency and coalescing, 150 concurrent streaming runs, event-loop lag, memory per run, wide Rule fan-outs, model governor under one quota, Bedrock executor saturation, `async` backlog and drain across two instances); settings tuned (`maxConcurrentRuns`, `maxConcurrencyPerRun`, quotas, executor threads, claim intervals, pressure thresholds); a report against design 11.1 | P2-05, P4-05 | R-01 to R-06, Q-01 to Q-05, Q-16 (budgets on the target task) |
| P5-03 | Live sanity | `tests/live/` with `LIVE_*` variables: Bedrock models, the real MCP server, the gateway with both schemas on `runtime_test` with cleanup; the open-data FSH agents, the Facility Operations Swarm and (once Q3 allows) the platform workflows on Azure OpenAI, asserting paths and result shapes | Phases 1 to 4 | V-01 to V-06, F-14 |
| P5-04 | Deployment to dev | Infrastructure as code per D-3 (task definition with one worker, ALB with SSE idle timeout, Secrets Manager entries for the gateway key, model credentials and the checkpoint AES key, environment overlays); schemas applied to `neondb_dev`; maintenance schedule | P5-01 | V-06 on dev |
| P5-05 | Documentation | READMEs; runbooks (deploy, rotate keys, add a node type, add a hook type, add a model, upgrade LangGraph with waiting runs, use the test client); decision records; extension recipes from design 17; a client integration guide for the deferred UI (contract files, auth, streaming and resume, interrupts, datasets) | Phases 1 to 4 | Review |
| P5-06 | Release gates | MCP server F1 to F3 fixed (D-6); full catalog green; the contract review by the UI owner (Section 13) done; the backend released to dev for API use and the test client; the end-user switch left to Q11 | P5-01 to P5-04 | All groups |

**Checkpoint M5.** Groups X, R, V and O pass on dev; the budgets hold on the target task (P5-02 report); the catalog check reports every case covered; D-6 closed; the test client completes a full run, an interrupt and a dataset page against dev.

---

## 15. Implementation Notes

Short sketches of the less obvious parts, in task order. They are illustrative; the design, Section 2 and the task rows are the specification.

### 15.1 Model base and gateway metadata (P0-03, P0-07)

```python
# models/base.py
class ZonicModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True)


# models/gateway/run_row.py
@gateway_view(schema="agent_runtime", name="v_run", owner_column="owner_sub")
class RunRow(ZonicModel):
    run_id: Annotated[UUID, Column("run_id")]
    session_id: Annotated[UUID, Column("session_id")]
    status: Annotated[RunStatus, Column("status")]  # statuses from run-states.json
    last_event_seq: Annotated[int, Column("last_event_seq")]


@gateway_procedure(schema="agent_runtime", name="sp_run_finish", placeholders=0, idempotent=True)
class RunFinishArgs(ZonicModel):
    run_id: UUID
    attempt: int
    save: TurnSave  # sent as JSON text, positional
```

The generic repository reads the metadata: the view name, the selected columns (from `Column`), the owner filter, the positional argument order (field order), the INOUT placeholder count, and whether a procedure may be retried. No module builds a payload by hand.

### 15.2 A dialect rule (P1-03)

```json
{
  "id": "rule.operator.equals",
  "match": { "path": "nodes[type=rule].config.rules[*].conditions[*].operator", "equals": "equals" },
  "set": { "op": "eq" },
  "remove": ["operator"],
  "note": "platform operator alias"
}
```

`DocumentNormalizer` applies rules in file order to a copy of the document, records each fired rule as a blueprint note, and never changes the stored document. Case B-15 fails when a fixture holds a field, handle or template namespace that no rule and no model covers.

### 15.3 The `flow` channel with provenance (P1-08, DR-1)

```python
# engine/state.py
class FlowChannel(TypedDict):
    values: dict[str, Any]
    provenance: dict[str, tuple[int, str]]  # key -> (superstep, writer node ID)
    conflicts: list[dict[str, Any]]


def merge_flow(left: FlowChannel, right: FlowPatchDict) -> FlowChannel:
    out = {
        "values": dict(left["values"]),
        "provenance": dict(left["provenance"]),
        "conflicts": list(left["conflicts"]),
    }
    step, writer = right["step"], right["writer"]
    for key, value in right["set"].items():
        prior = out["provenance"].get(key)
        if prior and prior[0] == step and prior[1] != writer:
            out["conflicts"].append({"key": key, "step": step, "writers": sorted([prior[1], writer])})
            if writer > prior[1]:  # deterministic: lowest node ID wins, whatever the arrival order
                continue
        deep_set(out["values"], key, value)
        out["provenance"][key] = (step, writer)
    return out
```

The template reads `step` from `config["metadata"]["langgraph_step"]`. The resolver reads only `values`; the driver turns new `conflicts` entries into `state.conflict` warnings. A hypothesis test applies the same writes in every order and asserts the same channel value.

### 15.4 Compiler and routing (P1-09)

```python
# engine/routing.py
def route(spec: NodeSpec, activate: list[str], index: EdgeIndex) -> list[str]:
    handles = spec.all_handles if activate == ["*"] else activate
    targets: list[str] = []
    for handle in handles:
        edges = index.targets(spec.node_id, handle)
        if not edges and not spec.handle_optional(handle):
            raise ZonicError("ROUTE_UNRESOLVED", node=spec.node_id, handle=handle)
        targets.extend(t.graph_name for t in edges)
    return list(dict.fromkeys(targets))  # stable, no duplicates
```

The compiler adds `START -> entry` and nothing else as a static edge; every node's `destinations` are the targets of all its handles, so `get_graph()` draws the document's topology (G-03). Compiled snapshots serialize `graph.get_graph()` plus each node's `defer`, retry, timeout and error-handler flags.

### 15.5 Node template and error policy (P1-10)

```python
# engine/error_policy.py
class ErrorPolicyHandler:
    def __init__(self, spec: NodeSpec) -> None:
        self.spec = spec

    async def __call__(self, state: RunState, error: NodeError, runtime: Runtime[RunContext]) -> Command:
        ctx = runtime.context
        decision = await ctx.hooks.on_node_error(self.spec, state, error)  # retry, recover, escalate
        if decision.recovered:
            return Command(
                update=decision.patch, goto=route(self.spec, decision.activate, ctx.blueprint.edges)
            )
        code = error_code(error.error)  # catalog code from ZonicError or the mapping
        record = failure_record(self.spec, code)
        match self.spec.error_policy:
            case "continue":
                return Command(update=record, goto=route(self.spec, ["default"], ctx.blueprint.edges))
            case "route":
                return Command(update=record, goto=route(self.spec, ["error"], ctx.blueprint.edges))
            case _:
                raise RunFailed(code, update=record)
```

If SP-2 shows that `Runtime` is not injected into error handlers, the handler takes `config` and finds `RunContext` in the run registry keyed by `config["configurable"]["thread_id"]` (D-5).

### 15.6 `invoke_child` (P1-10, DR-6)

```python
# engine/child.py
async def invoke_child(
    graph: CompiledStateGraph,
    child_input: Mapping[str, Any],
    runtime: Runtime[RunContext],
    config: RunnableConfig,
) -> dict[str, Any]:
    child_config = ensure_child_config(config, recursion_limit=runtime.context.settings.child_supersteps)
    async with runtime.context.permits.released():  # containers release their permit
        return await graph.ainvoke(child_input, child_config, context=runtime.context)
```

Callbacks, tags, metadata and the parent task's checkpoint namespace travel in `config`; the run's context and deadline in `context`. The principle check rejects any other `ainvoke` or `astream` on a compiled graph inside `engine/kinds/`.

### 15.7 `TurnSaver` (P2-04)

```python
# engine/persistence/turn_saver.py
class TurnSaver(BaseCheckpointSaver):
    """One buffer per thread (run ID). Never calls the gateway: RunDriver attaches and detaches buffers."""

    def attach(self, run_id: str, bundle: CheckpointBundle | None) -> None: ...
    def detach(
        self, run_id: str
    ) -> CheckpointBundle: ...  # latest checkpoint per namespace with pending writes

    async def aput(self, config, checkpoint, metadata, new_versions):
        buf = self._buffer(config)  # KeyError -> ZonicError("CHECKPOINT_THREAD_UNKNOWN")
        ns = config["configurable"].get("checkpoint_ns", "")
        buf.put(
            ns,
            checkpoint["id"],
            config["configurable"].get("checkpoint_id"),
            self.serde.dumps_typed(checkpoint),
            self.serde.dumps_typed(metadata),
        )
        return {
            "configurable": {
                "thread_id": buf.thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    # aput_writes, aget_tuple (latest and by ID), alist (newest first, before, limit), adelete_thread
```

The buffer keeps only what resume needs: by default the last two checkpoints per namespace (the latest and its parent, for delta-channel safety), configurable; `alist` therefore returns a short history. The conformance suite runs against a `TurnSaver` with an attached empty buffer.

### 15.8 Run driver and event transformer (P2-01)

```python
# run/driver.py
async def drive(self, run: ActiveRun, graph: CompiledStateGraph, payload: dict | Command) -> RunOutcome:
    self._saver.attach(run.id, run.restored_bundle)
    try:
        async with asyncio.timeout(run.deadline.remaining()):
            stream = await graph.astream_events(
                payload,
                config=run.config,
                context=run.context,
                durability=run.durability,
                control=run.control,
                version="v3",
                transformers=[RunEventTransformer.for_run(run)],
            )
            async for event in stream.extensions["zonic"]:
                run.bus.publish(event)  # seq assigned by the bus from last_event_seq
            final = await stream.output
        bundle = self._saver.detach(run.id)
        if stream.interrupted:
            return RunOutcome.paused(stream.interrupts, bundle)
        return RunOutcome.completed(final)
    except GraphDrained:
        return RunOutcome.drained(self._saver.detach(run.id))
    except (RunFailed, GraphRecursionError, NodeTimeoutError, TimeoutError, asyncio.CancelledError) as exc:
        self._saver.detach(run.id)
        return RunOutcome.from_exception(exc)
```

`TurnStateStore` then makes exactly one gateway call per outcome (`sp_run_finish`, `sp_interrupt_create`, or `sp_checkpoint_save` and `sp_run_release`).

### 15.9 Agent factory and middleware (P2-02, P2-05, P2-06)

```python
# engine/agents/agent_factory.py
def build(self, spec: NodeSpec[AgentNodeConfig]) -> CompiledStateGraph:
    cfg = spec.config
    return create_agent(
        model=self._models.governed(cfg.model_ref),
        tools=self._tools.for_node(spec),  # MCP, configured, built-in, handoff, routing
        system_prompt=None,  # resolved per visit through the prompt port
        middleware=[
            HookMiddleware(spec),
            GovernorMiddleware(),
            ToolPermitMiddleware(spec),
            *self._retry_middleware(spec),
            *self._approval_middleware(spec),
            ModelCallLimitMiddleware(run_limit=cfg.max_iterations, exit_behavior="end"),
            HandoffMiddleware(spec),
            UsageMiddleware(),
        ],
        response_format=self._structured.strategy_for(spec),
        state_schema=ZonicAgentState,
        context_schema=RunContext,
        name=spec.graph_name,
    )
```

The `agent` kind passes the resolved system prompt and the history as the first messages of the agent's input, invokes it with `invoke_child`, and maps the final state into `zonic.agent-result` and `turn_messages`.

### 15.10 App factory and SSE (P2-03)

```python
# main.py
def create_app(deps: AppDeps | None = None) -> FastAPI:
    deps = deps or AppDeps.from_settings(load_settings())
    app = FastAPI(lifespan=deps.lifespan, title="Zonic Agent Runtime", version=deps.version)
    app.state.deps = deps
    install_error_handlers(app, deps.errors)
    for router in api_routers():
        app.include_router(router, prefix="/v1")
    return app
```

The SSE writer is a `StreamingResponse` over an async generator that reads the run's replay buffer from `Last-Event-ID`, coalesces text deltas, emits heartbeats, respects `await request.is_disconnected()`, and never cancels the run on disconnect.

### 15.11 Saving a turn (P2-04)

```python
# run/state/turn_store.py
async def finish(self, run: ActiveRun, final: RunState) -> None:
    save = TurnSave.from_state(
        run, final, last_event_seq=run.bus.last_seq
    )  # messages, variables, timeline, datasets
    calls = self._dataset_batch(save) + [
        procedure_call(RunFinishArgs(run_id=run.id, attempt=run.attempt, save=save))
    ]
    await retry_until(
        self._settings.state.save_deadline,
        lambda: (
            self._gateway.batch(calls, mode="transactional")
            if len(calls) > 1
            else self._gateway.call(calls[0])
        ),
    )
```

### 15.12 Langfuse enrichment (P2-07, DR-5)

```python
# integrations/langfuse/callback_handler.py
class ZonicLangfuseCallbackHandler(CallbackHandler):
    def on_chain_start(self, serialized, inputs, *, run_id, metadata=None, **kw):
        spec = self._bridge.node_for(metadata)  # langgraph_node -> NodeSpec, or None
        super().on_chain_start(
            serialized, inputs, run_id=run_id, metadata=self._bridge.tag(spec, metadata), **kw
        )

    def on_chain_end(self, outputs, *, run_id, **kw):
        update = outputs.update if isinstance(outputs, Command) else outputs
        self._bridge.enrich(run_id, node_record_from(update))  # masked output, level, status message
        super().on_chain_end(outputs, run_id=run_id, **kw)
```

The exact override points follow the SDK version pinned by SP-4; if they are not stable, the fallback in design Section 12 applies.

### 15.13 Swarm handoff tool (P4-01)

```python
# engine/agents/swarm/handoff_tools.py
def transfer_tool(target: SwarmMember, limits: SwarmLimits) -> BaseTool:
    @tool(f"transfer_to_{target.name}", description=target.description)
    def transfer(message: str, runtime: ToolRuntime[RunContext, SwarmState]) -> Command:
        limits.check(runtime.state, target.name)  # SWARM_STEP_LIMIT, SWARM_REPETITIVE_HANDOFF
        ai = last_ai_message(runtime.state["messages"])
        ack = ToolMessage(content=f"Transferred to {target.name}", tool_call_id=runtime.tool_call_id)
        return Command(
            goto=target.node_name,
            graph=Command.PARENT,
            update={
                "active_member": target.name,
                "step": runtime.state["step"] + 1,
                "handoffs": [handoff_entry(runtime, target, message)],
                "messages": [ai, ack],
            },
        )

    return transfer
```

### 15.14 Pause and resume (P4-02)

```python
# application/resume_run.py
async def resume(self, run_id: UUID, interrupt_id: str, answer: dict, caller: Caller) -> RunHandle:
    pending = await self._interrupts.answer(
        interrupt_id, caller.sub, answer
    )  # validated; sp_interrupt_answer
    if not pending.ready_to_resume:  # SP-8 subset rule
        return RunHandle.waiting(run_id)
    row = await self._checkpoints.load(run_id)  # documents + bundle
    graph = self._graphs.get(row.root_revision, role="root", documents=row.documents)
    run = self._runs.activate(row, restored_bundle=row.bundle, last_event_seq=row.last_event_seq)
    return await self._executor.start(run, graph, Command(resume=pending.answers_by_id))
```

### 15.15 Drain and handover (P4-05)

On `SIGTERM`, the executor stops claiming, calls `control.request_drain("shutdown")` on every active run, and waits up to `settings.shutdown.drainSeconds`. `GraphDrained` gives `RunOutcome.drained(bundle)`. An `async` run's bundle is saved with `sp_checkpoint_save(kind='drain')` and the run released to `queued`; the claiming instance sees `has_checkpoint` and calls `drive(run, graph, None)` with the restored bundle. A `stream` or `sync` run still active at the end of the drain fails with `SHUTDOWN` and a retry action.

### 15.16 Scenario test from a builder workflow (P2-09)

```python
# tests/scenarios/builder/test_fsh_facility_agent.py
@pytest.mark.parametrize("use_db, expected", [(None, PATH_HTTP), (True, PATH_DB)])
async def test_F08_fsh_facility_routes_on_use_db_query(app_with_fakes, use_db, expected, snapshot):
    run = await app_with_fakes.run(
        "fsh-facility-agent",
        input="energy use at red hook",
        flow={"useDBQuery": use_db},
        script=SCRIPTS["handoff_sql"],
    )
    assert run.executed_node_names() == expected  # untaken branch never scheduled
    assert run.gateway_calls() == ["batch:sequential", "sp_run_finish"]
    assert run.golden_events() == snapshot
```

---

## 16. Repository Layout by Phase

The layout follows design Section 18.1 (`src/zonic_runtime/...`).

| Folder | Phase |
|---|---|
| `config/`, `models/base.py`, `models/markers.py`, `models/gateway_meta.py`, `models/resources/`, `integrations/gateway/`, `ports/`, `db/runtime/`, `db/observability/`, `deploy/`, `resources/`, `generated/`, `scripts/`, `tests/principles/`, `tests/fakes/` (gateway, model, clock, IDs) | 0 |
| `models/document/`, `models/nodes/`, `models/engine/`, `models/blueprint/`, `models/io/`, `workflow/loader/`, `workflow/blueprint/`, `workflow/compiler/`, `workflow/templates/`, `io/`, `engine/state.py`, `engine/patches.py`, `engine/context.py`, `engine/routing.py`, `engine/node_template.py`, `engine/error_policy.py`, `engine/child.py`, `engine/kinds/base.py`, `engine/kinds/registry.py`, `tests/fixtures/workflows/`, `tests/fixtures/compiled/`, `tests/compat/`, `tests/conformance/`, `tests/kinds/` | 1 |
| `models/events/`, `models/hooks/`, `models/observability/`, `engine/streaming/`, `engine/agents/` (factories, middleware, governor), core kinds, `engine/persistence/`, `run/driver.py`, `run/events/`, `run/executor/` (admission, permits), `run/state/` (turn store, history reader), `hooks/`, `api/`, `application/start_run.py`, `main.py`, `integrations/langfuse/`, `integrations/http/`, `observability/`, `runtime/`, `tests/scenarios/` | 2 |
| `integrations/mcp/`, `integrations/egress/`, `integrations/sandbox/`, `integrations/a2a/`, `models/content/`, remaining kinds including `subflow`, `run/state/dataset_store.py`, `tests/fixtures/recordings/` | 3 |
| `engine/agents/swarm/`, `engine/kinds/multi_agent/`, `engine/kinds/interrupt.py`, `run/state/checkpoint_store.py`, `application/resume_run.py`, `application/cancel_run.py`, `run/executor/claimer.py` and drain, `run/callbacks.py`, `run/events/proxy.py` | 4 |
| `tests/load/`, `tests/live/`, `infra/`, `docs/runbooks/`, `docs/client-integration.md` | 5 |

---

## 17. Python Dependencies

Pinned per D-4 after P0-10; the table names the packages and why each is there, so nothing is added without a reason.

| Group | Packages | Why |
|---|---|---|
| Runtime core | `langgraph` (1.2.x, at least 1.2.12), `langgraph-checkpoint` (comes with it), `langchain` with the `mcp` extra (1.4.x), `langchain-aws`, `langchain-openai`, `langchain-anthropic` | Engine, agents, models, MCP |
| Web | `fastapi`, `uvicorn[standard]`, `uvloop` | API and SSE |
| Models and settings | `pydantic` 2, `pydantic-settings`, `jsonschema` | Class models, environment, payload schema validation |
| Clients | `httpx`, `boto3` (through `langchain-aws`), `a2a-sdk` (optional, for `external_agent`) | Gateway, MCP, egress, Bedrock |
| Serialization | `orjson`, `zstandard`, `pycryptodome` (for `EncryptedSerializer`) | Fast JSON, checkpoint codec, encryption |
| Auth | `PyJWT[crypto]` | OIDC token verification with JWKS |
| Observability | `langfuse` 5.x, `opentelemetry-sdk` | Tracing and export |
| Metrics | `prometheus-client` | `/metrics` gauges (numbers, not logs) |
| Dev and test | `ruff`, `mypy`, `import-linter`, `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `syrupy`, `respx` (httpx mocking), `fastmcp` (in-process MCP server), `langgraph-checkpoint-conformance`, `pip-audit` | Gates, fakes, conformance |
| Load | k6 (outside Python) | P5-02 |

Not allowed: `psycopg`, `asyncpg`, `pyodbc`, `sqlalchemy`, `langgraph-checkpoint-postgres`, `requests`, logging libraries such as `loguru` or `structlog` (principle checks, P0-06).

---

## 18. Quality Gates

| Gate | Command | When | Blocks |
|---|---|---|---|
| Lint and format | `uv run ruff check . && uv run ruff format --check .` | Every pull request | Merge |
| Types | `uv run mypy src` | Every pull request | Merge |
| Unit, conformance, scenario, API, contract, principle tests | `uv run pytest` | Every pull request | Merge |
| Coverage | `uv run pytest --cov=zonic_runtime --cov-fail-under=90` for `workflow/`, `engine/`, `run/`, `hooks/`, `integrations/langfuse/` | Every pull request | Merge |
| Layer rules | `uv run lint-imports` | Every pull request | Merge |
| Generated files | `uv run python -m scripts.gen --check` (OpenAPI, JSON Schemas, builder registry sync) | Every pull request | Merge |
| Catalog coverage | `uv run python -m scripts.catalog_check` | Every pull request | Merge |
| Checkpointer conformance | `uv run python -m tests.conformance.checkpointer` | Every pull request touching `engine/persistence/` and nightly | Merge |
| SQL tests | `uv run pytest -m sql` (both schemas on `runtime_test` through the gateway) | Nightly and on `db/` changes | Merge for `db/` changes |
| Live sanity | `uv run pytest -m live` with `LIVE_*` variables | Nightly (opt-in) and before release | Release |
| Dependency audit | `uv run pip-audit` | Weekly and before release | Release |
| Client contract | `uv run pytest -k "A10 or A14 or A15"` (OpenAPI and schema snapshots, test client against the app); part of `uv run pytest`, listed for visibility | Every pull request | Merge |
| Load smoke | `k6 run tests/load/smoke.js` | Before release | Release |

---

## 19. Requirement Traceability and Risks

### 19.1 Requirements to tasks

| Design requirement (design Section 2) | Tasks |
|---|---|
| 1. Follow the target architecture | All; Section 2 records the deltas |
| 2. Python LangGraph Graph API, dynamic build | P0-10, P1-05, P1-08, P1-09, P1-10, P1-11, P2-01, P3-09, P4-01 |
| 3. Informed by the LangGraph documentation | P0-10 (spikes and decision records) |
| 4. Decoupled backend and frontend (API-first; UI deferred) | P0-03, P2-01, P2-03, P4-04 |
| 5. Schema design in class models | P0-03, P0-05, P1-01, P1-02, P1-07, P2-01, P3-08 |
| 6. Swarm | P1-05, P4-01 |
| 7. Database only through the gateway | P0-07, P0-08, P0-09, P2-04, P2-08, P4-02 |
| 8. Patterns, low code, no hard-coded values | P0-04, P0-05, P0-06, P1-03, P1-09, P2-06 |
| 9. Extensible node types | P1-02, P1-05, P1-10, P5-05 |
| 10. Sanity testing | P0-06 (catalog), every "Proves" column, P5-02, P5-03 |
| 11. No ad hoc logs; Langfuse in PostgreSQL | P0-06, P2-07, P2-08 |
| 12. Configurable hooks | P2-06, P3-10, P4-01, P4-03 |
| 13. Performance, async and concurrency | P2-01, P2-05, P4-05, P5-02 |
| 14. State and history once per turn | P2-04, P4-02, P4-05 |
| 15. Full data of database and HTTP nodes | P3-04, P3-05 |
| 16. Builder compatibility | P1-01, P1-03, P1-11, P2-09, P3-08 |

| Sanity group | Tasks |
|---|---|
| A, S | P2-01, P2-03, P2-04, P3-09, P4-04 |
| D | P1-01, P1-03, P1-04, P1-05, P1-09 |
| E | P1-06, P1-08, P1-09, P1-10, P1-11, P2-01, P2-04, P4-04 |
| I | P1-07 |
| B | P1-01, P1-03, P1-04, P2-02, P2-09, P3-05, P3-08 |
| G | P1-09, P1-10, P3-09, P4-01 |
| N | P1-06, P2-02, P3-02 to P3-04, P3-06 to P3-09, P4-02 |
| W | P4-01 |
| H | P4-02, P4-03, P4-04 |
| T | P3-01, P3-02 |
| P | P0-07, P0-08, P2-04, P3-05, P4-05 |
| X | P0-04, P0-06, P2-07, P3-03, P3-04, P5-01 |
| R | P2-01, P2-05, P4-05, P5-02 |
| Q | P2-05, P4-05, P5-02 |
| C | P1-10 |
| K | P2-06, P3-10, P4-01, P4-03 |
| M | P0-03, P0-05, P1-01, P1-02, P2-02, P2-03, P3-08 |
| L | P2-07, P2-08, P3-09, P3-11, P4-01, P4-02 |
| O | P0-09, P2-08, P3-11 |
| V | P5-03, P5-04 |
| F (Section 20.3) | P1-01, P1-03, P1-05, P1-06, P1-09, P1-11, P2-09, P3-12, P4-01, P4-02, P5-03 |

### 19.2 Build risks

Risks in the design itself are in design Section 20.3 with their mitigations, which the tasks implement. These are risks to building it.

| ID | Risk | Level | Mitigation |
|---|---|---|---|
| R1 | Gateway caller and datasource registration (D-2) depends on another team | High | Request it in wave 1; `InMemoryGateway` and a local gateway on the test branch until then |
| R2 | MCP server findings F1 to F3 not fixed in time (D-6) | High for release | Release gate P5-06; in-process MCP fake for all other work |
| R3 | Azure OpenAI not approved in time (Q3) | High for the platform workflows | Scripted model in CI; Bedrock for the builder's own workflows |
| R4 | A LangGraph spike fails (SP-1 to SP-3, SP-10 are the critical ones) | Medium | Fallbacks are designed (design 20.1); M0 decides them before P1-09; the fallbacks change at most `engine/streaming/`, `engine/persistence/` or the barrier selection, behind interfaces that tasks already use |
| R5 | LangGraph or LangChain releases change APIs during the build (1.2 features are recent, `langchain.mcp` is beta) | Medium | Pinned minors; upgrades only in their own pull request with the compiler, conformance, scenario and checkpointer suites |
| R6 | `TurnSaver` passes the conformance suite but a LangGraph feature needs history the buffer trimmed | Medium | Keep two checkpoints per namespace by default (Section 15.7); scenario H-07, H-08 and drain cases; raise the retention setting if a case fails |
| R7 | Python throughput below the budget on 1 vCPU (GIL, Pydantic validation per node, LangGraph superstep cost, blocking Bedrock client) | Medium | SP-1 and SP-5 measure early; group Q from P2-05 on; P5-02 tunes settings; fallbacks are a larger task with more workers behind per-process addresses, or fewer runs per process |
| R8 | Langfuse callback handler internals change (Section 15.12 overrides) | Medium | SP-4 pins the version; the fallback (template-opened observations) is designed; record-mapping tests O-01 |
| R9 | Builder documents or the registry drift | Medium | Pinned fixtures and registry with hashes; sync check in `gen --check`; coverage test in P2-09 |
| R10 | The platform dialect is broader than the two fixture files show | Medium | Rules are data; case B-15 fails CI on unknown constructs; ask the platform owners for more exports before the dev release |
| R11 | Trace volume exceeds the Neon plan (D-8) | Medium | Test branch; confirm limits before the dev release |
| R12 | The Zonic energy agent's function tool exists only in the Django runtime | Low | Answered by the fake in tests; the blueprint reports it unavailable in live runs; the open-data version calls the httpbin echo |
| R13 | Public data and APIs change | Low | CI replays recordings; live tests assert paths and shapes; recordings dated and refreshed in reviewed pull requests |
| R14 | With the UI deferred, no real client uses the API during the build, so contract gaps surface late | Medium | The contract is snapshot-tested from P2-03 (A-10, A-14); `scripts/run_client.py` exercises every flow (A-15); the UI owner reviews `generated/` at M2 and before P5-06 (Section 13); the client integration guide (P5-05) |

---

## 20. Test Data from the Strands Builder

The runtime is tested against the workflows people actually build. All workflow JSON comes from `Strands-visual-builder` (commit `24e29ee`); none contains credentials. This section is the Strands plan's Section 19 with LangGraph wording (DR-12).

### 20.1 Sources

| Set | Source | Files | Shape | Vendored to | Used for |
|---|---|---|---|---|---|
| Zonic exports | `backend/tests/workflow/fixtures/zonic/` | 7: `all-agents`, `fsh-energy-agent`, `fsh-facility-agent`, `fsh-facility-agent-v2`, `fsh-risk-agent`, `test-agent-workflow`, `test-relationship` | No `schemaVersion` (1.0); updates keyed `operation` with `role`; SQL Server tools | `tests/fixtures/workflows/builder/zonic/` | Compatibility, routing, sub-flows, tools against fakes |
| Zonic exports, 2.0 | `docs/examples-agent-wokflow/` | The same 7 with `schemaVersion: "2.0"` | 2.0 | `.../builder/zonic-v2/` | Version detection: both copies give the same blueprint |
| Templates | `backend/workflow/resources/templates/` | 4: `question_answering_with_tools`, `approval_flow`, `customer_support_swarm`, `facility_operations_swarm` | Wrapped `{name, title, description, document}`; empty `model`; updates keyed `op` | `.../builder/templates/` | Agents, approval, Swarm, the reference workflow |
| Invalid document | `docs/examples-agent-wokflow/new-workflow.json` | 1 | A Swarm with no members | `.../builder/invalid/` | Blueprint validation |
| Open-data versions | Dev database, exported once with `GET /api/v1/agents/{id}/export-package` | 3: `fsh-energy-agent`, `fsh-facility-agent`, `fsh-risk-agent` | 2.0; PostgreSQL connection `sample_open_data`; NYC Open Data, OpenFEMA, httpbin | `.../builder/open-data/` | Integration, recordings, live tests |
| InSite platform imports | `backend/tests/workflow/fixtures/agent.json` and `sub_agents.json` (identical in `ZonicAgentBuilderServer_FastAPI/tests/fixtures/`) | 9: `intelligent-buying-orchestrator` and 8 sub-agents | Platform dialect: Azure model objects, `promptTemplate`, routing tools, 53 tool nodes, loops, node-level updates, `nodeOutput` and `thread` templates | `.../builder/platform/` (one file per document) | Normalization, loops, routing tools, multi-turn state, MCP tool nodes |
| Django runtime workflows | `ZonicAgentRuntimeBackend_DjangoApp/.../tests_all/tests/test_data/workflows/` | 6, only where they add a case (for example `complex-agent`) | Builder 1.0 | `.../builder/django/` | Regression of the Django runtime's supported shapes |
| Synthetic | Written in this repository | Per uncovered node type and routing pattern; parallel fan-in of unequal lengths; Rule `expression` branches | 2.0 | `tests/fixtures/workflows/synthetic/` | `external_agent`, barriers, edge cases |

`SOURCE.json` records each file's source path, commit and SHA-256. The SQL Server versions of the FSH agents are never run against a database, only against fakes.

### 20.2 Coverage

| Node type | Fixtures |
|---|---|
| `start`, `output` | Every workflow |
| `agent` | All 7 Zonic exports; Customer Support Swarm (3 members); Facility Operations Swarm (4 members); Question Answering; the platform imports |
| `llm` | `fsh-energy-agent` (9), `fsh-facility-agent-v2` (3), `fsh-facility-agent` (2), `fsh-risk-agent` (2), Approval Flow, Facility Operations Swarm (3, one a member) |
| `workflow` | `all-agents` (3 children), `test-relationship` (1), `agent.json` |
| `rule` | `fsh-energy-agent` (5 branches), `fsh-facility-agent`, `fsh-risk-agent`, Facility Operations Swarm (2), the platform imports |
| `db_query` | `fsh-facility-agent`, `fsh-risk-agent`, `test-agent-workflow` (unconnected) |
| `http_request` | `fsh-facility-agent`, `fsh-risk-agent`, Facility Operations Swarm |
| `view` | `fsh-energy-agent` (3), `fsh-facility-agent-v2` (2), `fsh-facility-agent`, `fsh-risk-agent`, Facility Operations Swarm |
| `variable` | `fsh-energy-agent`, `fsh-facility-agent-v2`, Facility Operations Swarm, the platform imports |
| `swarm` | Customer Support Swarm, Facility Operations Swarm, `new-workflow.json` (invalid) |
| `guardrail`, `human_input`, `script`, `note` | Facility Operations Swarm (`note` also in `agent.json`) |
| `approval` | Approval Flow, Facility Operations Swarm |
| `tool` | The 9 platform imports (53 tool nodes) |
| `external_agent` | Synthetic only |

### 20.3 Fixture cases

| ID | Case | Expected |
|---|---|---|
| F-01 | Load every vendored document | All load; Zonic exports read as 1.0; each 2.0 copy gives the same blueprint |
| F-02 | Templates | The `document` member loads; an empty `model` resolves to the catalog default with a warning; `template-*` IDs never appear in the catalog |
| F-03 | Invalid documents and drift | `new-workflow.json` fails with `PLAN_INVALID` (SW-1); a fixture whose hash differs from `SOURCE.json`, or a node type without a fixture, fails CI |
| F-04 | Update key spellings | `op` and `operation` with `role` give the same state for every state operation |
| F-05 | Output paths | `{{nodes.X.output.result}}`, `.output.text` and the script path `{{nodes.X.result.<field>}}` resolve in Approval Flow and the Facility Operations Swarm |
| F-06 | Loop guard | The platform loops run until the agent answers; a scripted agent that always calls a routing tool stops at `maxVisitsPerNode` with `LOOP_LIMIT_EXCEEDED` after a bounded number of model calls; `all-agents` finishes after one sub-flow |
| F-07 | Linear runs and unreachable nodes | Question Answering runs Start, agent, Output; `test-agent-workflow` reports `DBQuery_1` unreachable and never compiles it |
| F-08 | Routing and joins | `fsh-facility-agent` and `fsh-risk-agent` take `r_else` to the HTTP node when `flow.useDBQuery` is unset and `r_default` to DB Query when `true`; in `fsh-energy-agent` each `flow.analysisType` selects its branch and `utility-analysis` (5 incoming edges), `utility-bills-variable-update` (3) and `Output_1` (2) each run once per path; nodes on untaken branches are never scheduled and emit no events |
| F-09 | Tools against recordings | Database tools receive their placeholder values from `flow` and `system`; SQL Server and PostgreSQL tools and API tools are answered from recordings; the function tool by the fake |
| F-10 | Sub-flows across fixtures | The `all-agents` supervisor hands off to each child (`fsh-energy-agent`, `fsh-facility-agent-v2`, `fsh-risk-agent`); children load with one `in` read; paths and observations nest; `test-relationship` reaches `fsh-energy-agent` the same way |
| F-11 | Customer Support Swarm | A scripted handoff from `triage` to `billing` ends with `finalAgent` `billing` and a two-step `nodeHistory`; limits 10 handoffs, 15 iterations, detection window 6 with 2 unique agents applied by the handoff tools |
| F-12 | Facility Operations Swarm members | Five members (four agents and the LLM member `report_writer`), entry `coordinator`; limits 12 handoffs, 24 iterations, 900 and 240 seconds, window 8 with 3 unique agents; each member step and handoff is an observation |
| F-13 | Interrupts | Approval Flow pauses at `Approval_1` and resumes to `Approved` or `Rejected`; in the Facility Operations Swarm a severity of 7 or more pauses at `publish_approval`; approved continues to `record_run`; rejected goes to `reviewer_feedback` (Human Input) and `Rejected`; all resume across requests and processes |
| F-14 | Live open data | The open-data FSH agents and the Facility Operations Swarm run against Bedrock, the MCP server, `sample_open_data` and the public APIs; paths and result shapes match the recorded runs |
| F-15 | Compiled graphs | Every vendored document compiles to a `StateGraph` whose nodes, `defer` flags, policies and destinations match the committed snapshot; a changed snapshot is reviewed like code |
| F-16 | Platform dialect | `agent.json` and the sub-agents normalize with the documented rules only, each listed in the blueprint notes; builder-dialect documents trigger none |
| F-17 | Platform state machine across turns | `agent.json`: turn 1 routes on `flow.current_state` to a sub-flow and sets the next state; turn 2, on another instance, reads the persisted state and takes the next branch; the 14-node loop exits to Output in both turns |
| F-18 | Platform agent and tool loops | Each sub-agent with two routing-tool calls then an answer: tool node, variable node and agent run twice in order; `toolResults` and `flow` hold the results; the MCP fake receives the resolved parameters; the run ends at Output |

### 20.4 Scenarios per workflow

| Workflow | Script | Expected path |
|---|---|---|
| `question_answering_with_tools` | One answer; optionally a `calculator` call first | Start_1, Agent_1, Output_1 |
| `test-agent-workflow` | One answer | Start_1, Agent_1, Output_1 |
| `test-relationship` | `handoff_to energy-agent`, then the child's script | Start_1, Agent_1, energy-agent (sub-flow), Output_1; no handoff: Start_1, Agent_1, Output_1 |
| `all-agents` | `handoff_to` one of the three children | Start_1, supervisor-agent, the chosen sub-flow, Output_1 |
| `fsh-facility-agent`, `fsh-risk-agent` | `handoff_to sql-query-generator`, then SQL text | Start_1, agent, sql-query-generator, Rule_1, then the HTTP node or query-analysis, Output_1; with `handoff_to data-analysis`: agent, data-analysis, report, Output_1 |
| `fsh-facility-agent-v2` | Keyword JSON, then `handoff_to direct-answer` or `data-processing` | Start_1, keywords-extraction, facility-agent, direct-answer, View_1 or data-processing, View_2, VarUpdate_1, Output_1 |
| `fsh-energy-agent` | Keyword JSON; `handoff_to get-historical-data-with-reference`; a tool call; analysis text | Start_1, keywords-extraction, energy-agent, get-historical-data-with-reference, Rule_1, the branch for `flow.analysisType`, utility-analysis, utility-bills-variable-update, Output_1 |
| `approval_flow` | One draft | Start_1, LLM_1, Approval_1 (interrupt), Approved or Rejected |
| `customer_support_swarm` | Triage hands off to billing; billing answers | Start_1, support_team (triage, billing), Output_1 |
| `facility_operations_swarm` | Parser JSON; coordinator, analyst and writer turns ending `SEVERITY: <n>` | Section 20.5 |
| `new-workflow.json` | None | Fails at blueprint with `PLAN_INVALID` |
| `agent.json` | Turn 1: `extract_entities` JSON, then a routing tool; turn 2: the next answer | Start, pre-routing rules, `current_state_router`, the chosen sub-flow or `procurement_policy_assistant`, variable updates, `display_output_rule`, Output (F-17) |
| Platform sub-agents (8) | Two routing-tool calls, then an answer | Start, agent, tool node, variable node(s), back to the agent, twice, then Output (F-18) |

### 20.5 Facility Operations Swarm paths

The template is the reference workflow (`Strands-visual-builder/docs/strands-builder/runbooks/facility-operations-swarm.md`); its mock-run path is pinned by the builder's `facilityOperationsSwarm.test.ts`, and the runtime must produce the same path.

| Input or script | Path |
|---|---|
| Investigation question; `SEVERITY: 8`; approved | Start_1, input_guardrail (`pass`), request_parser, scope_router (`r_investigate`), weather_context, operations_team, score_findings, review_gate (`r_review`), publish_approval (`approved`), record_run, report_view, Report |
| Same, `SEVERITY: 3` | … score_findings, review_gate (`r_else`), record_run, report_view, Report |
| Same, `SEVERITY: 8`; rejected | … publish_approval (`rejected`), reviewer_feedback (Human Input), Rejected |
| Parser returns `intent: definition` | Start_1, input_guardrail, request_parser, scope_router (`r_else`), quick_answer, Quick_answer |
| Guardrail fails | Start_1, input_guardrail (`fail`), Refused |
| Report without a `SEVERITY` line | Severity 10, the review path |

`About_this_workflow` (a note) is never compiled. `record_run` has two incoming edges from exclusive branches and runs once without a barrier.

### 20.6 Data behind the open-data workflows

| Source | Content | Access |
|---|---|---|
| `sample_open_data` (Neon dev) | 9 NYC Housing Authority facilities, 17,202 monthly bills from January 2022, vendors, user mapping (user 1809 sees all), 117 FEMA declarations, `v_facility_disaster`, tool helper functions | Role `sample_open_data_reader` (SELECT only, read-only transactions, 30-second timeout); `SAMPLE_OPEN_DATA_DSN` locally; `LIVE_SAMPLE_OPEN_DATA_DSN` for the nightly live job only |
| Loader and SQL | `Strands-visual-builder/backend/db/sample_open_data/`, `backend/scripts/load_sample_open_data.py` | Rerun to refresh |
| Public APIs | NYC Open Data (`jr24-e7cr`), OpenFEMA, Open-Meteo archive, httpbin | Keyless; allow-listed in `egress.json` for test settings only |

Reference results (user 1809, 1 January to 31 March 2024, facility `red hook`, reference period Q1 2023, month `2024-01`): `get_utility_bills_tool` 1,787 bills and 19 columns without a facility filter, 269 for `red hook`; `get_utility_bills_with_historical_data` 269 bills and 39 columns, and 1,548 bills with 875 matched for Electricity over two reference years; `facility_profile` 1 facility, `facility_disasters` 8 declarations; `nyc_electricity_benchmark` 25 developments, `openfema_recent_ny_declarations` 20, `weather_context` 91 days. Validation errors (`userID is required`; more than 3 reference periods) are recorded too.

### 20.7 Refreshing

| What | Command | When |
|---|---|---|
| Vendored builder files and `SOURCE.json` | `uv run python -m scripts.sync_builder --builder <path to Strands-visual-builder>` | When the builder changes a template, the registry or a fixture |
| Open-data exports | `uv run python -m scripts.export_builder_workflows --api <builder URL>` (prompts for a builder ID token) | When the dev-database versions change |
| Recordings | `uv run python -m scripts.record_fixtures` (live, needs `LIVE_SAMPLE_OPEN_DATA_DSN`) | When a fixture's tools change, or monthly |

CI never runs these; it checks only that vendored files match their hashes and that every recording a scenario needs exists.

---

<sub>Copyright © 2026 InSite. All rights reserved. · INSITEINTELLIGENCE.COM · 2021 L Street NW, 2nd Fl, Washington D.C. 20036</sub>
