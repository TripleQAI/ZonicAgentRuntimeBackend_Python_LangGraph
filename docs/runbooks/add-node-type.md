# Runbook: Add a Node Type

The recipes, with code, are in [../extending.md](../extending.md). This is the checklist.

1. **Choose the path:**
   - A, manifest only: the type behaves like an existing kind;
   - B, new kind: new behavior;
   - C, new multi-agent pattern.
2. **Builder.** The node type is defined in the builder repository. Re-vendor it with `uv run python -m scripts.sync_builder`, then check `scripts.sync_builder --check`.
3. **Manifest.** Add an entry under `nodeTypes` in `resources/node-runtime.json`: `kind`, `io` (ports and output map), `retry`, `timeoutSeconds`, `observation`, `optionalHandles`.
4. **Code, for paths B and C only:**
   - a `<Name>NodeConfig` in `models/nodes/configs.py`;
   - a `<Name>Kind` in `engine/kinds/`;
   - the kind added to `core_kinds()`;
   - any I/O behind a port, any new error code in `resources/error-catalog.json`, any limit in `resources/settings.json`.
5. **Tests:**
   - a fixture workflow and a scenario;
   - for path B, the kind added to `SUBJECTS` in `tests/conformance/test_kinds.py`;
   - test names carry the catalog case ID.
6. **Contract.** `uv run python -m scripts.gen` updates `generated/`. Review the diff: it is the published contract.
7. **Gates.** All of them. `tests/principles` refuses kind or type literals in `src/`.
8. **Check.** `GET /v1/meta/node-types` lists the type with its ports and output schema.
