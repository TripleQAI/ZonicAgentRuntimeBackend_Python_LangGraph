# 0002: Upgrade LangGraph to 1.2.18 for node error handlers

**Status:** accepted at the M1 review (2026-10-26)

## Context

Design 8.8 implements the error policies (`fail_run`, `continue`, `route`) with LangGraph's node-level `error_handler`. While building the node template (P1-10), the conformance cases C-06, C-07 and C-14 failed. With LangGraph 1.2.17 a handled error was re-raised at the end of the run whenever the failing task ran on the runner's futures path:

- the failing node shares its superstep with another task (any parallel branch; case E-15), or
- the run streams the `custom` or `messages` mode, including `astream_events(version="v3")` with `CustomTransformer`.

The second condition holds for every streamed run, because node events and tokens use those channels. The error handler ran and its `Command` was applied, then the original exception ended the run. `ainvoke`, and `astream` with `values`, `updates`, `debug`, `tasks` or `checkpoints` alone, were not affected.

**Cause:** `AsyncBackgroundExecutor.__aexit__` (`langgraph/pregel/_executor.py`) re-raised the exception of every submitted task. It ignored `SKIP_RERAISE_SET`, where the runner records the failures an error handler took over. LangGraph 1.2.18 (on PyPI) adds exactly that check: `if not reraise or task in SKIP_RERAISE_SET: continue`.

## Decision

Pin `langgraph~=1.2.18` (`uv.lock`: only `langgraph` changes; `langgraph-checkpoint` 4.3.0 and `langchain-core` 1.7.0 are unchanged).

Decision 0001 asks that an upgrade re-run the verification spikes first. They were restored from `bcd2fde` into the project root, run, and removed again: **63 of 63 pass on 1.2.18**, with no change to any SP-1 to SP-10 conclusion. The full suite passes as well.

## Consequences

- The error policies work as designed in parallel supersteps and in streamed runs. `test_E15_continue_in_one_parallel_branch_keeps_the_sibling_updates` is the regression test: it fails on 1.2.17.
- No workaround (a patched executor, or error policies inside the template) is needed. Either would have weakened design 8.8: inside the template, `TimeoutPolicy` failures and `RetryPolicy` exhaustion could not reach the policy.
- Keeping 1.2.17 and patching the executor until a later minor release was the alternative. It is rejected as more fragile than a patch-level upgrade, and the reviewer chose 1.2.18.
