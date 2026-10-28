"""``TurnStateStore``: the two gateway calls of a turn (design 9.2, 9.3; plan P2-04).

* **Start** (``begin``): one ``sequential`` batch on one connection: the session's variables for
  the workflow (``v_session_state``), its last ``history.maxMessages`` messages (``v_message``,
  read before the new message so history ends with the previous turn) and ``sp_run_start`` (the
  session if new, idempotency, the active-run caps, the run row and the user's message). A new
  session has no state to read, so its start is ``sp_run_start`` alone.
* **End** (``finish``, the driver's ``TurnFinisher``): one ``sp_run_finish`` with the turn's save:
  status, result, error, usage, the turn's messages (the workflow's thread writes and agents'
  turns, then the answer for the chat), the declared session variables (version-checked), the
  node timeline and ``lastEventSeq`` (DR-9). It is idempotent per run and attempt, so it is
  retried while the gateway is unavailable until ``state.saveDeadlineMilliseconds``; then the run
  fails with ``STATE_UNAVAILABLE`` and ``run.completed`` is never sent (case P-11). A version
  conflict on the variables is retried once over the latest ones; then the turn is saved without
  them, with a ``STATE_CONFLICT`` warning.

* **Pause** (P4-02): a turn that waits for interrupts is saved by ``CheckpointStore``'s one
  ``sp_interrupt_create``, with the turn's messages so far; the turn that resumes it saves only the
  messages after them. It is retried as the end is.
* **Durable runs** (P4-05): the start records the lease of this instance (or ``queued``), the
  caller's snapshot a claiming instance runs the turn as, and the completion callback. A drained
  ``async`` run saves its checkpoint (``sp_checkpoint_save``) and is released to the queue
  (``sp_run_release``); a run lost to another instance saves nothing (its writes would be fenced
  off). ``write_behind`` saves a running ``async`` run's latest checkpoint at most every
  ``state.checkpointIntervalSeconds`` (P-14).

The run's ``TurnSaver`` buffer is detached after the save: nothing of the turn stays in memory.
Nothing is written per node (case P-05).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from zonic_runtime.engine.persistence.turn_saver import TurnSaver
from zonic_runtime.engine.state import keyed_values, without_seq
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.http_gateway import translate_status
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.models.gateway.runtime import (
    MESSAGE_ID_OUT,
    RUN_ID_OUT,
    SESSION_ID_OUT,
    CheckpointSaveArgs,
    DatasetSaveArgs,
    MessageRow,
    RunFinishArgs,
    RunMode,
    RunReleaseArgs,
    RunStartArgs,
    SessionStateRow,
)
from zonic_runtime.models.gateway.wire import BatchItemResult, Filter, OrderBy
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.gateway import GatewayPort
from zonic_runtime.run.driver import ActiveRun, RunOutcome
from zonic_runtime.run.executor.instance import RunInstance
from zonic_runtime.run.state.checkpoint_store import CheckpointStore
from zonic_runtime.runtime.clock import Clock, SystemClock

#: Outcomes the turn-end save records; ``waiting_input`` is the pause's, ``drained`` the drain's.
FINISHED = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
WAITING, DRAINED = "waiting_input", "drained"
CHAT_THREAD = "messages"
_PREVIEW = 200
_UNAVAILABLE, _CONFLICT, _STATE_CONFLICT = "DATA_UNAVAILABLE", "DATA_CONFLICT", "STATE_CONFLICT"
_TEXT, _JSON = "markdown", "json"
_TEXT_PARTS = frozenset({_TEXT, "text"})  # "text": parts saved before P3-08


@dataclass(frozen=True)
class SavedState:
    """What a turn starts from: the session's saved variables, their version, its history."""

    flow: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    #: Thread name -> ``{role, content}`` messages, oldest first (``messages`` is the chat).
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class StartedTurn:
    run_id: str
    session_id: str
    message_id: str | None
    #: The request repeated an ``Idempotency-Key``: the run is the first request's.
    replayed: bool
    state: SavedState = field(default_factory=SavedState)
    #: The fencing token of the run's writes.
    attempt: int = 1


@dataclass(frozen=True)
class TurnFacts:
    """What the turn-end save needs about the turn beyond its outcome."""

    started: SavedState
    #: Flow keys that persist between turns (declared variables unless ``persist: false``).
    persisted: tuple[str, ...]
    #: The turn's messages a pause already saved (a resumed turn saves only the ones after them).
    saved: int = 0


def text_part(text: str) -> dict[str, Any]:
    return {"type": _TEXT, "text": text}


def user_message(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "kind": "user_input",
        "contentParts": [text_part(text)],
        "textPreview": text[:_PREVIEW],
    }


def _answer_text(result: Mapping[str, Any]) -> str | None:
    if isinstance(result.get("text"), str):
        return str(result["text"])
    strings = [v for v in result.values() if isinstance(v, str)]
    return strings[0] if len(strings) == 1 else None


def view_parts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The content parts the turn's nodes produced (views), in the order they completed."""
    nodes = state.get("nodes") or {}
    seen: list[str] = []
    for entry in state.get("timeline") or []:
        node_id = (entry.get("nodeId") or entry.get("node_id")) if isinstance(entry, Mapping) else None
        if isinstance(node_id, str) and node_id not in seen:
            seen.append(node_id)
    return [dict(p) for n in seen for p in (nodes.get(n) or {}).get("parts") or [] if isinstance(p, Mapping)]


def answer_message(result: Mapping[str, Any], parts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The run's answer as the chat's assistant message: its text, or the result as JSON, then
    the turn's view parts, so a reopened session shows them again."""
    text = _answer_text(result)
    part = text_part(text) if text is not None else {"type": _JSON, "value": dict(result)}
    preview = text if text is not None else json.dumps(dict(result), ensure_ascii=False, default=str)
    return {
        "role": "assistant",
        "kind": "answer",
        "visibility": "chat",
        "contentParts": [part, *(dict(p) for p in parts)],
        "textPreview": preview[:_PREVIEW],
    }


def history_message(row: MessageRow) -> dict[str, Any]:
    """A saved message as a later turn reads it: its text, or the JSON value of a one-part message."""
    parts = [p for p in row.content_parts if isinstance(p, Mapping)]
    texts = [str(p["text"]) for p in parts if p.get("type") in _TEXT_PARTS and isinstance(p.get("text"), str)]
    content: Any
    if parts and len(texts) == len(parts):
        content = "\n".join(texts)
    elif len(parts) == 1 and parts[0].get("type") == _JSON:
        content = parts[0].get("value")
    else:
        content = row.text_preview
    found: dict[str, Any] = {"role": row.role, "content": content}
    if row.node_id:
        found["nodeId"] = row.node_id
    return found


def history(rows: Sequence[MessageRow]) -> dict[str, list[dict[str, Any]]]:
    """By thread, oldest first. The chat thread keeps what the chat shows; a named thread (the
    workflow's own) keeps everything written to it."""
    threads: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: r.seq):
        if row.thread == CHAT_THREAD and row.visibility != "chat":
            continue
        threads.setdefault(row.thread, []).append(history_message(row))
    return threads


def _batch_error(item: BatchItemResult) -> ZonicError:
    """A failed batch item: the in-memory gateway reports the catalog code, the gateway the
    database's reason token."""
    error = item.error
    if error is None:
        return ZonicError("DATA_REQUEST_INVALID")
    if error.type.replace("_", "").isalpha() and error.type.isupper():
        return ZonicError(error.type)
    return translate_status(409, {"error": error.message, "type": error.type})


class TurnStateStore:
    def __init__(
        self,
        repository: GatewayRepository,
        gateway: GatewayPort,
        settings: RuntimeSettings,
        *,
        saver: TurnSaver | None = None,
        clock: Clock | None = None,
        checkpoints: CheckpointStore | None = None,
        instance: RunInstance | None = None,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._settings = settings
        self._saver = saver
        self._checkpoints = checkpoints
        #: This instance, when its runs are durable (P4-05): drains release runs as it.
        self._instance = instance
        self._clock = clock or SystemClock()
        #: Run ID -> the facts its save needs; dropped when it is saved.
        self._facts: dict[str, TurnFacts] = {}

    # -- start ---------------------------------------------------------------------------------

    def _start_args(self, turn: Mapping[str, Any]) -> RunStartArgs:
        runs = self._settings.runs
        queued = bool(turn.get("queued"))
        run: dict[str, Any] = {
            "runId": turn["run_id"],
            "ownerSub": turn["owner_sub"],
            "ownerEmail": turn.get("email") or "",
            "tenantId": turn.get("tenant_id") or "",
            "agenticWorkflowId": turn["agentic_workflow_id"],
            "workflowRevision": turn["revision"],
            "documentVersion": turn["document_version"],
            "mode": turn["mode"],
            "status": "queued" if queued else "running",
            "input": {"text": turn["text"], **({"caller": turn["caller"]} if turn.get("caller") else {})},
            "limits": {
                "maxActivePerSession": runs.max_active_per_session,
                "maxActivePerUser": runs.max_active_per_user,
                "maxActivePerTenant": runs.max_active_per_tenant,
            },
        }
        for key, name in (
            ("sessionId", "session_id"),
            ("title", "title"),
            ("idempotencyKey", "idempotency_key"),
            ("callback", "callback"),
        ):
            if turn.get(name) is not None:
                run[key] = turn[name]
        if turn.get("lease") and not queued:
            run.update(turn["lease"])
        return RunStartArgs(run=run, user_message=user_message(turn["text"]))

    async def begin(
        self,
        *,
        run_id: str,
        owner_sub: str,
        tenant_id: str | None,
        email: str | None,
        agentic_workflow_id: str,
        revision: str,
        document_version: str,
        mode: RunMode,
        text: str,
        session_id: str | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
        queued: bool = False,
        lease: Mapping[str, Any] | None = None,
        caller: Mapping[str, Any] | None = None,
        callback: Mapping[str, Any] | None = None,
    ) -> StartedTurn:
        """``queued``: an ``async`` run with no free permit waits for a claim (P4-05); ``lease``: this
        instance's, for a run it starts; ``caller``: the snapshot a claiming instance runs it as."""
        turn = {
            "run_id": run_id,
            "owner_sub": owner_sub,
            "tenant_id": tenant_id,
            "email": email,
            "agentic_workflow_id": agentic_workflow_id,
            "revision": revision,
            "document_version": document_version,
            "mode": mode,
            "text": text,
            "session_id": session_id,
            "title": title,
            "idempotency_key": idempotency_key,
            "queued": queued,
            "lease": dict(lease) if lease else None,
            "caller": dict(caller) if caller else None,
            "callback": dict(callback) if callback else None,
        }
        args = self._start_args(turn)
        if session_id is None:
            return self._started(run_id, await self._repository.call(args), SavedState())
        session = Filter(column="session_id", value=session_id)
        workflow = Filter(column="agentic_workflow_id", value=agentic_workflow_id)
        newest_first = [OrderBy(column="seq", descending=True)]
        items = [
            self._repository.view_item(
                "state", SessionStateRow, owner=owner_sub, filters=[session, workflow], limit=1
            ),
            self._repository.view_item(
                "history",
                MessageRow,
                owner=owner_sub,
                filters=[session],
                order_by=newest_first,
                limit=self._settings.history.max_messages,
            ),
            self._repository.procedure_item("run", args),
        ]
        found = await self._gateway.batch(items, mode="sequential")
        by_id = {r.id: r for r in found.results}
        start = self._rows(by_id["run"])
        saved = self._repository.parse_rows(SessionStateRow, self._rows(by_id["state"]))
        rows = self._repository.parse_rows(MessageRow, self._rows(by_id["history"]))
        state = SavedState(
            flow=dict(saved[0].flow) if saved else {},
            version=saved[0].version if saved else None,
            history=history(rows),
        )
        return self._started(run_id, start[0] if start else {}, state)

    @staticmethod
    def _rows(item: BatchItemResult) -> list[dict[str, Any]]:
        if item.status == "error":
            raise _batch_error(item)
        return list(item.rows or [])

    @staticmethod
    def _started(run_id: str, out: Mapping[str, Any], state: SavedState) -> StartedTurn:
        started_id = str(out[RUN_ID_OUT])
        message = out.get(MESSAGE_ID_OUT)
        return StartedTurn(
            run_id=started_id,
            session_id=str(out[SESSION_ID_OUT]),
            message_id=str(message) if message else None,
            replayed=started_id != run_id,
            state=state,
        )

    async def restart(
        self, *, run_id: str, session_id: str, agentic_workflow_id: str, owner_sub: str, attempt: int
    ) -> StartedTurn:
        """A claimed run's turn, run again (P4-05, ``runs.onInstanceLoss: restart_turn``): the
        session's variables and history as a new turn reads them, without the run's own messages."""
        session = Filter(column="session_id", value=session_id)
        workflow = Filter(column="agentic_workflow_id", value=agentic_workflow_id)
        items = [
            self._repository.view_item(
                "state", SessionStateRow, owner=owner_sub, filters=[session, workflow], limit=1
            ),
            self._repository.view_item(
                "history",
                MessageRow,
                owner=owner_sub,
                filters=[session],
                order_by=[OrderBy(column="seq", descending=True)],
                limit=self._settings.history.max_messages,
            ),
        ]
        found = await self._gateway.batch(items, mode="sequential")
        by_id = {r.id: r for r in found.results}
        saved = self._repository.parse_rows(SessionStateRow, self._rows(by_id["state"]))
        rows = [
            r
            for r in self._repository.parse_rows(MessageRow, self._rows(by_id["history"]))
            if str(r.run_id) != run_id
        ]
        state = SavedState(
            flow=dict(saved[0].flow) if saved else {},
            version=saved[0].version if saved else None,
            history=history(rows),
        )
        return StartedTurn(
            run_id=run_id,
            session_id=session_id,
            message_id=None,
            replayed=False,
            state=state,
            attempt=attempt,
        )

    def track(self, run_id: str, started: SavedState, persisted: Sequence[str], saved: int = 0) -> None:
        """Records what the run's save will need (the run service, after ``begin`` or a resume)."""
        keys = tuple(dict.fromkeys([*persisted, *self._settings.persistence.flow]))
        self._facts[run_id] = TurnFacts(started=started, persisted=keys, saved=saved)

    @staticmethod
    def turn_facts(facts: TurnFacts) -> dict[str, Any]:
        """A paused turn's facts, as its checkpoint keeps them for the resume (``resumed_facts``)."""
        started = facts.started
        return {
            "started": {"flow": started.flow, "version": started.version, "history": started.history},
            "persisted": list(facts.persisted),
            "saved": facts.saved,
        }

    @staticmethod
    def resumed_facts(turn: Mapping[str, Any]) -> tuple[SavedState, list[str], int]:
        started = turn.get("started") or {}
        state = SavedState(
            flow=dict(started.get("flow") or {}),
            version=started.get("version"),
            history=dict(started.get("history") or {}),
        )
        return state, list(turn.get("persisted") or []), int(turn.get("saved") or 0)

    # -- end ----------------------------------------------------------------------------------

    def save(
        self, run: ActiveRun, outcome: RunOutcome, session_state: dict[str, Any] | None
    ) -> dict[str, Any]:
        state: Mapping[str, Any] = outcome.state or {}
        facts = self._facts.get(run.run_id)
        messages = without_seq(state.get("turn_messages") or [])[facts.saved if facts else 0 :]
        if outcome.result is not None:
            parts = view_parts(state)
            answer = answer_message(outcome.result, parts)
            chat = {
                (m.get("role"), m.get("textPreview"))
                for m in messages
                if m.get("visibility", "chat") == "chat"
            }
            if parts or (answer["role"], answer["textPreview"]) not in chat:
                messages.append(answer)
        save: dict[str, Any] = {
            "status": outcome.status,
            "result": outcome.result,
            "usage": outcome.usage.wire(),
            "messages": messages,
            "nodeTimeline": without_seq(state.get("timeline") or []),
            "lastEventSeq": run.stream.last_seq,
        }
        if outcome.error is not None:
            save["error"] = {"code": outcome.error.code, "message": outcome.error.message}
        datasets = run.context.ports.datasets
        if datasets is not None and datasets.pending.metadata:
            save["datasets"] = list(datasets.pending.metadata)
        if session_state is not None:
            save["sessionState"] = session_state
        return save

    @staticmethod
    def _variables(facts: TurnFacts, outcome: RunOutcome, base: SavedState) -> dict[str, Any] | None:
        """The persisted keys only: those this turn changed over ``base`` (the variables it started
        from, or after a version conflict the latest saved ones)."""
        if not facts.persisted and base.version is None:
            return None
        final = keyed_values((outcome.state or {}).get("flow"))
        flow: dict[str, Any] = {}
        for key in facts.persisted:
            changed = final.get(key) != facts.started.flow.get(key)
            kept = final.get(key) if changed else base.flow.get(key)
            if kept is not None:
                flow[key] = kept
        found: dict[str, Any] = {"flow": flow}
        if base.version is not None:
            found["expectedVersion"] = base.version
        return found

    async def finish(self, run: ActiveRun, outcome: RunOutcome) -> RunOutcome:
        if run.stopped:  # lost to another instance, or released: its writes would be fenced off
            self._facts.pop(run.run_id, None)
            if self._saver is not None:
                self._saver.detach(run.run_id)
            return outcome
        if outcome.status == WAITING and self._checkpoints is not None:
            await self._pause(run, outcome)
            return outcome
        if outcome.status == DRAINED and self._checkpoints is not None and self._instance is not None:
            await self._drained(run)
            return outcome
        if outcome.status not in FINISHED:
            return outcome
        facts = self._facts.get(run.run_id, TurnFacts(started=SavedState(), persisted=()))
        base = facts.started
        try:
            for conflicts in range(2):
                variables = self._variables(facts, outcome, base) if outcome.status == "succeeded" else None
                try:
                    await self._finish_call(run, outcome, variables)
                    return outcome
                except ZonicError as error:
                    if error.code != _CONFLICT or variables is None:
                        raise
                    if conflicts == 0:
                        base = await self._latest(run, base)
            warning = {"code": _STATE_CONFLICT, "message": "the session variables changed during this turn"}
            outcome = replace(outcome, warnings=[*outcome.warnings, warning])
            await self._finish_call(run, outcome, None)
            return outcome
        finally:
            self._facts.pop(run.run_id, None)
            if self._saver is not None:
                self._saver.detach(run.run_id)

    def _turn(self, run: ActiveRun, facts: TurnFacts) -> dict[str, Any]:
        """What a resume or a claim continues the turn with (the checkpoint's ``turn``)."""
        identity = run.context.run
        return {
            **self.turn_facts(facts),
            "sessionId": identity.session_id,
            "agenticWorkflowId": identity.workflow_id,
            "revision": identity.revision,
            "mode": identity.mode,
        }

    def checkpoint_call(self, run: ActiveRun) -> CheckpointSaveArgs | None:
        """The running run's checkpoint now (a drain, write-behind); ``None`` without a buffer."""
        if self._checkpoints is None or self._saver is None or not self._saver.attached(run.run_id):
            return None
        facts = self._facts.get(run.run_id, TurnFacts(started=SavedState(), persisted=()))
        return self._checkpoints.save_call(run, self._saver.export(run.run_id), self._turn(run, facts))

    async def _drained(self, run: ActiveRun) -> None:
        """A drained ``async`` run (design 15.15): its checkpoint at the superstep boundary, then back
        to the queue; the claiming instance continues it from there (R-03)."""
        assert self._saver is not None and self._instance is not None
        try:
            args = self.checkpoint_call(run)
            if args is not None:
                await self._retried(lambda: self._repository.call(args), run.run_id)
            release = RunReleaseArgs(instance_id=self._instance.id, run_ids=[run.run_id])
            await self._retried(lambda: self._repository.call(release), run.run_id)
        finally:
            self._facts.pop(run.run_id, None)
            self._saver.detach(run.run_id)

    async def write_behind(self, run: ActiveRun) -> None:
        """A running ``async`` run's latest checkpoint, at most once per ``checkpointIntervalSeconds``
        and only when it changed; nodes never wait for it (P-14). Cancelled at the turn's end."""
        assert self._saver is not None
        seen = 0
        while True:
            await asyncio.sleep(self._settings.state.checkpoint_interval_seconds)
            puts = self._saver.puts.get(run.run_id, 0)
            if puts == seen:
                continue
            seen = puts
            args = self.checkpoint_call(run)
            if args is not None:
                with contextlib.suppress(ZonicError):  # the next interval saves a newer one
                    await self._repository.call(args)

    async def _pause(self, run: ActiveRun, outcome: RunOutcome) -> None:
        """The pause save: the bundle, the turn's facts and its messages since the last save."""
        assert self._checkpoints is not None and self._saver is not None
        facts = self._facts.pop(run.run_id, TurnFacts(started=SavedState(), persisted=()))
        try:
            messages = without_seq((outcome.state or {}).get("turn_messages") or [])
            turn = self._turn(run, replace(facts, saved=len(messages)))
            bundle = self._saver.export(run.run_id)
            args = self._checkpoints.call(run, outcome, bundle, turn, messages[facts.saved :])
            await self._retried(lambda: self._repository.call(args), run.run_id)
        finally:
            self._saver.detach(run.run_id)

    async def _latest(self, run: ActiveRun, previous: SavedState) -> SavedState:
        identity = run.context.run
        row = await self._repository.read_one(
            SessionStateRow,
            owner=identity.owner_sub,
            filters=[
                Filter(column="session_id", value=identity.session_id),
                Filter(column="agentic_workflow_id", value=identity.workflow_id),
            ],
        )
        return replace(previous, flow=dict(row.flow) if row else {}, version=row.version if row else None)

    async def _finish_call(
        self, run: ActiveRun, outcome: RunOutcome, variables: dict[str, Any] | None
    ) -> None:
        save = self.save(run, outcome, variables)
        attempt = run.context.run.attempt
        args = RunFinishArgs(run_id=UUID(run.run_id), attempt=attempt, save=save)
        datasets = run.context.ports.datasets
        chunks = list(datasets.pending.chunks) if datasets is not None else []

        async def call() -> Any:
            if not chunks:
                return await self._repository.call(args)
            # The chunks and the finish commit together, or neither does.
            items = [
                self._repository.procedure_item(
                    "chunks", DatasetSaveArgs(run_id=UUID(run.run_id), attempt=attempt, chunks=chunks)
                ),
                self._repository.procedure_item("finish", args),
            ]
            found = await self._gateway.batch(items, mode="transactional", retry=True)
            for item in found.results:
                self._rows(item)
            return None

        await self._retried(call, run.run_id)

    async def _retried(self, call: Callable[[], Awaitable[Any]], run_id: str) -> None:
        """A save, retried while the gateway is unavailable until ``state.saveDeadlineMilliseconds``;
        then ``STATE_UNAVAILABLE`` (case P-11)."""
        deadline = self._clock.monotonic() + self._settings.state.save_deadline_milliseconds / 1000
        wait = self._settings.gateway.retry_backoff_milliseconds / 1000
        while True:
            try:
                await call()
                return
            except ZonicError as error:
                if error.code != _UNAVAILABLE:
                    raise
                if self._clock.monotonic() + wait >= deadline:
                    raise ZonicError("STATE_UNAVAILABLE", run_id=run_id) from error
                await asyncio.sleep(wait)
                wait = min(wait * 2, 2.0)
