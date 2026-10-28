# P5-02 Performance Validation Report

Measured 2026-10-28 against the budgets of design Section 11.1.

**Target task.** One process in a container held to 1 vCPU and 2 GB (`docker run --cpus 1 --memory 2g`), the target size of D-3, run with Docker Desktop on the build machine. The deployed Fargate task was not available: the AWS deployment (P5-04) is waiting for approval.

**Gateway.** The real one: the deployed pgfoundation Lambda in tripleq-development, over the internet from the build machine to us-east-1.

**Model.** Simulated, as the design specifies: `tests/load/model.py`, a scripted model whose chunks carry their emission time.

## Results

| Budget (design 11.1) | Measured | Verdict |
|---|---|---|
| Compilation, 25-node document, cache miss ≤ 50 ms | p95 15.9 ms | Pass |
| Compilation, cache hit ≤ 0.1 ms | p95 0.002 ms | Pass |
| Node template + superstep per executed node ≤ 2 ms + 1 ms (Q-02, 200 nodes) | p95 1.10 ms per node (turn p95 220 ms) | Pass |
| Token latency added, model chunk to SSE frame, p95 ≤ 20 ms (Q-04, 200 tokens/s) | p95 6.5 ms, p99 10.4 ms; 200 tokens in 67 frames | Pass |
| 150 concurrent streaming runs per process, event-loop lag p99 ≤ 50 ms (Q-05) | 450 of 450 runs completed with 150 active; lag p99 24.7 ms; no event of another run in any stream | Pass |
| Memory per active run ≤ 2 MB (Q-05) | 0.79 MB: RSS 221 MB idle, 339 MB at 150 active (replay buffers included) | Pass |
| Run start, request to `run.started`, p95 ≤ 150 ms (Q-01) | p50 84 ms, p95 156 ms, p99 300 ms | See note 1 |
| Gateway calls per turn: two | Two: the start batch and `sp_run_finish` (Q-01, Q-02, offline and here) | Pass |
| `maxConcurrentRuns` (150) reached: 429, running runs unaffected (R-04) | 200 users: 150 completed, 50 got `429 RATE_LIMITED`; lag p99 38.6 ms | Pass |
| Model stream stalls (R-01) | botocore's read timeout (`models.requestTimeoutSeconds`) ends the stall; the LLM node retries `MODEL_UNAVAILABLE` and the turn completes, or fails with `MODEL_UNAVAILABLE` after its attempts (two new tests) | Pass, see note 3 |
| Gateway cold start and throttling (R-05) | See D-9 below: throttled calls are now retried | Fixed in code; capacity open |
| Replay buffers and caches under load (R-06) | After Q-05, a further 150 runs added 10 MB of RSS: the buffers are released after `stream.replayRetentionSeconds` | Pass |

## Notes

1. **Run start.** The time includes the internet round trip from the build machine to us-east-1. A start makes one batched gateway call; a single user's start took 85 ms to `run.started`, most of it that round trip. The p95 of 156 ms is just over the budget, from the slower requests while 150 users started within 10 s. Run inside the region on Fargate, the round trip is a few milliseconds, so this should be measured again after P5-04.
2. **Driver instead of k6.** k6 cannot read an SSE body as it streams, so token timing would be lost. `tests/load/drive.py` (httpx, asyncio) is the load driver instead.
   - For Q-04 the driver ran inside the container, so the server's and the driver's clocks agree.
   - For Q-05 and R-04 it ran on the host, so its CPU is not counted against the target's one vCPU.
3. **A retried node's tokens.** A stalled stream retried by its node has already sent tokens. The client receives the partial tokens, then a second `node.started` for the same node and visit, then the retried answer. The client guide now tells clients to discard the earlier tokens. An explicit `attempt` field on `node.started` would be clearer, but it is a contract change, left for review.
4. **LangGraph's `idle_timeout` does not detect a stalled model.** Streamed tokens do not count as progress for `TimeoutPolicy.idle_timeout`: on a node whose stream sends a chunk every 0.2 s, an idle timeout of 0.3 s fires. The manifest therefore leaves `idleTimeoutSeconds` unset for model nodes, and the provider client's read timeout catches stalls.

## D-9: gateway capacity

**Found.**
- The tripleq-development account's Lambda concurrency limit is 10 (`ConcurrentExecutions` 10, the default of a new account). Every Lambda in the account shares it: the gateway, the MCP server and the others.
- Under a burst of 150 simultaneous starts, Lambda refused about two of every three gateway calls with `429` (`ConcurrentInvocationLimitExceeded`, `FunctionInvocationRateLimitExceeded`).
- The runtime mapped that `429` to `DATA_REQUEST_INVALID` and did not retry: 61 of 450 starts failed with `500`.

**Fixed in the runtime.**
- A `429` from the gateway is now `DATA_UNAVAILABLE` (retryable), and every call, writes included, is retried `gateway.throttleRetries` (4) times with backoff. A throttled request was never run, so retrying a write is safe.
- New test: `test_R05_a_throttled_call_is_retried_writes_included_then_data_unavailable`.

**Measured.** With the starts spread over 10 s, all 450 runs succeeded at 150 concurrent (Q-05 above).

**Open (owner's decision).**
- Request a Lambda concurrency increase for the account through Service Quotas (1,000 is the usual regional default), or move the gateway to its container deployment.
- Until then, bursts of more than about 10 simultaneous gateway calls queue in the runtime's retries, and long bursts still fail.

## Settings

No setting changes were needed for the budgets:
- `runs.maxConcurrentRuns` (150) is the measured capacity at the target size;
- `engine.maxConcurrencyPerRun` (8) and the pressure thresholds held (no `OVERLOADED` sample in any run);
- the executor threads and claim intervals were not reached.

One new setting:
- `gateway.throttleRetries`: 4.

The model quotas in `models.json` remain the providers' limits. The governor queueing under them is proven offline (R-02).

## Data

The load runs wrote 1,013 sessions and runs, 1,982 messages, 1,013 traces and 11,019 observations under owners `load-test-001` to `load-test-200`. All were deleted afterwards by owner: a count before, one transaction, a count after. The 176 other runs in the database were not touched.

## Reproducing

```sh
docker build -f tests/load/Dockerfile -t zonic-agent-runtime:load .
docker run --rm --cpus 1 --memory 2g zonic-agent-runtime:load /app/.venv/bin/python -m tests.load.bench
# the server, on the real gateway (values from .env, never echoed)
docker run -d --name zrt-load --cpus 1 --memory 2g -p 18000:8000 -p 19000:9000 \
  -v "$PWD/tests/load/.keys:/load" -e LOAD_KEY_FILE=/load/key.pem -e LOAD_TOKENS=50 \
  -e LOAD_TOKEN_RATE=10 -e ZONIC_ENV=dev -e ZONIC_GATEWAY_BASE_URL=... -e ZONIC_GATEWAY_API_KEY=... \
  zonic-agent-runtime:load
uv run python -m tests.load.drive --users 150 --runs 3 --tenants 5 --ramp-seconds 10   # Q-05
uv run python -m tests.load.drive --users 200 --runs 1 --tenants 5 --ramp-seconds 4    # R-04
# Q-04: restart the server with LOAD_TOKENS=200 LOAD_TOKEN_RATE=200, then inside it:
docker exec zrt-load /app/.venv/bin/python -m tests.load.drive --base-url http://127.0.0.1:8000 \
  --metrics http://127.0.0.1:9000/metrics --key /load/key.pem --users 5 --runs 4 --tenants 1 --stamps
```

After a run against the product database, delete the `load-test-%` owners' sessions and traces. The raw results are in [results/](results/).
