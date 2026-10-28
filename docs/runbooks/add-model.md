# Runbook: Add a Model

Models are configuration: one entry in `resources/models.json`. A model a document names that is not in the catalog, or not enabled, is refused with `MODEL_NOT_ALLOWED`.

## Steps

1. **Entry.** Add the model under `models`, keyed by its catalog code:
   ```json
   "claude-sonnet-4-5": {
     "provider": "bedrock_converse",
     "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "enabled": true,
     "region": "us-east-1",
     "supportsNativeStructuredOutput": false,
     "parameters": { "maxTemperature": 1.0, "maxTokens": 16000 },
     "quota": { "maxInFlight": 20, "requestsPerMinute": 200, "tokensPerMinute": 400000, "maxQueueWaitMilliseconds": 20000 }
   }
   ```

   **Providers** (`ModelProvider` in `models/resources/catalogs.py`):

   | `provider` | Extra fields | Credentials |
   |---|---|---|
   | `bedrock_converse` | `region` | The task role. One shared client per region, sized to `models.executorThreads` |
   | `azure_openai` | `deployment`, `apiVersion`, `endpointEnv` (the environment field with the endpoint, for example `azure_openai_endpoint`) | `ZONIC_AZURE_OPENAI_API_KEY` |
   | `openai` | `endpointEnv` for an OpenAI-compatible endpoint | `ZONIC_OPENAI_API_KEY` |
   | `anthropic` | none | `ZONIC_ANTHROPIC_API_KEY` |

   - **`parameters`** bounds what a document may ask for: temperature and output tokens are clamped to these.
   - **`quota`** is enforced per instance by the model governor. Calls over `maxInFlight` wait up to `maxQueueWaitMilliseconds`, then fail with `MODEL_BUSY`. Size it to the account's provider quota divided by the instance count.
   - **`supportsNativeStructuredOutput`**: when true, `responseFormat: json` nodes with a schema use the provider's structured output instead of the prompt-and-parse path.
2. **Aliases.** If builder documents name the model by another string, such as a Bedrock model ID or an older name, map that string to the catalog code under `aliases`. Model names are checked when a node runs, not when a document loads. Search the documents in use, and the vendored ones under `tests/fixtures/workflows/`, for the model strings they name. For example, the live Facility Operations document names `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, an alias of `claude-sonnet-4-5`.
3. **Default.** Change `defaultModel` only on purpose. It applies to every node that names no model.
4. **Stand-ins.** `substitutes` maps a catalog code to the code that serves it while its own provider is unavailable. Documents keep naming their model, and the stand-in answers. The AWS deployment uses this (`deploy/aws/resources/models.dev.json`): the Claude codes are served by `openai-gpt-4.1` and `openai-gpt-4.1-mini` until Bedrock's Anthropic access is in place. To return to Bedrock, empty that file's `substitutes` and redeploy.
5. **Validate.** `uv run python -m scripts.gen --check`, and the tests under `tests/unit` that load the resources. `ModelCatalog` refuses aliases that point at unknown codes, and a `defaultModel` that is not an entry.
6. **Live check.** On dev, run a workflow that uses the model with the [test client](test-client.md). For Bedrock, the account must have access to the model in that region. Ask before the first call to a new paid model.

## Removing a model

Set `enabled: false` first, and look for documents that still name it. Each run of such a document fails with `MODEL_NOT_ALLOWED` until the document is changed or an alias points at a replacement.
