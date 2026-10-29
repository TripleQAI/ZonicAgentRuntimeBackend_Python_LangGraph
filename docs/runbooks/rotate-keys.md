# Runbook: Rotate Keys

Every credential the runtime uses comes from its environment (`src/zonic_runtime/config/env.py`) or from a `secret:<name>` reference resolved by the secret port. Rotating one never needs a code change. It does need a rolling restart, because the environment is read once at start.

Do the rolling restart as in [deploy.md](deploy.md), section 5. Instances drain, so no run is lost.

| Credential | Where it lives | Rotation |
|---|---|---|
| Gateway API key | `ZONIC_GATEWAY_API_KEY`; caller `zonic-agent-runtime` in the gateway's key store | [Gateway key](#gateway-api-key) |
| Checkpoint encryption key | `ZONIC_CHECKPOINT_AES_KEY` | [Checkpoint key](#checkpoint-encryption-key): needs care |
| Model provider keys | `ZONIC_AZURE_OPENAI_API_KEY`, `ZONIC_OPENAI_API_KEY`, `ZONIC_ANTHROPIC_API_KEY`, `ZONIC_NEBIUS_API_KEY` (Nebius Token Factory). Bedrock uses the task role, with no key | [Provider keys](#provider-and-referenced-secrets) |
| Referenced secrets | `ZONIC_SECRET_<NAME>` (tool credentials, HTTP node credentials, callback HMAC keys) | [Referenced secrets](#provider-and-referenced-secrets) |
| MCP server token | `ZONIC_MCP_AUTH_TOKEN`; the MCP server's `MCP_AUTH_TOKEN` in SSM `/zonic-mcp-server/runtime`; on AWS, Secrets Manager `zonic-agent-runtime/<stage>/mcp-auth-token` | Change it on the MCP server first (its `deploy.sh`), then in the runtime's secret, and roll: calls fail with `TOOL_UNAVAILABLE` in between |
| Langfuse keys | `ZONIC_LANGFUSE_PUBLIC_KEY`, `ZONIC_LANGFUSE_SECRET_KEY` (optional exporter only) | Like provider keys |
| Cognito signing keys | The pool's JWKS (`ZONIC_OIDC_JWKS_URL`) | Nothing to do: see below |
| Database passwords | The gateway's `AGENTRUNTIME_DSN` and `AGENTOBSERVABILITY_DSN` | The gateway owner's procedure. The runtime holds no DSN |

Never paste a key into a document, a ticket, a test or a commit. Put new values only in the secret store, or in your own git-ignored `.env`.

## Gateway API key

1. **New key.** The gateway owner issues a new key for caller `zonic-agent-runtime`. Ask whether the gateway accepts the old and the new key at the same time.
2. **Roll.** Put the new key in the runtime's secret store and roll the instances.
3. **Revoke.** Once every instance runs with the new key, the gateway owner revokes the old one.
   - If the gateway accepts only one key at a time, instances still on the old key get `401` from the gateway until they are replaced. Their requests fail with `DATA_REQUEST_INVALID`. Do the switch in a quiet window.

**Check:** `GET /readyz` alone may not show a refused key, so run a full run with the [test client](test-client.md) after the roll.

## Checkpoint encryption key

When `state.encryption.enabled` is on (prod), checkpoints are encrypted with AES-GCM under this key. LangGraph's `EncryptedSerializer` uses one key only. A checkpoint written under the old key cannot be read under a new one: a run paused or drained before the change fails when it resumes or is claimed.

Rotate only when no checkpoint is outstanding:
1. **Stop new pauses.** In a quiet window, stop starting runs that pause, or accept that the few that do will need to be started again.
2. **Wait for checkpoints to clear.** The count below must reach zero: waiting runs are answered or expire (`interrupts.defaultExpiryHours`, 72), and queued `async` runs finish. Read-only, with direct access to the database:
   ```sql
   SELECT r.status, count(*)
   FROM agent_runtime.runtime_checkpoint c
   JOIN agent_runtime.runtime_run r USING (run_id)
   WHERE r.status IN ('waiting_input', 'queued', 'running')
   GROUP BY r.status;
   ```
3. **Generate a key.** `python -c "import secrets; print(secrets.token_hex(32))"` gives 64 hex characters. Put the new value straight into the secret store, not into a shell history or a file.
4. **Roll** the instances.

**Supporting both keys:** reading with the old key while writing with the new one would remove the wait. That needs a code change, which is not built: a decrypting serializer that tries the old key second. Until then, a rotation that cannot wait means asking the users with waiting runs to start them again.

## Provider and referenced secrets

1. **Issue** the new key at the provider, so the old and new keys work at the same time.
2. **Roll.** Update the variable in the secret store and roll the instances.
3. **Revoke** the old key at the provider.

Callback HMAC keys (`options.callback.secretRef`) are shared with the receiver of the callback. The receiver must accept signatures from both keys until the roll is done. Only then does the runtime sign with the new key alone.

## Cognito signing keys

The runtime caches the pool's JWKS for `auth.jwksCacheSeconds` (3,600 s). It fetches the JWKS again at once when a token names a key ID it does not know. A Cognito key rotation therefore needs no action. If the JWKS cannot be fetched, requests fail with `AUTH_INVALID` ("keys unavailable") until it can.
