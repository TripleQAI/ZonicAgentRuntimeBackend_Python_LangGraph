# 0003: Design deviations found while building Phase 1

- **Status:** accepted at the M1 review (2026-10-26). The reviewer delegated these decisions to the builder.
- **Decides:** six places where Phase 1 differs from the design or the plan. Each is described in `docs/build-log.md` under the task named below.
- **Amended:** the design (Sections 6.2, 6.4, 6.5, 7.1, 8.2) and the plan (P1-07), in both the Markdown and HTML copies.

## 1. The fan-in rule (P1-05; design 6.2, 6.5)

**Design:** a join is a barrier when it is reached along paths that "do not pass an exclusive branch point" between the fan-out and the join.

**Decision:** a join is a barrier when it has two or more predecessors on paths that *diverge at one parallel fan-out*: one `(node, handle)` with two or more targets, or `Send`. An exclusive branch further down one of those paths does not remove the barrier.

**Why:** that branch may be taken. Without `defer` the join would then run once for each superstep in which a branch arrives, which is the double execution the barrier exists to prevent. `test_D15_an_exclusive_branch_inside_a_parallel_branch_keeps_the_barrier` holds the case, and the G-09 property test checks the rule against a reference interpreter. Joins after exclusive branches only (16 of 21 builder workflows) are unchanged: they are not barriers.

**Also accepted:** a join after parallel branches of equal length is marked a barrier too. It still runs once with both outputs, and the blueprint needs no proof that the branches arrive together.

## 2. A handle with no edge (P1-05, P1-09; design 6.4 point 3)

**Design:** a handle with no edge is a blueprint error unless the manifest marks it optional.

**Decision:** it is the warning `HANDLE_NO_EDGE`. The run fails with `ROUTE_UNRESOLVED` only when the node takes that handle. Taking it ends the path instead when the handle is optional, or when the node has no outgoing edges at all (a sink, such as a last node that is not an Output).

**Why:** D-08 already specifies "warn, fail only when taken" for rule branches, and builder workflows routinely leave a handle unconnected (an approval with no `rejected` edge). As an error, valid builder workflows would fail to plan.

## 3. Three parts in the effective output schema (P1-07; design 8.2)

**Design:** the effective output schema is `allOf` of four parts: the payload, the builder's vendored `outputSchema`, the manifest's `schemaExtensions` and the node's own `outputSchema`.

**Decision:** three parts. The builder's vendored `outputSchema` is not composed. Contract test I-02 holds it instead: every builder field is declared by the runtime payload with an equal or narrower type. This is what the Node.js runtime does.

**Why:** the builder types some fields more loosely, or differently, than the runtime produces them. Human Input `value` is a string in the builder, but form answers are objects. Composing the builder schema would reject correct outputs.

## 4. Where the template checker lives (P1-07; plan P1-07)

**Plan:** `io/template_checker.py`.

**Decision:** `workflow/blueprint/template_checker.py`, which is where design 18.1 already places it. `workflow/loader/paths.py` moves to `runtime/paths.py`, so every layer can use it.

**Why:** the checker needs the template resolver in `workflow/`, and `io` sits below `workflow` in the import contracts of design 4.3. The port binder takes `resolve` and `read` callables for the same reason.

## 5. `system` is a keyed channel (P1-08; design 7.1, DR-4)

**Design:** `system` has its own merge reducer, `merge_writable_system`.

**Decision:** `system` uses the same keyed channel as `flow`: `{values, provenance, conflicts}`, where the highest `(step, writer)` wins. DR-4's enforcement stays as designed: `to_update` raises `STATE_READ_ONLY` for a key outside `writableSystemKeys` before the update leaves the node.

**Why:** a plain dictionary merge depends on order when two parallel nodes write the same writable key. That would break the determinism DR-1 asks for.

## 6. Document limits (P1-11; design 6.4 point 4)

**Decision:** `WorkflowDocument.limits` (`maxVisitsPerNode`, `maxSteps`) is read and capped by `settings.engine.maxLimits`, as design 6.4 says. It stays a runtime-only key: the builder neither writes nor validates it, and no fixture has one. The Node.js runtime reads the same key.

**Consequence:** until the builder adds a field for it, every workflow runs with the settings' limits. No builder change is requested in Phase 1.
