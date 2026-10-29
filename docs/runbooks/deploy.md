# Runbook: Deploy

How to put a build of the runtime on an environment. The AWS target (P5-04, D-3) is built:
- `deploy/aws/runtime.yaml` is a CloudFormation stack: its own VPC, an ALB reachable only from CloudFront (which serves HTTPS), and an ECS Fargate service with one 1 vCPU / 2 GB task;
- the secrets are in Secrets Manager, and the task role calls Bedrock;
- `deploy/aws/deploy.sh` checks the account, builds and pushes the image, writes the secrets and deploys the stack:
  ```sh
  AWS_PROFILE=tripleq-development bash deploy/aws/deploy.sh   # prints the https URL when the service is stable
  ```

The schemas are the product database's (Neon `agent-build-langgraph`, through the gateway's `agentruntime` and `agentobservability` datasources), not `neondb_dev`: that was the user's choice of 2026-10-26. The sections below are what the script does and what to check.

## Before you deploy

1. **Gates.** Every gate passes on the commit: `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, `pytest`, `scripts.gen --check`, `scripts.sync_builder --check`, `scripts.sync_recordings --check` and `scripts.catalog_check`.
2. **Vendored SQL.** `uv run python -m scripts.sync_sql --check` passes. If the vendored SQL differs from the Node.js repository, the database may not match the code. Never edit the vendored procedures here.
3. **Waiting runs.** If the release changes the LangGraph minor version, follow [upgrade-langgraph.md](upgrade-langgraph.md) first.

## 1. Database

- **Schemas.** The target database needs the `agent_builder` schema (the builder installs it), then `agent_runtime` and `agent_observability`. Install or upgrade the last two from `db/`. The scripts are idempotent, and each records its version in `schema_migrations`:
  ```sh
  # DSNs only from the environment, never as arguments (they would land in shell history)
  AGENTRUNTIME_DSN=... AGENTOBSERVABILITY_DSN=... scripts/init_db.sh      # or scripts/init_db.ps1
  ```
  Leave out `AGENTOBSERVABILITY_DSN` when both schemas live in one database.
- **Gateway.** The pgfoundation gateway must know both datasources. `deploy/pgfoundation.agentruntime.yaml` is the registration: point the gateway's `PGF_CONFIG` at it and set `AGENTRUNTIME_DSN` and `AGENTOBSERVABILITY_DSN` in the gateway's environment. The gateway project itself is never changed (D-2).
- **Caller.** The gateway registers the caller `zonic-agent-runtime` with its API key and grants. Changing the grants is the gateway owner's call: ask before changing them.

## 2. Image

```sh
docker build -t zonic-agent-runtime:$(git rev-parse --short HEAD) .
```

The image (`Dockerfile`):
- is based on `python:3.13-slim`, with dependencies from `uv.lock` (`--frozen`, no dev groups);
- runs as user 10001;
- serves both ports from one process: `python -m zonic_runtime.server`.

Run one container per instance and scale horizontally (AD-11).

## 3. Configuration

Tunable values live in `resources/settings.json`, with the overlay for `ZONIC_ENV` (`settings.dev.json`, `settings.prod.json`). The image also copies the overlays in `deploy/aws/resources/`, which only the AWS deployment reads; today that is `models.dev.json`, whose stand-ins serve the Claude models with OpenAI. Change them in the repository, never in the environment. The environment holds only endpoints and credentials (`src/zonic_runtime/config/env.py`):

| Variable | Notes |
|---|---|
| `ZONIC_ENV` | `dev` or `prod`. Selects the settings overlay. `prod` turns on checkpoint encryption and turns off `/docs` |
| `ZONIC_GATEWAY_BASE_URL`, `ZONIC_GATEWAY_API_KEY` | The gateway and caller `zonic-agent-runtime`'s key |
| `ZONIC_OIDC_ISSUER`, `ZONIC_OIDC_AUDIENCE`, `ZONIC_OIDC_JWKS_URL` | The Cognito pool. Without a JWKS URL every token is refused |
| `ZONIC_AWS_REGION` and the task role | Bedrock. The model regions are in `resources/models.json` |
| `ZONIC_AZURE_OPENAI_ENDPOINT`, `ZONIC_AZURE_OPENAI_API_KEY`, `ZONIC_OPENAI_API_KEY`, `ZONIC_ANTHROPIC_API_KEY` | Only for providers whose models are enabled. The stack injects `ZONIC_OPENAI_API_KEY` from Secrets Manager (`zonic-agent-runtime/<stage>/openai-api-key`) |
| `ZONIC_NEBIUS_API_KEY`, `ZONIC_NEBIUS_BASE_URL` | Nebius Token Factory, which serves the NVIDIA Nemotron models. The URL defaults to `https://api.tokenfactory.nebius.com/v1/`. The stack injects the key from Secrets Manager (`zonic-agent-runtime/<stage>/nebius-api-key`) |
| `ZONIC_MCP_BASE_URL` | The MCP server, for MCP tools and database nodes |
| `ZONIC_CACHE_API_URL` | The cache service, for datasets with `store: cache` |
| `ZONIC_SANDBOX_URL` | A Piston API. Without it, Script nodes are `FEATURE_DISABLED` |
| `ZONIC_CHECKPOINT_AES_KEY` | 64 hex characters (32 bytes). Required when `state.encryption.enabled` (prod), or the process refuses to start |
| `ZONIC_LANGFUSE_*` | Only when `observability.exporters` includes `langfuse` |
| `ZONIC_MCP_AUTH_TOKEN` | The MCP server's bearer token (its `MCP_AUTH_TOKEN`) |
| `ZONIC_SECRET_<NAME>` | Values of `secret:<name>` references (tool keys, callback HMAC keys). On ECS, add each one to the task definition's `Secrets` from Secrets Manager: the container still reads it from its environment |

Keep every credential in the secret store or the task definition's secrets. Never put one in an image, a document or the repository.

## 4. Network

- **Public port 8000**, behind the load balancer:
  - TLS ends at the load balancer;
  - set the idle timeout above `sse.heartbeatSeconds` (15 s), for example 60 s, so streams stay open;
  - liveness: `GET /healthz`;
  - readiness: `GET /readyz`. It answers `503` with the failing dependency, and `200 degraded` while the instance sheds new runs.
  - Set `api.corsOrigins` for the browser client's origin.
- **Internal port 9000** (`api.internalPort`): `/metrics` for autoscaling, and `/internal/runs/{id}/events`, which other instances use to proxy a run's events. Reachable between instances only, never from the internet.
  - Instances find each other by the address in a run's lease: `api.internalAddress`, or else the container's host name and port 9000.
  - On a platform where host names are not routable between tasks, set `api.internalAddress` in the environment's settings overlay.

## 5. Rolling out

Replace instances one at a time. On `SIGTERM` an instance drains:
1. It claims no more queued runs.
2. Each running `async` run stops at its next superstep boundary. Its checkpoint is saved and the run is released to the queue, where another instance claims it and continues without repeating finished nodes.
3. `stream` and `sync` runs get `shutdown.drainSeconds` (30 s) to finish. After that they fail with `SHUTDOWN`; the client sees `run.failed` and can retry.
4. Runs waiting for an answer are not touched. Any instance resumes them.

Give the container a stop timeout above the drain: uvicorn waits up to 60 s. The stack sets 90 s on ECS (`StopTimeout`) and a deregistration delay of 45 s on the target group.

Every server instance runs two schedules:
- **Maintenance**, every `maintenance.intervalMinutes` (60): it ensures trace partitions ahead, purges traces past `observability.retentionDays` and sessions past `retention.sessionsDays`, and refreshes the usage rollups.
- **Interrupt expiry**, every `interrupts.expiryCheckSeconds`: it moves runs whose questions expired onto their expiry path.

## 6. Verify

1. **Readiness.** `GET /readyz` answers `{"status": "ok"}` on every instance.
2. **Test client.** With a token of the environment's pool ([test-client.md](test-client.md)), complete a full run, a run that pauses and is answered, and a dataset page. `ZONIC_DEPLOYED_URL=<url> AWS_PROFILE=tripleq-development uv run pytest -m live tests/live/test_deployed_live.py` does the same with the test identity (V-06, M5).
3. **Traces.** `GET /v1/runs/{id}/trace` of that run shows its nodes.
4. **Product data.** Test runs go under a test owner and tenant, never under a real user.

## Rollback

Deploy the previous image tag.
- **Runs.** If the two releases share a LangGraph minor version, waiting and queued runs continue on it unchanged. If they differ, [upgrade-langgraph.md](upgrade-langgraph.md) applies in reverse.
- **Database.** Before rolling back across a database change, check the change in `db/` (vendored from the Node.js repository): the older image needs the procedures and views it calls.
