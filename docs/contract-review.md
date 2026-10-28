# Contract Review: Python Runtime and the Runtime UI

This is the review the UI owner gives before release (plan P5-06, design Section 14). It compares this runtime's published contract (`generated/openapi.json` and `generated/schemas/`) with the contract the runtime UI is built on. That is `ZonicAgentRuntimeUI_NextJS/contract/`, synced from the Node.js runtime at commit `b522fd6`. The comparison was made on 2026-10-28.

**Summary.**
- Every endpoint and every run-event type matches.
- Two response shapes would break the UI as it is written: the node timeline and the error details. A third, `Usage.model`, differs but nothing reads it.
- The rest are additive fields, a few renamed or missing fields, and schema strictness.

**Decisions needed from the UI owner:** items 1, 2 and 3.

## Matches

- **Endpoints:** all 20 operations, with the same methods and paths. This runtime adds `GET /v1/runs/{runId}/trace-link`.
- **Run events:** the same 21 types, with the same envelope on the wire (`id`, `runId`, `seq`, `ts`, `nodeId`, `path`, `truncated`, `data`).
- **Content parts:** the same eleven types, and the same `datasetRef` paging through `/v1/datasets/{datasetId}`.
- **Interrupts:** the same list and answer routes, with `ApprovalAnswer` and `InputAnswer` published. The answer schemas were added to `generated/schemas/answers/` in this review.

## Breaking for the current UI

| # | Where | UI expects (Node.js) | This runtime sends | Recommendation |
|---|---|---|---|---|
| 1 | `GET /v1/runs/{runId}/nodes`, items | `NodeVisitRecord`: `nodeId`, `name`, `type`, `visit`, `status`, `handles`, `durationMs`, `refs`; optional `input`, `output`, `usage` | `TimelineEntry`: `nodeId`, `nodeType`, `visit`, `status`, `startedAt`, `durationMs`, `path`; `handles` optional; no `name`, `refs`, `input`, `output`, `usage` | Align this runtime with `NodeVisitRecord`: rename `nodeType` to `type`, add `name` and `refs`, and add `input`, `output` and `usage` for permitted roles. Keep `startedAt` and `path` as additions |
| 2 | Error envelope, `error.details[]` | `ValidationErrorModel`: `path`, `constraints` | `ErrorDetail`: `path`, `message`, `type` | Pick one shape for both runtimes. `message` reads better in a form; `constraints` is what the UI's `ApiError` reads today |
| 3 | `Usage` (run results, `run.completed`) | `inputTokens`, `outputTokens`, `totalTokens`, `model` | The same tokens, plus `cost` and `modelCalls`; no `model` | No UI component reads `usage.model` today (`TraceTree` reads the observation's `model`). Drop it from the shared contract, or add it here as the last model's code |

## Additive or minor

| Where | Difference | Effect on a client |
|---|---|---|
| `NodeStartedData` | Adds `resumed` (a resumed visit after an interrupt) | None; useful for the timeline |
| `NodeFinishedData` | Adds `policy` (the error policy that applied) | None |
| `WarningData` | Adds `details` | None |
| `DatasetRef` | Adds `nodeId`, `visit`, `preview` (inline rows) | None; `preview` saves a first page request |
| `LoopIterationData` | No `startPoints` | The UI's generated type has it; no component reads it |
| `RunEvent` schema | Published as a union discriminated by `type` (one schema per event), not one envelope with an open `data` | The same JSON on the wire. A generated client gets narrower types |
| Required fields in responses | Fields with defaults (`items`, `rows`, `columns`, `pendingInterrupts`, `answerSchema`) are optional in this runtime's response schemas; the Node.js contract marks them required. Fields whose value is `null` are left out of responses | A generated TypeScript type is optional where the UI assumes present. The values are always sent when they have one |
| Tool approval answer | `{"type": "approve" \| "edit" \| "reject", ...}` with LangChain's snake-case `edited_action` | Render the form from each interrupt's `answerSchema`, as for other interrupts |
| A retried node | A second `node.started` with the same `nodeId` and `visit`; the retry streams its answer again | The UI should drop the tokens that node streamed before the repeated `node.started` ([client guide](client-integration.md)) |

## What to do next

1. The UI owner decides items 1 to 3.
2. Whichever side changes regenerates its contract: `scripts.gen` here, or `npm run sync:contract` in the UI.
3. The other side syncs. The UI's `SOURCE.json` records which runtime and commit it was synced from.
4. Point the UI's contract sync at this runtime only once the end-user switch (Q11) is decided. Until then the UI follows the Node.js runtime, and this backend is released for API use and the test client only.
