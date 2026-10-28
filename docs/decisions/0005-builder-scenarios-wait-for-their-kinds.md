# 0005: Builder scenarios join with the tasks that build their node kinds

- **Status:** accepted with the M2 approval (2026-10-27)
- **Concerns:** plan task P2-09; cases F-08, F-10, F-17, F-18, B-12 and B-13; the text of checkpoint M2
- **Amended:** the plan's P2-09 row, the "Proves" column of P3-03, P3-04, P3-06, P3-07, P3-08 and P3-09, and the M2 checkpoint, in both the Markdown and HTML copies; then `tests/catalog.yaml` regenerated

## Context

P2-09 depends only on P1-03, P1-11, P2-02 and P2-03. Its scenarios (plan Section 20.4) and the M2 checkpoint expect the builder fixtures and the platform workflows to run end to end. But Phase 2 builds only the `entry`, `llm`, `agent`, `router`, `variable`, `output` and `ignore` kinds, and of the 32 vendored workflows only three plan with them alone:

| Fixture | Waits for |
|---|---|
| `templates/question_answering_with_tools` | runs now (the calculator variant needs the built-in tools of P3-02) |
| `zonic/test-agent-workflow`, `zonic-versioned/test-agent-workflow` | run now (DB Query is unreachable) |
| `invalid/new-workflow` | refused with `PLAN_INVALID`, as specified |
| `fsh-energy-agent`, `fsh-facility-agent-v2` (both dialects, and open-data) | `view` (P3-08): every path ends in a View node |
| `fsh-facility-agent`, `fsh-risk-agent` | `http` (P3-03), `mcp_execute` (P3-04), `view` (P3-08) |
| `all-agents`, `test-relationship`, `django/complex-agent` | `subflow` (P3-09) |
| the nine platform workflows | `tool` (P3-06); `catalogue-assistant` and `intelligent-buying-orchestrator` also `sandbox` (P3-07), the orchestrator `subflow` (P3-09) |
| `approval_flow`, `customer_support_swarm`, `facility_operations_swarm` | `interrupt` (P4-02), `multi_agent` (P4-01); already planned for those tasks |

So F-08 (routing and joins on the fsh fixtures), F-10 (sub-flows), F-17 and B-12 (`agent.json`, the orchestrator) and F-18 and B-13 (the platform sub-agents' tool loops) cannot pass in Phase 2 with the real kinds.

## Proposal

- **P2-09 delivers** the scenario framework (`tests/scenarios/builder/`), the scenarios that run with Phase 2 kinds in stream and sync mode with golden streams (F-07), the refused `new-workflow`, and the coverage gate.
  - The gate computes each vendored workflow's compiled node kinds.
  - It fails when a workflow that runs with today's kinds has no scenario.
  - It also fails when a workflow waits for a kind whose task is already complete. Each later task therefore has to add its scenarios.
- **The other cases move to the tasks that build their kinds:**
  - F-08 to P3-03, P3-04 and P3-08;
  - F-10 to P3-09;
  - F-17 and B-12 to P3-06, P3-07 and P3-09;
  - F-18 and B-13 to P3-06.
- **M2 would read:** "Every builder fixture whose node kinds exist runs in stream and sync modes against the scripted model (F-07); the scenario coverage gate passes". The remaining fixtures then become a checkpoint M3 item.

## Alternative

Run the routing and loop scenarios now with test stand-ins for the missing kinds: an echo kind for View, HTTP and DB Query, and a scripted kind for Tool. This would check routing and joins on the real fixture graphs at M2.

It is not proposed, for three reasons:

- Golden streams recorded with stand-ins would pin the stand-ins' events and have to be re-recorded when the real kinds land.
- F-17 and F-18 check what tool results do to `flow` and to routing, which a stand-in would only imitate.
- The routing and join mechanics themselves are already proven on synthetic fixtures (the E cases), and the compiled structure of every fixture is pinned by F-15.

## Consequences

- `P2-09` is added to `tests/progress.yaml`, and `tests/catalog.yaml` is regenerated from the amended plan.
- When P3-08 completes, the coverage gate fails until the fsh-energy and fsh-facility-v2 scenarios exist. The same holds for each later kind.
