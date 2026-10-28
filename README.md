# ZonicAgentRuntimeBackend_Python_LangGraph

The backend of the Zonic Agent Runtime. It runs the workflows people design in the Zonic builder:
- each document is compiled at run time into a LangGraph `StateGraph`, cached per revision;
- runs stream over SSE, pause for people (approvals, human input, tool approvals) and resume on any instance;
- asynchronous runs survive restarts through a durable queue;
- everything is stored through the pgfoundation gateway in PostgreSQL, Langfuse-shaped traces included.

The runtime UI is deferred. Clients build on the published contract under `generated/`.

## Documents

| Document | For |
|---|---|
| [Architecture design](docs/zonic-agent-runtime-langgraph-architecture-design.md) | What the runtime is and why: the specification |
| [Implementation plan](docs/zonic-agent-runtime-langgraph-implementation-plan.md) | The tasks, phases and test catalog; its Section 2 amends the design |
| [Build log](docs/build-log.md) | What each task built, deviations, checkpoint results |
| [Decision records](docs/decisions/README.md) | Decisions taken during the build |
| [Client integration guide](docs/client-integration.md) | Building a client: contract files, auth, streaming, interrupts, datasets |
| [Extending the runtime](docs/extending.md) | New node types, kinds, multi-agent patterns, hook types |
| [Runbooks](docs/runbooks/README.md) | Deploy, rotate keys, add a model, upgrade LangGraph, use the test client |
| [CLAUDE.md](CLAUDE.md) | Commands and the rules CI enforces |

## Getting started

Requires Python 3.13 and uv.

```sh
uv sync
cp .env.example .env            # fill in what you need; .env is git-ignored
uv run pytest                   # offline: unit, compiler, conformance, scenario, API, contract, principles
```

The offline tests need no credentials: the gateway, models, MCP server and identity provider are fakes.

To run the server locally:

```sh
uv run python -m zonic_runtime.server     # public API on :8000, internal /metrics on :9000
```

The server needs these in `.env`:
- `ZONIC_GATEWAY_API_KEY`: the deployed gateway's key for caller `zonic-agent-runtime`;
- `ZONIC_OIDC_*`: the Cognito pool;
- credentials for the model providers you use.

With `ZONIC_ENV=dev`, the interactive API docs are at `http://127.0.0.1:8000/docs`. To try it by hand, use the [test client](docs/runbooks/test-client.md).

## Checks

The gates CI runs (details in [CLAUDE.md](CLAUDE.md)):

```sh
uv run ruff check . ; uv run ruff format --check . ; uv run mypy ; uv run lint-imports
uv run python -m scripts.gen --check              # generated/ matches the models
uv run python -m scripts.sync_builder --check     # vendored builder registry and workflows
uv run python -m scripts.sync_recordings --check  # vendored recorded responses
uv run python -m scripts.catalog_check            # every catalog case has a test
uv run pytest -m live                             # opt-in: against the deployed gateway (needs .env)
uv run pytest -m sql                              # opt-in: the SQL scripts (ZONIC_SQL_TEST_DSN in .env)
```

## Layout

```
src/zonic_runtime/
  api/            FastAPI routes, auth, errors, SSE
  application/    run, resume, interrupt, dataset, observability services
  workflow/       loading, dialects, blueprint analysis, the compiler, templates
  engine/         everything that touches LangGraph: node template, kinds, agents, Swarm, persistence, streaming
  run/            driver, executor (permits, claimer, leases), events, state stores, callbacks
  hooks/          hook registry, resolver, runner, built-in types
  integrations/   gateway, MCP, egress, sandbox, Langfuse export, HTTP pools
  models/         every schema, as Pydantic models
  config/env.py   the only reader of the process environment
resources/        settings, manifest, models, hooks, error catalog, dialects (all configuration)
generated/        OpenAPI and JSON Schemas: the published client contract
db/               agent_runtime and agent_observability SQL, vendored from the Node.js runtime
scripts/          gen, catalog check, vendoring, database init, the test client
tests/            catalog.yaml (the sanity cases), progress.yaml, and the suites
```
