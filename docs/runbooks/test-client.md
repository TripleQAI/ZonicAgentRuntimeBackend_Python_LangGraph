# Runbook: Use the Test Client

`scripts/run_client.py` is the developer test client (design 3.6). It uses only the published API, so what works here works for any client. It lists the catalog, starts a run and prints its event stream, continues a session, answers interrupts, and pages a dataset.

## Get a token

The client needs a Cognito access token of the pool the server verifies (`ZONIC_OIDC_*`). Sign in yourself, through the pool's hosted UI or the AWS CLI, and put the token in an environment variable of your own shell:

```sh
export TOKEN='<access token>'        # PowerShell: $env:TOKEN = '<access token>'
```

The token is yours: never paste it into a ticket, a document or a chat. Use a test user, so test runs and sessions are not mixed with a real user's.

**The test identity.** User `runtime-dev-verify@example.com` in the pool `strands-visual-builder-users-docker`, with its own app client (admin password sign-in), was created for the dev checks. Its password is only in the runtime's git-ignored `.env` (`DEV_VERIFY_*`). With AWS credentials for the account:
```sh
TOKEN=$(AWS_PROFILE=tripleq-development uv run python -c "from tests.live.test_deployed_live import access_token; print(access_token())")
```
The runtime accepts it because `ZONIC_OIDC_AUDIENCE` lists both the builder UI's client and this client.

## Commands

All commands are run as `uv run python -m scripts.run_client ...`. The default `--base-url` is `http://127.0.0.1:8000`.

| Goal | Arguments |
|---|---|
| List the workflows you may run | `--token "$TOKEN" --list` |
| Start a session and stream a run | `--token "$TOKEN" --workflow <agenticWorkflowId> --message "Show last month's usage"` |
| Follow up in the same session | `--token "$TOKEN" --session <sessionId> --message "And the month before?"` |
| Answer every interrupt with one answer | add `--answer '{"approved": true}'` |
| Page a dataset | `--token "$TOKEN" --dataset <datasetId> --page 2 --page-size 50 --sort kwh:desc --filter '{"borough": "queens"}'` |
| Another server | `--base-url https://<host>` |

## What it prints

- **Tokens** as they stream, and one line per other event: `[seq] type nodeId data`, with data cut to 200 characters.
- **Pauses.** When the run pauses (`run.waiting`), the pending interrupt: its kind, node, question and answer schema.
  - With `--answer`, that answer is sent.
  - At a terminal, you type the answer as JSON. An empty line stops and leaves the run waiting.
  - With neither (input piped, no `--answer`), the run is left waiting.

  After each answer the resumed stream is printed, until the run ends.
- **At the end:** `run <runId> in session <sessionId>`, one `dataset <datasetId>` line for each dataset the run's events name, and a note if the run is still waiting.

The exit code is `0` when the run completed and `1` otherwise.

## Typical answers

| Interrupt `kind` | Answer |
|---|---|
| `approval` | `{"approved": true}` or `{"approved": false, "comment": "why"}` |
| `tool_approval` | `{"type": "approve"}`, `{"type": "reject", "message": "why"}`, or `{"type": "edit", "edited_action": {"name": "<tool>", "args": {...}}}` |
| `input` | What the printed answer schema asks for |

## The M5 check

Against dev, with a test user:
1. a run that completes;
2. a run that pauses, answered with `--answer` or at the prompt;
3. a page of a dataset that a database node's run printed.

`tests/api/test_api.py` covers the same three paths offline (case A-15).
