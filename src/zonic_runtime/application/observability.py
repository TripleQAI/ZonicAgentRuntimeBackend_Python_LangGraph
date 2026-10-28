"""The trace and usage APIs, feedback and maintenance (design 10, 12.2; plan P3-11), after the Node.js
runtime's ``trace-service.ts``, ``feedback-service.ts`` and ``maintenance-service.ts``.

* ``TraceService``: ``GET /v1/runs/{id}/trace`` (the trace, its observation tree and its scores in
  one gateway batch), ``/trace-link`` (the run on the Langfuse server, when one is configured) and
  ``GET /v1/observability/usage`` (daily node usage, or the caller's tenant's model usage). All are
  for the roles in ``observability.traceRoles`` (403 otherwise); a trace is read only for its own
  user, so another user's run is the same 404 as a missing one (L-15).
* ``FeedbackService``: ``POST /v1/messages/{id}/feedback`` stores the rating (the procedure checks
  the message is the caller's), then scores the trace of the message's run, best effort (L-14).
* ``MaintenanceService``: partitions ahead, trace retention, the daily rollups and runtime retention,
  every ``maintenance.intervalMinutes`` on every instance; the procedures are idempotent, and a
  failing step does not stop the others (O-07).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from zonic_runtime.application.caller import Caller
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.integrations.langfuse.provider import trace_id
from zonic_runtime.models.api.observability import (
    Trace,
    TraceLink,
    TraceObservation,
    TraceScore,
    UsageItem,
    UsageList,
)
from zonic_runtime.models.api.requests import FeedbackRequest
from zonic_runtime.models.gateway.runtime import FeedbackSaveArgs, MessageRow, RetentionPurgeArgs
from zonic_runtime.models.gateway.wire import BatchItemResult, Filter, FilterOp, OrderBy
from zonic_runtime.models.observability import (
    ObsModelUsageRow,
    ObsNodeStatsRow,
    ObsObservationRow,
    ObsPartitionEnsureArgs,
    ObsRefreshRollupsArgs,
    ObsRetentionPurgeArgs,
    ObsScoreRow,
    ObsScoreUpsertArgs,
    ObsTraceRow,
)
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.gateway import GatewayPort
from zonic_runtime.runtime.clock import Clock, SystemClock

FORBIDDEN, NOT_FOUND, INVALID = "FORBIDDEN", "NOT_FOUND", "INPUT_INVALID"
_MODEL = "model"


def _time(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value is not None else None


class TraceService:
    def __init__(
        self,
        repository: GatewayRepository,
        gateway: GatewayPort,
        settings: RuntimeSettings,
        *,
        langfuse_url: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository, self._gateway, self._settings = repository, gateway, settings
        self._langfuse = langfuse_url.rstrip("/") if langfuse_url else None
        self._clock: Clock = clock or SystemClock()

    def permitted(self, caller: Caller) -> bool:
        return caller.has_any(self._settings.observability.trace_roles)

    def _require(self, caller: Caller) -> None:
        if not self.permitted(caller):
            raise ZonicError(FORBIDDEN, reason="a trace role is required")

    async def trace(self, caller: Caller, run_id: str) -> Trace:
        self._require(caller)
        found = Filter(column="trace_id", value=trace_id(run_id))
        limit = self._settings.gateway.max_view_rows
        items = [
            self._repository.view_item("trace", ObsTraceRow, owner=caller.subject, filters=[found], limit=1),
            self._repository.view_item(
                "observations",
                ObsObservationRow,
                owner=caller.subject,
                filters=[found],
                order_by=[OrderBy(column="start_time")],
                limit=limit,
            ),
            self._repository.view_item(
                "scores", ObsScoreRow, owner=caller.subject, filters=[found], limit=limit
            ),
        ]
        by_id = {r.id: r for r in (await self._gateway.batch(items, mode="sequential")).results}
        traces = self._rows(ObsTraceRow, by_id["trace"])
        if not traces:
            raise ZonicError(NOT_FOUND, run_id=run_id)
        row = traces[0]
        return Trace(
            trace_id=row.trace_id,
            run_id=row.run_id,
            name=row.name,
            status=row.status,
            session_id=row.session_id,
            agentic_workflow_id=row.agentic_workflow_id,
            start_time=_time(row.start_time),
            end_time=_time(row.end_time),
            latency_ms=row.latency_ms,
            observation_count=max(0, row.observation_count or 0),
            error_count=max(0, row.error_count or 0),
            input_tokens=max(0, row.input_tokens or 0),
            output_tokens=max(0, row.output_tokens or 0),
            total_cost=row.total_cost or 0,
            tags=[t for t in row.tags or [] if isinstance(t, str)],
            observations=self._tree(self._rows(ObsObservationRow, by_id["observations"])),
            scores=[self._score(s) for s in self._rows(ObsScoreRow, by_id["scores"])],
        )

    async def link(self, caller: Caller, run_id: str) -> TraceLink:
        """The run's trace on the Langfuse server; ``NOT_FOUND`` when no server is configured."""
        self._require(caller)
        if self._langfuse is None:
            raise ZonicError(NOT_FOUND, reason="no Langfuse server is configured")
        found = trace_id(run_id)
        return TraceLink(trace_id=found, url=f"{self._langfuse}/trace/{found}")

    async def usage(
        self, caller: Caller, *, group_by: str = "node", start: date | None = None, end: date | None = None
    ) -> UsageList:
        self._require(caller)
        today = self._clock.now().astimezone(UTC).date()
        first = start or today - timedelta(days=self._settings.observability.usage_default_days)
        last = end or today
        if first > last:
            raise ZonicError(INVALID, field="from", reason="from is after to")
        days = [
            Filter(column="day", op=FilterOp.GE, value=first.isoformat()),
            Filter(column="day", op=FilterOp.LE, value=last.isoformat()),
        ]
        order, limit = [OrderBy(column="day")], self._settings.gateway.max_view_rows
        if group_by == _MODEL:
            models = await self._repository.read(
                ObsModelUsageRow,
                filters=[*days, Filter(column="tenant_id", value=caller.tenant or "")],
                order_by=order,
                limit=limit,
            )
            return UsageList(
                group_by="model",
                items=[
                    UsageItem(
                        day=str(r.day)[:10],
                        model=r.model,
                        calls=r.calls,
                        input_tokens=r.input_tokens,
                        output_tokens=r.output_tokens,
                        total_cost=r.total_cost,
                    )
                    for r in models
                ],
            )
        nodes = await self._repository.read(ObsNodeStatsRow, filters=days, order_by=order, limit=limit)
        return UsageList(
            group_by="node",
            items=[
                UsageItem.model_validate({**r.model_dump(exclude={"day"}), "day": str(r.day)[:10]})
                for r in nodes
            ],
        )

    def _rows[R: BaseModel](self, model: type[R], item: BatchItemResult) -> list[R]:
        if item.status == "error":
            raise ZonicError("DATA_REQUEST_INVALID", reason=item.error.message if item.error else "")
        return self._repository.parse_rows(model, list(item.rows or []))

    @staticmethod
    def _tree(rows: Sequence[ObsObservationRow]) -> list[TraceObservation]:
        """Observations nested by ``parent_observation_id``, each level in start order."""
        ordered = sorted(rows, key=lambda r: _time(r.start_time) or "")
        nodes = {r.observation_id: TraceService._observation(r) for r in ordered}
        roots: list[TraceObservation] = []
        for row in ordered:
            parent = nodes.get(row.parent_observation_id or "")
            (parent.children if parent is not None else roots).append(nodes[row.observation_id])
        return roots

    @staticmethod
    def _observation(row: ObsObservationRow) -> TraceObservation:
        return TraceObservation(
            observation_id=row.observation_id,
            parent_observation_id=row.parent_observation_id,
            type=row.type,
            name=row.name,
            level=row.level or "DEFAULT",
            status_message=row.status_message,
            start_time=_time(row.start_time) or "",
            end_time=_time(row.end_time),
            latency_ms=row.latency_ms,
            model=row.model,
            usage=row.usage_details,
            total_cost=row.total_cost,
            node_id=row.node_id,
            node_type=row.node_type,
            node_name=row.node_name,
            visit=row.visit,
            path=row.path,
            input=row.input,
            output=row.output,
            metadata=dict(row.metadata or {}),
        )

    @staticmethod
    def _score(row: ObsScoreRow) -> TraceScore:
        return TraceScore(
            name=row.name,
            data_type=row.data_type,
            value=row.value,
            string_value=row.string_value,
            source=row.source,
            comment=row.comment,
            observation_id=row.observation_id,
            timestamp=_time(row.timestamp),
        )


def score_id(message_id: str, subject: str) -> str:
    """A UUID derived from the message and the user, so a new rating replaces the earlier score."""
    digest = hashlib.sha256(f"{message_id}|{subject}".encode()).hexdigest()
    return str(UUID(f"{digest[:12]}5{digest[13:16]}8{digest[17:32]}"))


class FeedbackService:
    def __init__(self, repository: GatewayRepository, settings: RuntimeSettings) -> None:
        self._repository, self._settings = repository, settings

    async def save(self, caller: Caller, message_id: str, request: FeedbackRequest) -> None:
        feedback = {
            "messageId": message_id,
            "ownerSub": caller.subject,
            "rating": request.rating,
            "comment": request.comment or "",
        }
        await self._repository.call(FeedbackSaveArgs(feedback=feedback))
        observability = self._settings.observability
        if not observability.enabled:
            return
        # The rating is saved; the score follows the run's trace, which may not exist (sampled out).
        with contextlib.suppress(ZonicError):
            message = await self._repository.read_one(
                MessageRow, owner=caller.subject, filters=[Filter(column="message_id", value=message_id)]
            )
            if message is None or message.run_id is None:
                return
            score: dict[str, Any] = {
                "scoreId": score_id(message_id, caller.subject),
                "traceId": trace_id(str(message.run_id)),
                "sessionId": str(message.session_id),
                "name": observability.feedback_score_name,
                "dataType": "NUMERIC",
                "value": request.rating,
                "source": "API",
                "authorUserId": caller.subject,
                "environment": observability.environment,
                "metadata": {"messageId": message_id},
            }
            if request.comment:
                score["comment"] = request.comment
            await self._repository.call(ObsScoreUpsertArgs(score=score))


class MaintenanceService:
    def __init__(
        self, repository: GatewayRepository, settings: RuntimeSettings, clock: Clock | None = None
    ) -> None:
        self._repository, self._settings = repository, settings
        self._clock: Clock = clock or SystemClock()
        self._task: asyncio.Task[None] | None = None
        #: The last pass: when, and each step's ``ok`` or error code.
        self.last: dict[str, Any] | None = None

    def _steps(self) -> Mapping[str, BaseModel]:
        observability, retention = self._settings.observability, self._settings.retention
        now = self._clock.now().astimezone(UTC)
        return {
            "partitions": ObsPartitionEnsureArgs(months_ahead=observability.partition_months_ahead),
            "traceRetention": ObsRetentionPurgeArgs(
                before=now - timedelta(days=observability.retention_days)
            ),
            "rollups": ObsRefreshRollupsArgs(),
            "runtimeRetention": RetentionPurgeArgs(before=now - timedelta(days=retention.sessions_days)),
        }

    async def run(self, only: Callable[[str], bool] = lambda _: True) -> dict[str, Any]:
        steps: dict[str, str] = {}
        for name, args in self._steps().items():
            if not only(name):
                continue
            try:
                await self._repository.call(args)
                steps[name] = "ok"
            except ZonicError as error:
                steps[name] = error.code
        self.last = {"at": _time(self._clock.now()), "steps": steps}
        return self.last

    def start(self) -> None:
        """Runs now and every ``maintenance.intervalMinutes`` (in the running loop)."""
        if not self._settings.maintenance.enabled or self._task is not None:
            return
        seconds = self._settings.maintenance.interval_minutes * 60

        async def loop() -> None:
            while True:
                await self.run()
                await asyncio.sleep(seconds)

        self._task = asyncio.create_task(loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
