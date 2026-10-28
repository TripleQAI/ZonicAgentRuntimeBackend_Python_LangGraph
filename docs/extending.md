# Extending the Runtime

Recipes for the three ways a new node type enters the runtime (design Section 17), and for hook types. Every recipe ends with the same gates as any change (see [CLAUDE.md](../CLAUDE.md)).

Node kinds are named in exactly one place: `resources/node-runtime.json`. Code finds a kind's class and configuration model from class names: `VectorSearchKind` serves the kind `vector_search`, and `VectorSearchNodeConfig` configures it. So `src/` never contains a node type or kind literal (rule 1, checked by `tests/principles`).

## Which path

| Path | Use it when | You write |
|---|---|---|
| A. Manifest only | The type behaves like an existing kind: a "Summarizer" that is an LLM with a preset prompt, a "SQL Report" on `mcp_execute`; or a type needs a new port or output field | A manifest entry and a fixture |
| B. New node kind | The type has new behavior, for example a vector search | One configuration model, one kind class, a manifest entry, fixtures |
| C. New multi-agent pattern | A container with another collaboration pattern: a supervisor team, agents as tools, a router team | One pattern strategy for the `multi_agent` kind |

What keeps a new type compatible without compiler changes:
- **Routing is generic.** The builder registry gives the handles, and the node template turns them into `Command(goto=...)`.
- **Configuration, input and output are generic.** Ports and output maps come from the manifest.
- **Hooks match by kind, type, name, workflow or tag.**
- **Checks run automatically.** `GET /v1/meta/node-types` lists the new type, the builder comparison checks the registry against the manifest, and the conformance suite (C-01 to C-16) runs every kind inside a compiled graph.

## Path A: manifest only

1. **Builder side.** The builder repository defines the node type's JSON, including preset configuration such as the Summarizer's prompt: the builder writes it into each node's `config`. Re-vendor the registry with `uv run python -m scripts.sync_builder`. It updates `resources/builder-registry.json` and the vendored workflow fixtures.
2. **Manifest entry.** Add the type under `nodeTypes` in `resources/node-runtime.json`. Copy the entry of the kind it behaves like and change what differs. Unknown keys are refused (the model is `NodeTypeEntry` in `models/resources/manifest.py`).
   ```json
   "summarizer": {
     "kind": "llm",
     "io": {
       "inputs": [
         { "name": "prompt", "schema": { "ref": "port.text@1" }, "required": true,
           "binding": { "configPath": "config.userQueryMapping", "template": "{{system.userQuery}}" } }
       ],
       "output": { "schema": { "ref": "zonic.text@1" }, "map": { "text": "$.text", "...": "$.fields" } }
     },
     "streamTokens": true,
     "retry": { "maxAttempts": 3, "initialIntervalSeconds": 0.5, "backoffFactor": 2.0,
                "maxIntervalSeconds": 8, "jitter": true, "on": ["MODEL_UNAVAILABLE", "MODEL_BUSY", "TIMEOUT"] },
     "timeoutSeconds": 120,
     "observation": { "type": "generation", "captureInput": "full", "captureOutput": "full" }
   }
   ```
   - `io.inputs`: each port's schema (from the schema registry) and its binding: `configPath`, `statePath` or `template`.
   - `io.output.map`: JSONPath from the kind's raw result to the node's output fields. `"..."` spreads a mapping.
   - `optionalHandles`: handles the type may activate beyond `default` (for example `error`).
   - Also available: `options`, `handles`, `timeoutSecondsFrom`, `idleTimeoutSeconds`, `errorPolicy`, `joinPolicy`, `terminal`, and `enabledBy`, which names a feature flag in `settings.features`.
3. **Fixture.** Add a workflow with the new type under `tests/fixtures/workflows/` and a scenario or compiler test that runs it. Name the test after the catalog case it proves (`test_G10_...`).
4. **Gates.** `uv run python -m scripts.gen` regenerates `generated/` when the manifest's schemas change. Then run `scripts.gen --check`, `scripts.sync_builder --check` and the tests.

## Path B: new node kind

Example: a `vector_search` kind that queries an index and returns the matching passages.

1. **Builder side and manifest.** Do steps 1 and 2 of path A, with `"kind": "vector_search"`.
2. **Configuration model.** Add it to `src/zonic_runtime/models/nodes/configs.py`. Template-valued fields carry `TemplateField()`; credentials carry `SecretRef()` and accept only `secret:<name>` references:
   ```python
   class VectorSearchNodeConfig(NodeConfigBase):
       index: str | None = None
       query: Annotated[str | None, TemplateField()] = None
       top_k: int | None = None
       api_key: Annotated[str | None, SecretRef()] = None
   ```
   The class name gives the kind (`config_models()` in `models/nodes/registry.py`). Unknown keys are kept and reported, so the builder's round trip is lossless.
3. **Kind class.** Add it in `src/zonic_runtime/engine/kinds/`. It implements only `execute`, reads its validated input and the run through `NodeContext`, and keeps no run data on `self`: one instance serves every run of a compiled graph (rule 14, case C-15).
   ```python
   class VectorSearchKind(BaseKind):
       def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
           self.spec, self.blueprint = spec, blueprint

       async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
           config = self.spec.config
           # `vectors` stands for the port you add for the index (step 3's first bullet)
           hits = await ctx.services.ports.vectors.search(config.index, node_input.get("query"), config.top_k)
           handle = ctx.services.resources.manifest.defaults.default_handle
           return NodeOutcome(output={"passages": hits}, activate=[handle])
   ```
   - I/O goes through a port (`src/zonic_runtime/ports/`, a `Protocol`) with an adapter in `integrations/`, so tests use a fake. URLs belong in `config/env.py` (rule 3). Limits and timeouts belong in `resources/settings.json` (settings models have no code defaults, M-02).
   - Errors are `ZonicError("<CATALOG_CODE>", ...)`. A new code goes into `resources/error-catalog.json`.
   - Class flags:
     - `interrupts_first`: the kind asks a human before it does anything (with `request`, `answer_model` and `response`);
     - `holds_permit = False`: the kind contains other nodes;
     - `owns_deadline`: the kind enforces its own timeout.
   - Child graphs run only through `invoke_child`.
4. **Registry.** Add the class to `core_kinds()` in `src/zonic_runtime/engine/kinds/core.py`. For a kind kept outside the package, pass `core_registry().with_kinds({...})` where the runtime is built (`WorkflowRuntime(kinds=...)`).
5. **Conformance and fixtures.** Add the kind to `SUBJECTS` in `tests/conformance/test_kinds.py`, so C-01 to C-16 run it in a one-node graph and in a join graph. Add a fixture workflow and a scenario that uses the node.
6. **Gates.** Run `scripts.gen`, then all the gates. `lint-imports` keeps `engine/` from importing `api/` or concrete integrations.

## Path C: new multi-agent pattern

The `multi_agent` kind (`engine/kinds/swarm_kind.py`) builds its members' subgraph. The Swarm is the pattern shipped today (`engine/agents/swarm/`).

1. **Builder side.** A `swarms`-style semantics entry for the container in the builder's `node_semantics.json`. Re-vendor it with `scripts.sync_builder`.
2. **Manifest entry.** `{"kind": "multi_agent", "options": {"pattern": "<name>"}}`.
3. **Pattern strategy.** Add it next to `engine/agents/swarm/`. It builds a `StateGraph` of the members, as `build_swarm` does, with LangChain's subagent, router or custom workflow patterns. Reuse what the Swarm already provides:
   - `MemberNode`, for the `beforeMemberCall` and `onHandoff` hooks and the `swarm.*` events;
   - `SwarmLimits`, for step and repetition limits;
   - `member_context`, for the shared and handoff context modes.

   Hand control over with `Command(goto=..., graph=Command.PARENT)`, never with a direct call.
4. **Selection.** `MultiAgentKind.execute` refuses any `options.pattern` other than `swarm` with `NODE_TYPE_UNSUPPORTED`. Make it choose the strategy by pattern instead. The pattern names are option values, so each needs a `# principle: allow rule-1-node-type <reason>` waiver, as `SWARM_PATTERN` has.
5. **Tests.** Write a scenario with a golden stream, like `tests/api/test_swarm.py`. Cover the limits, the deadline (`SWARM_TIMEOUT`) and the member events.

## New hook type

A hook type is one class with a configuration model. Bindings in `resources/hooks.json`, a document's `runtimeHooks` or a node's `properties.hooks` use it without any compiler or kind change (case K-16).

1. **Configuration model.** Add it in `src/zonic_runtime/models/hooks/result.py`, next to `TransformHookConfig`.
2. **Class.** Write it in `src/zonic_runtime/hooks/builtin.py`, or in `integration.py` when it needs a port. It implements `NodeHook`:
   ```python
   class TagHook(BaseHook):
       name: ClassVar[str] = "tag"
       config: ClassVar[type[BaseModel]] = TagHookConfig

       def check(self, config: Any) -> str | None:  # problems the model cannot express
           return None if config.tags else "tags is empty"

       async def run(self, context: HookContext, config: Any) -> HookResult:
           context.run.annotate(config.tags, {}, None)
           return HookResult()
   ```
   - A hook reads copies through `HookContext`. It never mutates them; it returns changes in `HookResult.patch`, `handles` or `updates`.
   - Actions: `continue`, `skip`, `fail`, `retry`, `recover` and `cancel`. Only bindings with `canRoute` may change handles at `afterNode`.
3. **Registration.** Add the class to `builtin_hooks()` or `integration_hooks()`. An application that embeds the runtime can pass `AppDeps.build(hook_types=[...])` instead.
4. **Binding** (in `resources/hooks.json`):
   ```json
   { "bindings": [
     { "id": "tag-billing", "hook": "tag", "on": ["afterNode"],
       "match": { "nodeTypes": ["agent"] }, "mode": "blocking", "failurePolicy": "ignore",
       "config": { "tags": ["billing"] } } ] }
   ```
   `match` also takes `nodeKinds`, `nodeNames`, `workflowIds` and `tags`. Other binding fields: `when`, `order`, `timeoutMs`, `canRoute`, `overridable` and `enabled`.
5. **Tests.** A unit test for each action, and an app test that binds the hook through `make_app(hook_types=...)`. Then `scripts.gen` (the bindings schema) and the gates.
