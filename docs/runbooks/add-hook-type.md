# Runbook: Add a Hook Type

The recipe, with code, is in [../extending.md](../extending.md#new-hook-type). This is the checklist.

1. **Configuration model** in `models/hooks/result.py`. Its fields are validated when bindings are resolved, so a bad binding fails the plan, not the run.
2. **Class** implementing `NodeHook` (`name`, `config`, `check`, `run`), in `hooks/builtin.py`, or in `hooks/integration.py` when it needs a port.
   - It returns a `HookResult` and never mutates its context.
   - Only `canRoute` bindings may change handles.
3. **Registration** in `builtin_hooks()` or `integration_hooks()`, or through `AppDeps.build(hook_types=...)` for a type kept outside the package.
4. **Hook points.** State which points the type supports, and refuse the others in `check`:
   - `beforeRun`, `afterRun`, `onRunError`;
   - `beforeNode`, `afterNode`, `onNodeError`;
   - `beforeModelCall`, `afterModelCall`, `beforeToolCall`, `afterToolCall`;
   - `beforeMemberCall`, `onHandoff`;
   - `onInterrupt`, `onResume`.

   For interrupt kinds, `beforeNode` allows only pure hooks.
5. **Observability.** A blocking execution is a child span of its node; an asynchronous one is an event. Use `context.run.trace_hook` or `annotate` rather than logging.
6. **Tests:**
   - unit tests for each action the type returns;
   - an app test with a binding, through `make_app(hook_types=...)`;
   - a test of `failurePolicy` and `timeoutMs`.
7. **Bindings.** Add any default bindings to `resources/hooks.json`, then run `scripts.gen` and the gates.
