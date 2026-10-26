# Synthetic workflow fixtures

Builder 2.0 documents written for this repository (plan P1-01, Section 20.1) for the patterns no
vendored builder document covers. Node shapes copy the builder's own documents; every file validates
against the vendored `workflow-document-v2.schema.json`. Model IDs are placeholders; tests run them
against `ScriptedChatModel`.

| File | Pattern | Expected path, roughly |
|---|---|---|
| `external-agent.json` | The only `external_agent` node in the fixtures | Start_1, External_1, Output_1 |
| `routing-rule-branches.json` | Rule with two conditional branches and an else branch | Start_1, Rule_1, then Billing, Outage or Other by `flow.topic` |
| `routing-handoff.json` | Agent `handoff-<target>` edges to two LLM nodes, joining at Output | Start_1, Agent_1, Billing or Tech or neither, Output_1 |
| `routing-guardrail-approval.json` | Guardrail `pass`/`fail`, approval `approved`/`rejected` | Start_1, Guardrail_1, LLM_1, Approval_1, Approved or Rejected; Refused on fail |
| `fan-in-unequal.json` | Parallel fan-in of unequal lengths (1 node and 3 nodes) | Join_1 runs once, after B_3 |
| `nested-loops.json` | Inner refine loop inside an outer section loop | Outer_1, (Inner_1, Inner_check)+, Count_1, Outer_check, repeated |
| `supervisor-loop.json` | Supervisor agent delegating to workers that return to it | Supervisor_1, Worker_A or Worker_B, Supervisor_1, ..., Output_1 |
| `approval-in-loop.json` | Approval whose `rejected` branch loops back to the draft | Draft_1, Approval_1, repeated until approved |
| `subflow-chain-parent.json`, `-child.json`, `-grandchild.json` | Three-level sub-flow chain by `agenticWorkflowId` | parent, child, grandchild |
| `rule-expression.json` | Rule branches with free-text `expression` and empty `conditions` (design 5.2, `RULE_EXPRESSION_UNSUPPORTED`) | Not executable unless `settings.compat.ruleExpression` is `parse_simple` |
