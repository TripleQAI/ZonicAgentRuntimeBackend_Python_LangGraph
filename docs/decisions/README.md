# Decision Records

Each record states a decision taken while building the runtime, why it was taken, and what it changes relative to the design or the plan. A new record takes the next number. A record is superseded by a later one, never edited to say something else.

| Record | Decision |
|---|---|
| [0001](0001-langgraph-verification-spikes.md) | LangGraph verification spikes SP-1 to SP-10 and what they confirmed ([reports](0001-spike-reports/)) |
| [0002](0002-langgraph-1.2.18.md) | Upgrade to LangGraph 1.2.18 for node error handlers |
| [0003](0003-m1-design-deviations.md) | Design deviations found while building Phase 1 |
| [0004](0004-cognito-only-access.md) | The Cognito token is the only access control; the document's `security` is not enforced |
| [0005](0005-builder-scenarios-wait-for-their-kinds.md) | Builder scenarios join with the tasks that build their node kinds |
| [0006](0006-recordings-vendored-from-nodejs.md) | The recorded responses are vendored from the Node.js runtime |
| [0007](0007-interrupt-rows-per-pause.md) | Interrupt rows are written per pause, and a run resumes with each answer |

Smaller deviations, per task, are in [../build-log.md](../build-log.md).
