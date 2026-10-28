# Runbook: Upgrade LangGraph With Waiting Runs

`langgraph`, `langchain`, `langchain-core`, `langchain-aws`, `langchain-openai`, `langchain-anthropic` and `langfuse` are each pinned to one minor version in `pyproject.toml` (AD-15, decision 0002: LangGraph 1.2.18). An upgrade gets its own pull request and nothing else.

## Why waiting runs matter

A run that waits for an answer, or an `async` run that was drained or queued, exists only as a checkpoint row (`agent_runtime.runtime_checkpoint`). The row holds LangGraph's own serialized checkpoints, writes and channel blobs. Its body records the codec `format` and the `langgraphVersion` that wrote it.

When such a run resumes, the new version must read the old row and continue the graph it describes. A change in LangGraph's checkpoint format, channel layout or task IDs between minors can break that. If it does, the run cannot resume.

## 1. Prepare the change

1. **Release notes.** Read them for every package between the two versions. Look for changes to:
   - checkpoint serialization (`JsonPlusSerializer`, `EncryptedSerializer`), channel versions and pending writes;
   - `Command`, `interrupt()` and `response_schema`, and how a resume maps to interrupt IDs;
   - `TimeoutPolicy`, `RetryPolicy`, `error_handler`, `RunControl` and `GraphDrained`, durability modes;
   - streaming (`astream_events`, namespaces) and `ParentCommand`;
   - LangChain's `HumanInTheLoopMiddleware` and agent middleware hooks. The runtime overrides `_tool_approval_request`, a private method (`engine/agents/middleware/approval.py`).
2. **Capture an old row.** Before bumping the pins, write a fixture under the current version: pause a scenario at an approval, and at an approval inside a sub-flow, and save the checkpoint bodies. Write them unencrypted, since fixtures hold no secrets. There is no such fixture yet; the first upgrade adds it under `tests/fixtures/compiled/`.
3. **Bump.** Change the pins and run `uv lock`.

## 2. Prove it

Run these suites under the new version:
- **Everything:** `uv run pytest`. In particular:
  - `tests/conformance`: every kind in a compiled graph;
  - `tests/scenarios`: the golden streams. An event-order change shows up here first;
  - `tests/api/test_persistence.py`: LangGraph's own `checkpointer_test` against `TurnSaver`;
  - `tests/api/test_interrupts.py`, `test_approvals.py`, `test_durable_runs.py`: pause, resume, drain and claim.
- **Spikes.** The behavior in `docs/decisions/0001-spike-reports/SP-01` to `SP-10` still holds.
- **Old rows.** The rows from step 2 resume under the new version and finish with the same result.
- **Live suite** on the deployed gateway: `uv run pytest -m live`.
- **Every other gate.**

## 3. Decide how to roll out

| Old rows resume under the new version | Rollout |
|---|---|
| Yes | A normal rolling deploy ([deploy.md](deploy.md)). Waiting runs resume on new instances |
| No | Do not deploy while rows wait. Choose one of the two options below |

**Option 1: wait for the rows to clear.**
1. Stop starting new runs that pause.
2. Let waiting runs be answered or expire (`interrupts.defaultExpiryHours`, 72 h), and queued runs finish.
3. When the count below is zero, deploy.

Read-only, with direct access to the database:
```sql
SELECT c.body->>'langgraphVersion' AS version, r.status, count(*)
FROM agent_runtime.runtime_checkpoint c
JOIN agent_runtime.runtime_run r USING (run_id)
WHERE r.status IN ('waiting_input', 'queued', 'running')
GROUP BY 1, 2;
```

**Option 2: keep the old minor until the rows expire.** Run instances of both releases, and route resumes of old rows to the old ones. The runtime has no routing by `langgraphVersion` yet. This option needs that routing built first, so prefer option 1.

## 4. After the rollout

- **Check:** the count above shows no new rows under the old version.
- **Record:** a decision record for the new pins (as 0002 did) and an entry in `docs/build-log.md`.
