"""The gateway span exporter and the record mapper (P2-08: O-01 to O-06, exporter side; the SQL side
of O-02 to O-04 is ``tests/sql/test_observability.py``). Spans come from real traced runs or are
built by hand; ``sp_obs_ingest`` is a fake that keeps records the way the procedure does."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, TraceFlags

from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.streaming import DriverHarness
from tests.unit.test_core_kinds import agent, chain, lookup, output
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.langfuse.export import tidy
from zonic_runtime.integrations.langfuse.gateway_exporter import GatewaySpanExporter
from zonic_runtime.integrations.langfuse.provider import LangfuseTracer, trace_id
from zonic_runtime.integrations.langfuse.record_mapper import LangfuseRecordMapper
from zonic_runtime.models.observability import ObsIngestArgs, ObsObservationRecord, ObsTraceRecord

DEV = load_resources(EnvironmentModel(env="dev"))
TRACE = 0x0123456789ABCDEF0123456789ABCDEF
T0 = 1_790_000_000_000_000_000  # ns, 2026-09-21


@dataclass
class FakeIngest:
    """Raises the queued failures first; keeps traces merged and observations inserted once."""

    failures: list[str] = field(default_factory=list)
    calls: list[ObsIngestArgs] = field(default_factory=list)
    traces: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __call__(self, args: ObsIngestArgs) -> None:
        self.calls.append(args)
        if self.failures:
            raise ZonicError(self.failures.pop(0))
        for trace in args.traces:
            self.traces.setdefault(trace["traceId"], {}).update(trace)
        for observation in args.observations:
            self.observations.setdefault(observation["observationId"], observation)


def export_settings(**values: Any) -> Any:
    return DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 1, **values})


def exporter(ingest: FakeIngest, **values: Any) -> GatewaySpanExporter:
    return GatewaySpanExporter(ingest, LangfuseRecordMapper(DEV.observability), export_settings(**values))


def span(
    span_id: int,
    *,
    name: str = "s",
    parent: int | None = None,
    attributes: dict[str, Any] | None = None,
    start: int = T0,
    end: int | None = T0 + 5_000_000,
) -> ReadableSpan:
    def context(identifier: int) -> SpanContext:
        return SpanContext(TRACE, identifier, is_remote=False, trace_flags=TraceFlags(1))

    return ReadableSpan(
        name=name,
        context=context(span_id),
        parent=context(parent) if parent is not None else None,
        attributes=attributes or {},
        start_time=start,
        end_time=end,
    )


def root(span_id: int = 1, **attributes: Any) -> ReadableSpan:
    attrs = {
        "langfuse.observation.metadata.zonic_run_root": "true",
        "langfuse.trace.name": "wf",
        "langfuse.observation.type": "span",
        **attributes,
    }
    return span(span_id, name="wf", parent=0xFFFF, attributes=attrs)


def validate(ingest: FakeIngest) -> None:
    for args in ingest.calls:
        for trace in args.traces:
            ObsTraceRecord.model_validate(trace)
        for observation in args.observations:
            ObsObservationRecord.model_validate(observation)


# --- O-01 --------------------------------------------------------------------------------------


async def test_O01_a_traced_agent_run_becomes_one_trace_and_one_observation_per_span() -> None:
    script = [Turn.call("lookup", {"city": "Oslo"}, text="Checking."), Turn.say("Sunny.")]
    h, _ = agent_harness(script, DEV, tools={"ag": [lookup]})
    ingest = FakeIngest()
    lf = LangfuseTracer(DEV.settings.observability, DEV.observability, exporter(ingest), release="r1")
    run_id = str(uuid.uuid4())
    result = await DriverHarness(h, tracer=lf).run(chain(agent(), output()), run_id=run_id)
    lf.flush()
    lf.shutdown()
    assert result.outcome.status == "succeeded"
    validate(ingest)

    [trace] = ingest.traces.values()
    assert trace["traceId"] == trace_id(run_id) and trace["runId"] == run_id
    assert trace["name"] == "test workflow" and trace["userId"] == "owner" and trace["release"] == "r1"
    assert trace["status"] == "succeeded" and {"wf", "test"} <= set(trace["tags"])
    assert "sessionId" not in trace and trace["metadata"]["sessionId"] == "s-1"  # not a UUID
    assert trace["output"] == {"text": "Sunny."} and trace["startTime"] <= trace["endTime"]

    found = list(ingest.observations.values())
    ids = {o["observationId"] for o in found}
    by_node = {o["nodeId"]: o for o in found if o.get("visit") == 1}
    assert {n: (o["type"], o["nodeType"], o["nodeKind"]) for n, o in by_node.items()} == {
        "start": ("span", "start", "entry"),
        "ag": ("agent", "agent", "agent"),
        "out": ("span", "output", "output"),
    }
    [top] = [o for o in found if "parentObservationId" not in o]
    assert top["metadata"] == {} or "zonic_run_root" not in top["metadata"]
    assert all(o["parentObservationId"] in ids for o in found if o is not top)  # no dangling parent
    assert not [o for o in found if o["name"] in ("model", "tools") and o["type"] == "chain"]  # SP-4

    def under(observation: dict[str, Any], node: dict[str, Any]) -> bool:
        parents = {o["observationId"]: o.get("parentObservationId") for o in found}
        current = observation.get("parentObservationId")
        while current is not None and current != node["observationId"]:
            current = parents.get(current)
        return current == node["observationId"]

    generations = [o for o in found if o["type"] == "generation"]
    assert len(generations) == 2 and all(under(g, by_node["ag"]) for g in generations)
    assert all(g["model"] == "scripted" and g["usageDetails"]["input"] > 0 for g in generations)
    assert all("totalCost" not in g for g in generations)  # priced by sp_obs_ingest (O-04)
    [tool] = [o for o in found if o["type"] == "tool"]
    assert tool["name"] == "lookup" and under(tool, by_node["ag"])
    [warning] = [o for o in found if o["type"] == "event"]
    assert warning["level"] == "WARNING" and warning["parentObservationId"] == by_node["out"]["observationId"]
    assert all("zonic_node_id" not in o["metadata"] and "thread_id" not in o["metadata"] for o in found)


def test_O01_gen_ai_attributes_fill_what_the_langfuse_ones_leave_out() -> None:
    mapper = LangfuseRecordMapper(DEV.observability)
    record = mapper.observation(
        span(
            7,
            parent=1,
            attributes={
                "langfuse.observation.type": "generation",
                "gen_ai.request.model": "vendor-model",
                "gen_ai.usage.input_tokens": 5,
                "gen_ai.usage.output_tokens": 7,
                "gen_ai.request.temperature": 0.2,
            },
        )
    )
    assert record["model"] == "vendor-model" and record["usageDetails"] == {"input": 5, "output": 7}
    assert record["modelParameters"] == {"temperature": 0.2} and record["latencyMs"] == 5
    langfuse_first = mapper.observation(
        span(8, attributes={"langfuse.observation.model.name": "lf", "gen_ai.request.model": "other"})
    )
    assert langfuse_first["model"] == "lf"


def test_O01_an_unknown_type_is_a_span_and_an_oversized_output_is_summarized() -> None:
    mapper = LangfuseRecordMapper(DEV.observability)
    big = json.dumps({"rows": ["x" * 1000] * 100})
    record = mapper.observation(
        span(9, attributes={"langfuse.observation.type": "mystery", "langfuse.observation.output": big})
    )
    assert record["type"] == "span" and record["output"] == {"type": "object", "fields": 1, "keys": ["rows"]}
    assert record["metadata"]["truncated"] == ["output"]
    ObsObservationRecord.model_validate(record)


def test_O01_wrapper_spans_are_dropped_and_their_children_hang_under_the_wrappers_parent() -> None:
    meta = "langfuse.observation.metadata."
    node = span(2, name="ag", parent=1)
    model = span(
        3,
        name="model",
        parent=2,
        attributes={"langfuse.observation.type": "chain", meta + "langgraph_node": '"model"'},
    )
    call = span(4, name="ChatModel", parent=3, attributes={"langfuse.observation.type": "generation"})
    kept = tidy([call, model, node])
    assert [s.name for s in kept] == ["ChatModel", "ag"]
    assert kept[0].parent is not None and kept[0].parent.span_id == 2 and kept[0].context == call.context


# --- O-02, O-03, O-04 ----------------------------------------------------------------------------


def test_O02_the_same_spans_exported_twice_send_identical_records_and_insert_nothing_new() -> None:
    ingest = FakeIngest()
    spans = [span(2, parent=1), span(3, parent=2), root()]
    out = exporter(ingest)
    out.export(spans)
    out.export(spans)
    first, second = ingest.calls
    assert first.wire() == second.wire()
    assert len(ingest.observations) == 3 and len(ingest.traces) == 1
    validate(ingest)


def test_O03_observations_may_go_out_before_their_trace_header() -> None:
    ingest = FakeIngest()
    out = exporter(ingest)
    out.export([span(2, parent=1), span(3, parent=2)])
    out.export([root(**{"langfuse.trace.tags": ("b", "a")})])
    early, late = ingest.calls
    assert early.traces == [] and len(early.observations) == 2
    assert [t["tags"] for t in late.traces] == [["a", "b"]] and late.traces[0]["traceId"] == format(
        TRACE, "032x"
    )


def test_O04_cost_is_left_to_the_procedure_unless_the_span_carries_it() -> None:
    mapper = LangfuseRecordMapper(DEV.observability)
    usage = {"langfuse.observation.usage_details": json.dumps({"input": 10, "output": 2})}
    priced_later = mapper.observation(span(5, attributes=usage))
    assert "totalCost" not in priced_later and "costDetails" not in priced_later
    cost = json.dumps({"input": 0.1, "output": 0.2, "total": 0.3})
    priced = mapper.observation(span(6, attributes={**usage, "langfuse.observation.cost_details": cost}))
    assert priced["totalCost"] == 0.3 and priced["costDetails"]["input"] == 0.1


# --- O-05 --------------------------------------------------------------------------------------


def test_O05_a_batch_above_the_count_limit_is_split() -> None:
    ingest = FakeIngest()
    exporter(ingest, max_observations_per_call=3).export([span(i, parent=1) for i in range(2, 12)])
    assert [len(c.observations) for c in ingest.calls] == [3, 3, 3, 1]
    assert len(ingest.observations) == 10


def test_O05_a_batch_above_the_byte_limit_is_split_and_an_oversized_record_goes_alone() -> None:
    ingest = FakeIngest()
    text = json.dumps("x" * 900)
    spans = [span(i, parent=1, attributes={"langfuse.observation.input": text}) for i in range(2, 6)]
    huge = span(9, parent=1, attributes={"langfuse.observation.input": json.dumps("y" * 5000)})
    exporter(ingest, max_bytes_per_call=2500).export([*spans, huge])
    sizes = [len(json.dumps(c.observations)) for c in ingest.calls]
    assert [len(c.observations) for c in ingest.calls] == [2, 2, 1] and sizes[-1] > 2500
    assert all(size <= 2500 for size in sizes[:-1])


# --- O-06 --------------------------------------------------------------------------------------


def test_O06_a_503_during_ingest_is_retried_without_duplicates() -> None:
    ingest = FakeIngest(failures=["DATA_UNAVAILABLE", "DATA_UNAVAILABLE"])
    out = exporter(ingest, retry_attempts=5)
    out.export([span(2, parent=1), root()])
    assert len(ingest.calls) == 3 and ingest.calls[0].wire() == ingest.calls[2].wire()
    assert len(ingest.observations) == 2 and out.healthy and out.queued == 0 and out.dropped == 0


def test_O06_with_the_gateway_down_the_queue_keeps_the_newest_and_drops_the_oldest() -> None:
    ingest = FakeIngest(failures=["DATA_UNAVAILABLE"] * 100)
    out = exporter(ingest, retry_attempts=1, max_queue_size=5)
    out.export([span(i, parent=1) for i in range(2, 10)])  # 8 records
    assert not out.healthy and out.queued == 5 and out.dropped == 3 and out.last_error == "DATA_UNAVAILABLE"
    ingest.failures.clear()
    assert out.force_flush(1000)
    assert sorted(int(i, 16) for i in ingest.observations) == [5, 6, 7, 8, 9] and out.healthy
    assert out.queued == 0 and out.dropped == 3


def test_O06_a_batch_the_gateway_refuses_is_dropped_and_the_next_one_still_goes() -> None:
    ingest = FakeIngest(failures=["DATA_REQUEST_INVALID"])
    out = exporter(ingest, max_observations_per_call=2)
    out.export([span(i, parent=1) for i in range(2, 6)])
    assert (
        out.dropped == 2
        and len(ingest.calls) == 2
        and sorted(ingest.observations)
        == [
            format(4, "016x"),
            format(5, "016x"),
        ]
    )


def test_O06_shutdown_flushes_what_is_queued() -> None:
    ingest = FakeIngest(failures=["DATA_UNAVAILABLE"])
    out = exporter(ingest, retry_attempts=0)
    out.export([span(2, parent=1)])
    assert out.queued == 1
    out.shutdown()
    assert out.queued == 0 and len(ingest.observations) == 1
