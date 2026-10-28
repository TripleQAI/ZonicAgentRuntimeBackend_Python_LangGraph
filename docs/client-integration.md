# Client Integration Guide

This guide is for whoever builds a client of the runtime API. That includes the deferred runtime UI (`ZonicAgentRuntimeUI_NextJS`, design Section 3.6) and any service that starts runs. The design's Section 14 is the specification. This guide covers how to use it.

## Contract files

Everything a client depends on is generated from the Pydantic models and committed under `generated/`. CI fails when `generated/` is out of date (`scripts.gen --check`), and the contract snapshot test fails on any unreviewed change.

| File | What it is |
|---|---|
| `generated/openapi.json` | OpenAPI 3.1: every endpoint, request and response, the error envelope |
| `generated/schemas/events/RunEvent.schema.json` | The union of every run event; one schema per event type beside it |
| `generated/schemas/content/*.schema.json` | Content parts: `markdown`, `table`, `grid`, `chart`, `card`, `json`, `file`, `html`, `citation`, `error`, `form` |
| `generated/schemas/index.json` | The index of all generated schemas |

Generate a typed client from these files rather than writing the types by hand. Field names on the wire are camelCase. Requests with unknown fields are refused (`INPUT_INVALID`). A breaking change needs a new version under `/v1` and a review.

The interactive API docs (`/docs`) are served only when `api.docsEnabled` is on, which is true in development settings only.

## Authentication

- **Token.** Send `Authorization: Bearer <access token>`, a Cognito access token from the pool the server is configured with (`ZONIC_OIDC_ISSUER`, `ZONIC_OIDC_AUDIENCE`, `ZONIC_OIDC_JWKS_URL`). The Cognito token is the only access control (decision 0004).
- **Claims** (`settings.auth.claims`). The subject is `sub`, the tenant `custom:tenant`, groups `cognito:groups`. The groups in `auth.internalMessageRoles` (`runtime-support`, `runtime-admin`) may read internal messages (`visibility=internal`) and node inputs and outputs.
- **Owner scope.** Every session, run, message and dataset belongs to its owner. Another user's ID is answered exactly like a missing one: `404 NOT_FOUND`, or `DATASET_NOT_FOUND` for datasets. Do not treat a `404` as proof that an ID never existed.
- **CORS.** Allowed origins are listed in `api.corsOrigins`. The exposed headers are `ETag`, `Retry-After`, `X-Request-Id`, `X-Run-Id` and `X-Session-Id`.

## Errors

Every error uses one envelope:

```json
{ "error": { "code": "INPUT_INVALID", "message": "...", "details": [{ "path": "input.text", "message": "...", "type": "..." }], "traceId": "...", "retryable": false } }
```

- **Codes.** The `code` comes from `resources/error-catalog.json`. Branch on `code`, never on `message`. Messages are safe to show to users and contain no internal details.
- **Retries.** `retryable` says whether repeating the request may succeed. `RATE_LIMITED` (429) comes with `Retry-After`.
- **Request body limit.** A body above `api.maxBodyBytes` (1 MiB) is `413 INPUT_TOO_LARGE`, with or without a `Content-Length` header.

## Starting a run

```http
POST /v1/runs
Idempotency-Key: 7b0f...            (optional; the same key returns the same run for 24 hours)

{ "agenticWorkflowId": "wf-...", "input": { "text": "Show last month's usage" }, "mode": "stream" }
```

`GET /v1/catalog/agents` lists the workflows the caller may run. To continue a conversation, send `sessionId` instead of `agenticWorkflowId`. Without a `sessionId`, the runtime creates a session.

| Mode | Answer | Use it for |
|---|---|---|
| `stream` | `200 text/event-stream`; the IDs are in `X-Run-Id` and `X-Session-Id` | Chat: tokens as they come |
| `sync` | `200 RunResult`, or `202 RunAccepted` after `runs.syncWaitSeconds` (30) | Short calls from services |
| `async` | `202 RunAccepted` with `links.self` and `links.events` | Long or queued work. The run is durable and survives instance restarts. `options.callback` (`{url, secretRef}`) has the summary posted, signed in `X-Zonic-Signature` |

**Polling.** `GET /v1/runs/{runId}` returns the status and result with an `ETag`. Send `If-None-Match` to get `304` while nothing changed. A run still in progress also carries `Retry-After`. The statuses are listed in `resources/run-states.json`: `queued`, `running`, `waiting_input`, `succeeded`, `failed`, `cancelled` and `timed_out`.

**Cancelling.** `POST /v1/runs/{runId}/cancel`. A stream run ends with `run.cancelled`. A closed connection does not cancel the run (`runs.cancelOnDisconnect` is off), so reconnect instead.

## Streaming and resume

Each SSE frame carries one event. The frame's `id:` is the event's `seq`, and its `data:` is the envelope:

```json
{ "id": "...", "runId": "...", "seq": 12, "type": "token.delta", "ts": "2026-10-27T01:59:11.256Z", "nodeId": "agent_1", "data": { "text": "Hel" } }
```

**Event types:**
- run: `run.started`, `run.waiting`, `run.completed`, `run.failed`, `run.cancelled`;
- nodes: `node.started`, `node.finished`, `loop.iteration`;
- text: `token.delta`, `reasoning.delta`;
- tools: `tool.started`, `tool.finished`;
- swarm: `swarm.member.started`, `swarm.member.finished`, `swarm.handoff`;
- other: `content.part`, `state.updated`, `interrupt.requested`, `warning`, `stream.gap`, `heartbeat`.

How to handle them:
- **The run ends** with exactly one of `run.completed`, `run.failed`, `run.cancelled` or `run.waiting`, and the stream then closes.
- **Heartbeats** arrive every `sse.heartbeatSeconds` (15). Treat about two missed heartbeats as a lost connection.
- **Reconnect** with `GET /v1/runs/{runId}/events` and `Last-Event-ID: <last seq you processed>`. The runtime replays from its buffer (the last `stream.replayBufferEvents` events, 5,000, kept `stream.replayRetentionSeconds`, 300, after the run ends) and then continues live, without duplicates. This works from any instance: a run driven elsewhere is proxied.
- **`stream.gap`** (`fromSeq`, `toSeq`) means events were lost from the buffer. Reload the state the gap hides: `GET /v1/runs/{runId}` for the status and result, and `GET /v1/sessions/{id}/messages` for the conversation.
- **`seq`** strictly increases per run from 1 and continues across a resume.
- **`path`**, when present, gives the enclosing nodes of an event from inside a sub-flow, as `/`-joined node IDs (for example `sub_flow_1`). `nodeId` is the node within that sub-flow. Swarm token and tool events also name their member in `data`.
- **`truncated: true`** means the event's data was cut to `sse.maxEventBytes` (64 KiB). Read the full output from the run or the node timeline.
- **`token.delta`** text is coalesced (about 16 ms or 1 KiB), so fragments do not align with words.
- **A retried node** gets a second `node.started` with the same `nodeId` and `visit`, for example after a model stream stalled and was retried. Discard the tokens that node streamed before the repeated `node.started`: the retry streams its answer again from the start.

## Interrupts (human input and approvals)

A run that needs a person pauses. Its stream ends with `interrupt.requested` events and then `run.waiting` (`data.interruptIds`), and the run's status is `waiting_input`. The run waits without holding any server resources, for as long as the interrupt's `expiresAt` allows.

1. **Read** the pending interrupts with `GET /v1/runs/{runId}/interrupts`. Each one has:
   - `interruptId`, `nodeId` and `kind`: `approval`, `input` or `tool_approval`;
   - `prompt.text`: the question, with templates resolved;
   - `answerSchema`: a JSON Schema the answer must match;
   - `uiSchema`: rendering hints;
   - `requestedAt` and `expiresAt`.
2. **Render** a form from `answerSchema` and `uiSchema`. Do not hard-code forms per node type. Typical answers:
   - Approval: `{"approved": true, "comment": "ok"}`. The `readOnly` fields `decidedBy` and `expired` are set by the server.
   - Tool approval: `{"type": "approve"}`, or `{"type": "edit", "edited_action": {"name": "<same tool>", "args": {...}}}`, or `{"type": "reject", "message": "why"}`. These are LangChain's decision names, in snake case.
   - Human input: whatever the node's `answerSchema` declares.
3. **Answer** with `POST /v1/runs/{runId}/interrupts/{interruptId}` and the body `{"answer": ..., "mode": "stream"}`. The run resumes in the mode you give, as with `POST /v1/runs`. Nodes that already ran do not run again.
4. **More interrupts.** A run can pause several times, and one pause can hold several interrupts. Answer each one; after every answer, read the list again.

| Answer error | Meaning |
|---|---|
| `400 INTERRUPT_ANSWER_INVALID` | The answer does not match `answerSchema`. Show the validation message and ask again |
| `409 INTERRUPT_ALREADY_ANSWERED` | Someone answered first. Reload the run |
| `410 INTERRUPT_EXPIRED` | Too late. The run took its expiry path, so reload it |

## Content parts and datasets

The server never sends executable content: every content part is data, validated against its schema.

- **`html`**: values are escaped, and scripts, handlers, `javascript:` URLs and external resources are removed. Render it without script permission anyway, for example in a sandboxed `iframe` with no `allow-scripts`.
- **`chart`**: an ECharts option object with allow-listed keys and no functions.
- **`grid`** (and large `table` results): carries a `datasetRef` with a `datasetId`, plus a few inline preview rows. The full rows stay on the server.

**Paging a dataset.** `GET /v1/datasets/{datasetId}?page=2&pageSize=50&sort=kwh:desc&filter={"borough":"queens"}`:
- `page` starts at 1, and `pageSize` is at most `api.maxPageSize`.
- `sort` is `column` or `column:desc`. Sorting is natural, so `"10"` comes after `"9"`.
- `filter` is a JSON object mapping each column to text its value must contain, ignoring case. All entries must match.
- The answer is `{datasetId, kind, rows, total, page, pageSize, columns}`.

A dataset belongs to the run's owner and session. Another user gets `404 DATASET_NOT_FOUND`.

## Sessions, history and feedback

- **Listing.** `GET /v1/sessions` (cursor paging, title search) and `GET /v1/sessions/{id}/messages` (keyset paging, newest first) give the conversation.
- **Messages** carry their content parts. `textPreview` gives a short preview.
- **Changes.** Rename or archive a session with `PATCH /v1/sessions/{id}`. Rate an answer with `POST /v1/messages/{messageId}/feedback` (`rating`: -1 or 1 for thumbs, 1 to 5 for stars; optional `comment`).
- **Node details.** `GET /v1/runs/{runId}/nodes` gives the node timeline. Permitted roles also see node inputs and outputs. `GET /v1/runs/{runId}/trace` gives the trace tree.

## A reference client

`scripts/run_client.py` uses only this API: catalog, stream, resume with answers, and dataset paging. Its `RuntimeClient` class is a compact example of every flow above. See [runbooks/test-client.md](runbooks/test-client.md).
