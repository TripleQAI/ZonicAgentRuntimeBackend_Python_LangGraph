# 0006: The recorded responses are vendored from the Node.js runtime

- **Status:** decided under the delegated design deviations (M1: "you should decide Design deviations"), 2026-10-27; for review at M3
- **Concerns:** plan task P3-12; case F-09; open item D-7
- **Deviates from:** the P3-12 row, which has this project write `scripts/record_fixtures.py` and record against `sample_open_data` itself

## Context

P3-12 asks for an opt-in live script that runs the open-data fixtures' database tools and DB Query nodes against `sample_open_data` as `sample_open_data_reader` and their HTTP calls through the egress guard. It writes `tests/fixtures/recordings/<workflow>/<tool>.<parameter hash>.json`, which the fakes replay (F-09).

The Node.js runtime has done exactly this (its P3-12, `npm run record:fixtures`): 25 recordings and `contexts.json`, committed in that repository. They are keyed by tool name, the parameter values the MCP server resolves, and a hash of the tool's request template. None of that depends on the runtime.

Writing and running a second recorder here would need:

- a PostgreSQL driver in this project, which has none (the runtime reaches data only through the gateway, rule 5);
- the DB Query kind, which is P3-04 and not built yet;
- new live access to `sample_open_data`. The DSN approval given in the Node.js session covered that task only.

Two runtimes recording separately would also drift: the same call could get different answers in each, and parity tests would then compare different data.

## Decision

- **Vendoring:** `scripts/sync_recordings.py` vendors the Node.js `tests/fixtures/recordings/` byte for byte into `tests/fixtures/recordings/`, with a `SOURCE.json` that records the commit and the SHA-256 of each file. `--check` fails when a vendored file changed, or when the Node.js repository (when present next to this one) changed or gained or lost files since.
- **The recorded documents travel with the recordings:** a recording's template hash comes from the document it was recorded against. This project's `open-data/fsh-energy-agent.json` is an older export of its own product database (agent-build-langgraph, hands-off). It holds 4 of the 5 recorded templates; the Node.js copy has since replaced `fn_get_bill_summary` with an NYC Open Data call and the Django-only function tool with an httpbin stand-in. The script therefore also vendors the four documents of the Node.js `SOURCES` into `tests/fixtures/recordings/documents/`.
- **The lookup is ported exactly** (`tests/fakes/recordings.py`, `integrations/mcp/tool_parameters.py`): canonical JSON, the 16-character hashes, file names, `resolveToolParameters`, `resolveServerTokens` and `templateAliases`. A test checks that every vendored recording is found by the key this runtime computes.
- **Replay:**
  - `FakeMcpServer` answers from the recordings, with the Node.js fake's envelopes, and keeps what `saveToCache` tools store.
  - `FakeEgress` replays the HTTP recordings.
  - An unrecorded call fails the test.
- **Re-recording** happens in the Node.js repository (`npm run record:fixtures`), followed by `uv run python -m scripts.sync_recordings` here. This project has no `scripts/record_fixtures.py`.

## Consequences

- F-09 runs now for `fsh-energy-agent` (the recorded open-data copy and the Zonic SQL Server copy through template aliases).
- The `fsh-facility-agent` and `fsh-risk-agent` HTTP scenarios need the DB Query kind to plan, so they join with P3-04. Their HTTP recordings are replayed by a `FakeEgress` test now.
- D-7 is unchanged: the live M3 check against the real MCP server still needs the `sample_open_data` connection on the MCP server.
- If this runtime ever needs a recording that the Node.js fixtures do not make, the Node.js `contexts.json` is the place to add it.
