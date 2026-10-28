"""``CheckpointStore``: the pause save and its read (design 9.2, 9.4; plan P4-02; cases P-08, H-04).

* **Pause** (``pause``): one ``sp_interrupt_create`` writes the pause's interrupts (one row per
  pending LangGraph interrupt, under its row ID for this attempt, decision 0007), one checkpoint
  row and the turn's messages so far, and moves the run to ``waiting_input``. The checkpoint body
  is the run's ``TurnSaver`` bundle plus what a resume needs: the documents the run compiled, the
  row IDs mapped to LangGraph's interrupt IDs, the seq of the pause's last event and the turn (the
  state it started from, its session and mode, and how many of its messages are saved). It
  expires with the last of the pause's interrupts.
* **Drain and write-behind** (P4-05): a running ``async`` run's checkpoint, the same body
  without interrupts, through ``sp_checkpoint_save`` (fenced by the attempt; latest wins).
* **Load** (``load``): the run's checkpoint from ``v_checkpoint``, for its owner, on any instance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zonic_runtime.engine.persistence.codec import CheckpointBundle
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.models.gateway.runtime import CheckpointRow, CheckpointSaveArgs, InterruptCreateArgs
from zonic_runtime.models.gateway.wire import Filter
from zonic_runtime.run.driver import ActiveRun, RunOutcome
from zonic_runtime.run.interrupt_ids import row_id

#: The ``CheckpointBundle`` members of a checkpoint body; the rest is the pause's.
_BUNDLE = frozenset(CheckpointBundle(thread_id="").model_dump(by_alias=True))
_INPUT = "input"
_ROW_KEYS = ("nodeId", "kind", "prompt", "answerSchema", "uiSchema", "expiresAt")


@dataclass(frozen=True)
class LoadedCheckpoint:
    run_id: str
    #: The attempt that paused: the resume moves the run on to ``attempt + 1``.
    attempt: int
    bundle: CheckpointBundle
    #: ``pause`` (interrupts), or ``async`` (a drain or write-behind of a running ``async`` run).
    kind: str = "pause"
    #: Workflow ID -> the stored document the run compiled.
    documents: dict[str, Any] = field(default_factory=dict)
    #: Interrupt row ID -> LangGraph interrupt ID.
    interrupts: dict[str, str] = field(default_factory=dict)
    last_event_seq: int = 0
    turn: dict[str, Any] = field(default_factory=dict)


def interrupt_rows(
    run_id: str, attempt: int, interrupts: Sequence[Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The pause's ``runtime_interrupt`` rows (the kind's ``InterruptRequest`` of each) and the map
    of their row IDs to LangGraph's interrupt IDs."""
    rows, ids = [], {}
    for interrupt in interrupts:
        value = (
            interrupt.value
            if isinstance(interrupt.value, Mapping)
            else {"prompt": {"text": str(interrupt.value)}}
        )
        found = row_id(run_id, attempt, str(interrupt.id))
        ids[found] = str(interrupt.id)
        row = {"interruptId": found, **{k: value[k] for k in _ROW_KEYS if value.get(k) is not None}}
        row.setdefault("kind", _INPUT)
        row.setdefault("nodeId", "")
        schema = getattr(interrupt, "response_schema", None)  # a tool approval's decision schema
        if "answerSchema" not in row and isinstance(schema, Mapping):
            row["answerSchema"] = dict(schema)
        rows.append(row)
    return rows, ids


class CheckpointStore:
    def __init__(self, repository: GatewayRepository) -> None:
        self._repository = repository

    @staticmethod
    def body(
        run: ActiveRun,
        bundle: CheckpointBundle,
        turn: Mapping[str, Any],
        last_event_seq: int,
        interrupts: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            **bundle.model_dump(mode="json", by_alias=True),
            "documents": run.context.documents.stored(),
            "interrupts": dict(interrupts or {}),
            "lastEventSeq": last_event_seq,
            "turn": dict(turn),
        }

    def save_call(
        self, run: ActiveRun, bundle: CheckpointBundle, turn: Mapping[str, Any]
    ) -> CheckpointSaveArgs:
        """A running run's checkpoint (a drain, write-behind): its events so far, no interrupts."""
        identity = run.context.run
        body = self.body(run, bundle, turn, run.stream.last_seq)
        return CheckpointSaveArgs(run_id=UUID(identity.run_id), attempt=identity.attempt, body=body)

    def call(
        self,
        run: ActiveRun,
        outcome: RunOutcome,
        bundle: CheckpointBundle,
        turn: Mapping[str, Any],
        messages: list[dict[str, Any]],
    ) -> InterruptCreateArgs:
        """The pause's ``sp_interrupt_create`` call."""
        identity = run.context.run
        rows, ids = interrupt_rows(identity.run_id, identity.attempt, outcome.interrupts)
        # The pause's own events follow the save (its warnings, interrupt.requested, run.waiting):
        # the resumed turn's events continue after them (case H-05).
        last = run.stream.last_seq + len(outcome.warnings) + len(rows) + 1
        body = self.body(run, bundle, turn, last, ids)
        expiries = sorted(str(r["expiresAt"]) for r in rows if r.get("expiresAt"))
        checkpoint: dict[str, Any] = {"body": body}
        if expiries:
            checkpoint["expiresAt"] = expiries[-1]
        return InterruptCreateArgs(
            interrupt={"runId": identity.run_id, "interrupts": rows},
            checkpoint=checkpoint,
            messages=messages,
            attempt=identity.attempt,
        )

    async def load(self, run_id: str, owner: str) -> LoadedCheckpoint | None:
        row = await self._repository.read_one(
            CheckpointRow, owner=owner, filters=[Filter(column="run_id", value=run_id)]
        )
        if row is None:
            return None
        body = dict(row.body)
        bundle = CheckpointBundle.read({k: v for k, v in body.items() if k in _BUNDLE})
        return LoadedCheckpoint(
            run_id=str(UUID(str(row.run_id))),
            attempt=row.attempt,
            bundle=bundle,
            kind=row.kind,
            documents=dict(body.get("documents") or {}),
            interrupts={str(k): str(v) for k, v in (body.get("interrupts") or {}).items()},
            last_event_seq=int(body.get("lastEventSeq") or 0),
            turn=dict(body.get("turn") or {}),
        )
