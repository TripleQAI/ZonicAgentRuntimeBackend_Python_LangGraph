# 0001: LangGraph verification spikes SP-1 to SP-10

- **Status:** accepted at the M0 review (2026-10-26)
- **Date:** 2026-10-25
- **Decides:** D-4 (exact pins) and D-5 (fallbacks), plan task P0-10
- **Evidence:** one report per spike in [`0001-spike-reports/`](0001-spike-reports/). The spike code (63 checks, all passing on 2026-10-25) was removed after M0, as the plan says. It stays in git history: `git show bcd2fde:spikes/<file>` (the paths are those named in each report). Restore it with `git checkout bcd2fde -- spikes` to re-run after a LangGraph or LangChain minor upgrade.

## Context

Design Section 20.1 lists ten behaviors the design relies on that the LangGraph and LangChain documentation does not state outright, each with a fallback. The plan says the spikes run before the compiler (P1-09) and that every failed spike gets a decided fallback at M0.

## D-4: pins

The installed versions pass every spike. `pyproject.toml` already pins each to one minor with `~=`, so nothing changes:

| Package | Version | Pin |
|---|---|---|
| langgraph | 1.2.17 (langgraph-checkpoint 4.3.0, langgraph-prebuilt 1.1.1) | `~=1.2.17` |
| langchain (with `mcp`) | 1.4.7 | `~=1.4.7` |
| langchain-core | 1.7.0 | `~=1.7.0` |
| langchain-aws | 1.9.0 | `~=1.9.0` |
| langchain-openai | 1.7.2 | `~=1.7.2` |
| langchain-anthropic | 1.7.6 (anthropic 1.17.0) | `~=1.7.6` |
| langfuse | 5.0.4 | `~=5.0.4` |
| fastmcp (test) | 4.1.3 (mcp 2.3.0) | `~=4.1.3` |
| langgraph-checkpoint-conformance (test) | 0.0.2 | `~=0.0.2` |

Two APIs the design uses are marked experimental or beta and emit `LangChainBetaWarning`: `astream_events(version="v3")` and `langchain.mcp`. Both stay behind their ports (`engine/streaming/`, the MCP port). A minor upgrade of LangGraph or LangChain re-runs these spikes first (restored from git history, above).

## D-5: outcome per spike

No spike failed, so no design fallback is taken. Four spikes add conditions the later tasks must meet.

| Spike | Outcome | Condition for later tasks | Task |
|---|---|---|---|
| SP-1 `Command` joins, `defer`, overhead | Holds | `defer` is a **run-level** barrier: a deferred join also waits for branches that never reach it. Keep `defer` for joins after branching, because list-form barrier edges never fire when a source branch is untaken. `durability="exit"` costs nothing per superstep (203 µs, the same as no checkpointer). The installed default recursion limit is 10,007, so `recursion_limit` is always set. | P1-09, P1-11 |
| SP-2 TurnSaver, nested interrupts, new-process resume | Holds | `TurnSaver` = `InMemorySaver` + bundle export and import (zstd, base64). Resume in a new process works for child-graph and `create_agent` interrupts, and the model turn is not repeated. Base conformance passes. `error_handler` gets `NodeError` and `Runtime`. Interrupt IDs are 32 hex characters and fit `runtime_interrupt.interrupt_id uuid`. | P2-04, P4-01 |
| SP-3 events v3 | Holds | Pass `CustomTransformer` and `ToolCallTransformer` to every v3 call; without them there are no `custom` or `tools` events. The namespace rule of DR-11 is confirmed, and child graphs see the root run's context. | P2-01 |
| SP-4 Langfuse v5 | Holds, with a change | Do not use the handler's observation types: it types any chain whose name contains "agent" as `agent`. The compiler attaches `zonic_node_id`, `zonic_node_type` and `zonic_kind` as node metadata. That metadata reaches the node span and every model and tool span inside it. The exporter types and enriches from it, reads `Command` output as `{update, goto}`, drops the agent's wrapper spans, and **re-parents children of dropped spans**. | P2-07, P2-08 |
| SP-5 Bedrock concurrency | Bounded-executor fallback, as named | `ChatBedrockConverse` has no native async. LangChain runs its stream on the loop's **default** executor, so 32 threads give a p50 time-to-first-token of 1.39 s against 0.36 s at 160. `settings.models.executorThreads` is now 160. `ModelFactory` sizes botocore `max_pool_connections` to match (the default is 10). A per-provider executor needs a thin subclass that overrides `_astream`. `AsyncAnthropicBedrock` stays the alternative if the P5 load test shows thread cost. | P3 `ModelFactory`, P5 load test |
| SP-6 Swarm | Holds | Handoff tools return `Command(graph=Command.PARENT)` from inside agents invoked in member nodes. The swarm's `maxSteps` is the swarm invocation's `recursion_limit`, and `GraphRecursionError` maps to `LOOP_LIMIT_EXCEEDED`. | P3 swarm kind |
| SP-7 MCPAdapter | Holds | Hold one entered `MCPAdapter` per server for the process lifetime: held, 25 calls share 1 connection; not held, every call connects. Refresh tool lists with `cache_mode="refresh"`. Cancellation reaches the server. | P3-01 |
| SP-8 Partial resume | Holds | Always resume with an ID-keyed map. A plain value raises when several interrupts are pending. Unanswered nodes re-run on every resume, so nothing with side effects runs before `interrupt()` (DR-10). | P4-02, P4-03 |
| SP-9 Node names | Holds, wider reserved set | Escape `:`, `\|` and every name starting with `__` (LangGraph reserves `__interrupt__`, `__pregel_*` and others; some are reported only at compile). Reject empty node IDs in validation. | P1-05, P1-09 |
| SP-10 Drain | Holds, with a condition | Under `durability="exit"`, a drain while a child graph runs inside a node loses the child's progress: its steps re-run on resume (SP-2's interrupt case is not affected). Async runs, the only ones resumed after a drain, use `durability="async"`, with the TurnSaver pruned to `state.checkpointsKeptPerNamespace` (2). Stream and sync runs keep `"exit"`, since they fail with `SHUTDOWN` on instance loss. | P2-04, P4-05 |

## Consequences

- No design fallback is needed; the critical spikes (SP-1 to SP-3 and SP-10, plan risk R4) hold.
- The design text should state the SP-1 `defer` scope, the SP-4 typing rule, the SP-9 reserved set and the SP-10 durability split at its next revision.
- `resources/settings.json`: `models.executorThreads` 32 → 160.
- Reported upstream (optional): the SP-10 child-checkpoint gap under `durability="exit"`, and the SP-4 name-based typing.
