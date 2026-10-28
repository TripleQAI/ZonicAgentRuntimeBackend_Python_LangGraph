# 0007: Interrupt rows are written per pause, and a run resumes with each answer

- **Status:** decided under the delegated design deviations (M1: "you should decide Design deviations"), 2026-10-27; for review at M4
- **Concerns:** plan task P4-02; cases H-02, H-03, H-10; spike SP-8
- **Deviates from:** nothing in the design; it fixes how the design's subset rule (design 9.4 step 4, SP-8) fits the vendored SQL, and differs from the Node.js runtime's behaviour

## Context

With two interrupts pending (parallel branches), SP-8 showed that `Command(resume={id_a: answer})` resumes branch a while branch b stays pending under the same LangGraph interrupt ID. The run then pauses again on b.

The vendored SQL (`db/runtime/005_procedures.sql`, Node.js commit `5c75833`) constrains how that second pause is saved:

- `sp_interrupt_create` is idempotent per interrupt: when the pause's first interrupt ID already exists, it returns without writing anything. Under LangGraph's ID, the second pause (b alone, still pending) would be a no-op: no new checkpoint, no messages, the run left `running`.
- No view returns an answered interrupt's answer. `v_interrupt_pending` lists pending rows only. So the Node.js approach (record answers until the last one arrives, then resume with all of them) cannot read the earlier answers back.
- No procedure expires one interrupt. `sp_interrupt_answer` refuses a row past `expiresAt`.

The Node.js repository has since added `v_interrupt` and `sp_interrupt_expire` for this, but only in its working tree. Those changes are not committed, so they cannot be vendored. Procedure SQL comes only from the Node.js repository by vendoring; this project never writes it.

## Decision

- **Rows per pause.** Each pause writes its interrupts under row IDs of its own: `uuid5(run, attempt, LangGraph interrupt ID)` (`run/interrupt_ids.py`). The checkpoint body maps them back to LangGraph's IDs. Events, the API and the trace use the row IDs.
- **Resume with each answer (SP-8).** `POST /v1/runs/{id}/interrupts/{interruptId}` records the answer and resumes at once with `Command(resume={its LangGraph ID: answer})`. A branch still waiting pauses the run again on the next attempt, under new row IDs.
- **Only the current pause is pending.** The API lists and accepts only rows whose `attempt` is the run's. A row an earlier pause left pending answers `404`. `sp_run_finish` closes it as `cancelled`.
- **Expiry** uses `sp_run_transition`, as design 9.4 step 6 says. With `onExpiry: cancel`, the run is cancelled with `INTERRUPT_EXPIRED`. With `reject`, an Approval resumes as rejected (`{approved: false, expired: true}`) without `sp_interrupt_answer`, which refuses expired rows. Attempt fencing makes a second instance's try fail harmlessly.

## Consequences

- The run's status shows each partial answer at once (`waiting_input` again with the other interrupt), and the client answers the next one under its new ID, which `GET /v1/runs/{id}/interrupts` and the stream's `run.waiting` give.
- An interrupt carried into a later pause keeps its row from the earlier pause in the database until the turn ends.
- When the Node.js SQL with `v_interrupt` and `sp_interrupt_expire` is committed and vendored, the expiry can use `sp_interrupt_expire`, and the rows can keep one ID across pauses. The resume-per-answer rule stays.
