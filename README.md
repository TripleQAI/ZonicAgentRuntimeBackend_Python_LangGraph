# ZonicAgentRuntimeBackend_Python_LangGraph

The backend of the Zonic Agent Runtime. It runs the workflows people design in the Zonic builder:
- each document is compiled at run time into a LangGraph `StateGraph`, cached per revision;
- runs stream over SSE, pause for people (approvals, human input, tool approvals) and resume on any instance;
- asynchronous runs survive restarts through a durable queue;
- everything is stored through the pgfoundation gateway in PostgreSQL, Langfuse-shaped traces included.

The chat UI is [ZonicAgentRuntimeUI_NextJS_For_LangGraph](https://github.com/TripleQAI/ZonicAgentRuntimeUI_NextJS_For_LangGraph). Other clients build on the published contract under `generated/`.

## Live demo

| App | URL |
|---|---|
| Chat, on this runtime | https://dev.tripleq.ai/agent-runtime-lg |
| Agent builder | https://dev.tripleq.ai/agent-visual-builder-lg/workflows |

Both sign in with Amazon Cognito. The test accounts are in the hackathon submission's testing instructions.

## NVIDIA Nemotron on Nebius Token Factory

This runtime powers our entry in the Nebius x NVIDIA Global AI Hackathon (Best Apps and Agents track). Every LLM node, agent and swarm member in the demo workflows runs on an NVIDIA Nemotron model served by [Nebius Token Factory](https://tokenfactory.nebius.com/).

### How it is wired

- **Model catalog.** [`resources/models.json`](resources/models.json) lists the Nemotron models under short codes. A workflow node names a model by its code, for example `nemotron-3-ultra`, and the builder offers the same codes in each node's model list.

  | Code | Token Factory model |
  |---|---|
  | `nemotron-3-nano` | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
  | `nemotron-3-super` | `nvidia/nemotron-3-super-120b-a12b` |
  | `nemotron-3-ultra` | `nvidia/Nemotron-3-Ultra-550b-a55b` |
  | `nemotron-3.5-super-vl` | `nvidia/Nemotron-3.5-Super-VL` |

- **Connection.**
  - Token Factory is OpenAI-compatible, so these entries use the `openai` provider with `endpointEnv: nebius_base_url` and `apiKeyEnv: nebius_api_key`.
  - The endpoint defaults to `https://api.tokenfactory.nebius.com/v1/` ([`config/env.py`](src/zonic_runtime/config/env.py)).
  - The key is `ZONIC_NEBIUS_API_KEY`. Deployed, it comes from AWS Secrets Manager ([deploy runbook](docs/runbooks/deploy.md)).
- **Per-model limits.** Each entry has its own quota: requests and tokens per minute, and calls in flight. Nemotron 3.5 Lightning is in the catalog but disabled, because it wrote its reasoning text into the answers.
- **Streaming usage.** Token Factory's Nano and Ultra put a running usage total on every stream chunk, and LangChain added those up: a 7-call run reported 3.6M tokens. `FinalUsageChatOpenAI` ([`model_factory.py`](src/zonic_runtime/engine/agents/model_factory.py)) counts usage only from the closing chunk, so traces and token budgets stay correct.
- **Tested live** on the four enabled models: tool calls, token streaming and `json_schema` structured output.

### Which model does what

We use Nano for the small, frequent calls and Ultra where reasoning matters. We chose each model by running the workflows on Token Factory:

| Role in the demo workflows | Model | Why |
|---|---|---|
| `question_router`, `lane_redirect`: a one-line routing decision, a short redirect | `nemotron-3-nano` | Fast and cheap. Nemotron reasons before it answers, and the reasoning counts toward `maxTokens`, so these nodes allow 1,000 tokens (200 tokens cut the answers off). |
| `energy_analyst`, `facilities_engineer`, `advisory_rewriter`: the tool-calling lane agents | `nemotron-3-super` | Reliable tool calls at a fraction of Ultra's cost. Nano took 7–8 s per tool call. |
| `intake_coordinator`, `risk_advisor`: swarm coordination and the exposure reasoning | `nemotron-3-ultra` | In about two of three swarm transfers, Super sent `context: "None"` (a string), which the runtime rejects; Ultra's hand-off arguments were valid. A full swarm risk run costs about $0.21. |

`nemotron-3.5-super-vl` is enabled, but the demo doesn't use it yet because no demo takes image input.

The advisory guardrail's policy model is a runtime setting, `guardrails.model` in [`resources/settings.json`](resources/settings.json). When it is empty, the catalog default is used.

### The demo agents

Four workflows, built in the agent builder on open data only:

| Workflow | What it answers |
|---|---|
| Energy Agent | Utility bill history of New York City public-housing developments (electricity and gas), cited with bill IDs |
| Facilities Agent | Why buildings and the grid behave as they do: NYISO hourly load, hourly weather, building benchmarking, equipment records |
| Risk Agent | Capacity and price exposure for the coming week, with options. It gives no recommendation, and a guardrail checks every answer. |
| All Agents · Energy, Facilities & Risk | A LangGraph swarm of an intake coordinator and the three lane agents, which hand a conversation to each other turn by turn |

The agents' tools run on the [Zonic MCP server](https://github.com/TripleQAI/ZonicMCPServer_FastAPI). Data:
- [NYC Open Data](https://opendata.cityofnewyork.us/): NYCHA electric and gas bills, LL84 benchmarking, and energy audit measures.
- [NYISO](https://www.nyiso.com/) public load, forecast and day-ahead price files.
- [Open-Meteo](https://open-meteo.com/) weather (CC BY 4.0).
- [math.js](https://mathjs.org/) for exact arithmetic.

### The platform

| Repository | Role |
|---|---|
| [ZonicAgentBuilderUI_NextJS](https://github.com/TripleQAI/ZonicAgentBuilderUI_NextJS) | The visual builder: design a workflow and pick a Nemotron model for each node |
| [ZonicAgentBuilderServer_FastAPI](https://github.com/TripleQAI/ZonicAgentBuilderServer_FastAPI) | The builder's API: validation, import and export, the node registry and model options |
| **ZonicAgentRuntimeBackend_Python_LangGraph** (this repository) | Runs the workflows on LangGraph and calls Nemotron through Token Factory |
| [ZonicAgentRuntimeUI_NextJS_For_LangGraph](https://github.com/TripleQAI/ZonicAgentRuntimeUI_NextJS_For_LangGraph) | The chat app people use the agents in |
| [ZonicMCPServer_FastAPI](https://github.com/TripleQAI/ZonicMCPServer_FastAPI) | The agents' tools over open data |
| [ZonicPostgresqlServer_FastAPI](https://github.com/TripleQAI/ZonicPostgresqlServer_FastAPI) | The PostgreSQL gateway every service stores data through (Neon) |

All of it is deployed on AWS (Lambda behind CloudFront), with PostgreSQL on Neon.

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
- `ZONIC_NEBIUS_API_KEY`: a Nebius Token Factory API key, for the Nemotron models;
- credentials for any other model providers you use.

To add or change a model, see the [add-a-model runbook](docs/runbooks/add-model.md).

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

## License

Licensed under the Apache License 2.0; see [LICENSE](LICENSE).
