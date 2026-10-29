"""Tracing with Langfuse on an isolated provider (P2-07: L-01 to L-05, L-07, L-08, L-10, L-12, L-13,
L-16, L-17, X-06). Spans go to an in-memory exporter; nothing reaches the network."""

from __future__ import annotations

import json
import logging  # noqa: TID251  # the test plays a library that logs
import uuid
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.tools import tool
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.harness import EchoKind, FailingKind, Harness, Recorder, answer_after, policy_kinds
from tests.kinds.streaming import DriverHarness, DriverRun
from tests.unit.test_core_kinds import agent, chain, llm, output, variable
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.integrations.langfuse.provider import LangfuseTracer, trace_id
from zonic_runtime.models.resources.catalogs import ModelCatalog
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.observability import logging_policy

DEV = load_resources(EnvironmentModel(env="dev"))
TYPE, LEVEL, STATUS = (
    "langfuse.observation.type",
    "langfuse.observation.level",
    "langfuse.observation.status_message",
)
INPUT, OUTPUT, META = (
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.observation.metadata.",
)
KINDS = sorted({e.kind for e in DEV.manifest.node_types.values()})


def observability(**updates: Any) -> Any:
    return DEV.settings.observability.model_copy(update=updates)


def tracer(exporter: InMemorySpanExporter, prices: Any = None, **settings: Any) -> LangfuseTracer:
    return LangfuseTracer(
        observability(**settings), DEV.observability, exporter, release="test-release", prices=prices
    )


def meta(span: Any, key: str) -> Any:
    raw = (span.attributes or {}).get(META + key)
    return json.loads(raw) if isinstance(raw, str) else raw


def nodes(spans: list[Any]) -> list[Any]:
    """The node visit spans, in start order."""
    return sorted((s for s in spans if meta(s, "zonic_node_span")), key=lambda s: s.start_time)


async def traced(
    h: Harness, document: dict[str, Any], prices: Any = None, **settings: Any
) -> tuple[DriverRun, list[Any], str]:
    exporter = InMemorySpanExporter()
    lf = tracer(exporter, prices, **settings)
    run_id = str(uuid.uuid4())
    result = await DriverHarness(h, tracer=lf).run(document, run_id=run_id)
    lf.flush()
    spans = list(exporter.get_finished_spans())
    lf.shutdown()
    return result, spans, run_id


# --- L-01, L-02, L-03 --------------------------------------------------------------------------


async def test_L01_one_run_is_one_trace_with_the_run_id_session_user_tags_release_and_environment() -> None:
    h, _ = agent_harness([Turn.say("Hello.")], DEV)
    _, spans, run_id = await traced(h, chain(llm(), output()))
    assert (
        {format(s.context.trace_id, "032x") for s in spans} == {run_id.replace("-", "")} == {trace_id(run_id)}
    )
    root = next(s for s in spans if meta(s, "zonic_run_root"))
    attrs = root.attributes or {}
    assert attrs["session.id"] == "s-1" and attrs["user.id"] == "owner"
    assert attrs["langfuse.trace.name"] == "test workflow" and {"wf", "test", "mode:stream"} <= set(
        attrs["langfuse.trace.tags"]
    )
    assert (
        attrs["langfuse.release"] == "test-release"
        and attrs["langfuse.environment"] == DEV.settings.observability.environment
    )
    assert (
        otel_trace.get_tracer_provider().__class__.__name__ == "ProxyTracerProvider"
    )  # the global provider is untouched


async def test_L02_one_observation_per_node_visit_in_order_typed_from_the_manifest() -> None:
    h, _ = agent_harness([Turn.say("Hello.")], DEV)
    _, spans, _ = await traced(h, chain(llm(), variable("v"), output()))
    found = nodes(spans)
    assert [meta(s, "zonic_node_id") for s in found] == ["start", "n", "v", "out"]
    expected = {
        n: DEV.manifest.node_types[t].observation.type
        for n, t in [("start", "start"), ("n", "llm"), ("v", "variable"), ("out", "output")]
    }  # type: ignore[union-attr]
    assert {meta(s, "zonic_node_id"): (s.attributes or {})[TYPE] for s in found} == expected
    assert all(meta(s, "zonic_visit") == 1 for s in found)


async def test_L03_a_manifest_observation_override_is_applied() -> None:
    data = DEV.manifest.model_dump(by_alias=True, exclude_none=True)
    data["nodeTypes"]["variable"]["observation"] = {
        "type": "chain",
        "captureInput": "summary",
        "captureOutput": "summary",
    }
    resources = replace(DEV, manifest=NodeRuntimeManifest.model_validate(data))
    h, _ = agent_harness([], resources)
    _, spans, _ = await traced(h, chain(variable("v")))
    assert {meta(s, "zonic_node_id"): (s.attributes or {})[TYPE] for s in nodes(spans)}["v"] == "chain"


# --- L-04, L-05 --------------------------------------------------------------------------------


async def test_L04_a_loop_has_one_observation_per_visit_with_the_loop_id_and_iteration_events() -> None:
    from tests.unit.test_loops import loop_document

    recorder = Recorder()
    h = Harness(DEV, policy_kinds(KINDS, recorder, answer_after(2)), recorder=recorder)
    result, spans, _ = await traced(h, loop_document())
    agent_spans = [s for s in nodes(spans) if meta(s, "zonic_node_id") == "agent"]
    assert [meta(s, "zonic_visit") for s in agent_spans] == [1, 2, 3]
    assert len({meta(s, "zonic_loop_id") for s in agent_spans}) == 1
    events = [s for s in spans if (s.attributes or {}).get(TYPE) == "event" and s.name == "loop.iteration"]
    assert len(events) == 2 and result.outcome.status == "succeeded"


@tool
def lookup(email: str) -> str:
    """Look up a customer by e-mail."""
    return f"customer {email} is active"


async def test_L05_a_generation_with_model_and_usage_and_masked_tool_observations_nested_in_their_node() -> (
    None
):
    h, _ = agent_harness([Turn.say("Hi.", usage={"input_tokens": 5, "output_tokens": 2})], DEV)
    _, spans, _ = await traced(h, chain(llm()))
    node = next(s for s in nodes(spans) if meta(s, "zonic_node_id") == "n")
    generation = next(s for s in spans if (s.attributes or {}).get(TYPE) == "generation")
    assert generation.parent is not None and generation.parent.span_id == node.context.span_id
    attrs = generation.attributes or {}
    assert attrs["langfuse.observation.model.name"] == "scripted"
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {"input": 5, "output": 2, "total": 7}

    script = [Turn.call("lookup", {"email": "ada@example.com"}), Turn.say("Done.")]
    h, _ = agent_harness(script, DEV, tools={"ag": [lookup]})
    _, spans, _ = await traced(h, chain(agent()))
    tool_span = next(s for s in spans if (s.attributes or {}).get(TYPE) == "tool")
    assert meta(tool_span, "zonic_node_id") == "ag"
    assert "ada@example.com" not in json.dumps(dict(tool_span.attributes or {}))
    assert not [
        s for s in spans if s.name in ("model", "tools") and (s.attributes or {}).get(TYPE) == "chain"
    ]


# --- L-07, L-08, L-17 ------------------------------------------------------------------------------


def failing(policy: str) -> tuple[Harness, dict[str, Any]]:
    recorder = Recorder()
    kinds = {
        "variable": lambda s, b: (
            FailingKind(s, b, recorder, "TOOL_UNAVAILABLE") if s.node_id == "n" else EchoKind(s, b, recorder)
        )
    }
    target = {"id": "n", "type": "variable", "name": "n", "config": {"errorHandling": {"policy": policy}}}
    document = chain(
        target, variable("ok"), edges=[{"source": "start", "target": "n"}, {"source": "n", "target": "ok"}]
    )
    if policy == "route":
        document["nodes"].append(variable("err"))
        document["edges"] = [
            {"source": "start", "target": "n"},
            {"source": "n", "target": "ok"},
            {"source": "n", "target": "err", "sourceHandle": "error"},
        ]
    return Harness(DEV, kinds, recorder=recorder), document


@pytest.mark.parametrize(
    ("policy", "level", "after"),
    [("fail_run", "ERROR", None), ("continue", "WARNING", "ok"), ("route", "WARNING", "err")],
)
async def test_L07_a_node_failure_is_error_for_fail_run_and_warning_when_the_policy_handles_it(
    policy: str, level: str, after: str | None
) -> None:
    h, document = failing(policy)
    result, spans, _ = await traced(h, document)
    failed = [
        s for s in nodes(spans) if meta(s, "zonic_node_id") == "n" and not meta(s, "zonic_error_handler")
    ]
    assert failed and all((s.attributes or {})[LEVEL] == level for s in failed)
    assert "TOOL_UNAVAILABLE" in (failed[0].attributes or {})[STATUS]
    if after is None:
        assert result.outcome.status == "failed"
        root = next(s for s in spans if meta(s, "zonic_run_root"))
        assert (root.attributes or {})[LEVEL] == "ERROR"
    else:
        later = [meta(s, "zonic_node_id") for s in nodes(spans)]
        assert later.index(after) > later.index("n")


async def test_L08_a_rule_decision_is_an_event_with_the_chosen_handle() -> None:
    from tests.unit.test_core_kinds import flow

    rule = {
        "id": "r",
        "type": "rule",
        "config": {
            "rules": [
                {"ruleId": "r_big", "conditions": [{"field": "{{flow.n}}", "op": "gt", "value": 10}]},
                {"ruleId": "r_else", "isDefault": True},
            ]
        },
    }
    edges = [
        {"source": "start", "target": "r"},
        {"source": "r", "target": "a", "sourceHandle": "r_big"},
        {"source": "r", "target": "b", "sourceHandle": "r_else"},
    ]
    h, _ = agent_harness([], DEV)
    exporter = InMemorySpanExporter()
    lf = tracer(exporter)
    harness = DriverHarness(h, tracer=lf)
    run, graph = harness.prepare(
        chain(rule, variable("a"), variable("b"), edges=edges), run_id=str(uuid.uuid4())
    )
    await harness.driver().drive(run, graph, flow(n=42))
    lf.flush()
    spans = list(exporter.get_finished_spans())
    rule_span = next(s for s in nodes(spans) if meta(s, "zonic_node_id") == "r")
    events = [s for s in spans if (s.attributes or {}).get(TYPE) == "event" and s.name == "route"]
    assert (
        len(events) == 1
        and events[0].parent is not None
        and events[0].parent.span_id == rule_span.context.span_id
    )
    assert meta(events[0], "handles") == ["r_big"]


async def test_L17_the_node_record_is_unwrapped_from_the_command_and_error_handlers_belong_to_their_node() -> (
    None
):
    h, document = failing("continue")
    _, spans, _ = await traced(h, document)
    handler = [s for s in nodes(spans) if meta(s, "zonic_error_handler")]
    assert len(handler) == 1 and meta(handler[0], "zonic_node_id") == "n"
    assert (handler[0].attributes or {})[LEVEL] == "WARNING" and (handler[0].attributes or {})[
        STATUS
    ] == "TOOL_UNAVAILABLE"
    ok = next(s for s in nodes(spans) if meta(s, "zonic_node_id") == "ok")
    exported = json.loads((ok.attributes or {})[OUTPUT])
    assert (
        "update" not in exported and "goto" not in exported
    )  # the record's output, captured, not the Command


# --- L-10, X-06 ------------------------------------------------------------------------------------


async def test_L10_capture_rules_summarise_or_drop_and_mask_before_export() -> None:
    raw = {"result": {"note": "call ada@example.com", "rows": list(range(30))}}
    recorder = Recorder()
    kinds = {
        "output": lambda s, b: EchoKind(s, b, recorder, raw=raw),
        "variable": lambda s, b: EchoKind(s, b, recorder, raw={"changed": {"k": "secret value"}}),
    }
    h = Harness(DEV, kinds, recorder=recorder)
    _, spans, _ = await traced(h, chain(variable("v"), output()))
    by_node = {meta(s, "zonic_node_id"): s.attributes or {} for s in nodes(spans)}
    assert DEV.observability.capture_by_node_type["variable"].output == "summary"
    assert json.loads(by_node["v"][OUTPUT])["type"] == "object" and "secret value" not in by_node["v"][OUTPUT]
    assert DEV.observability.capture_by_node_type["output"].output == "full"
    full = json.loads(by_node["out"][OUTPUT])
    assert "ada@example.com" not in json.dumps(full) and full["note"] == "call ***"
    assert all(json.loads(a[INPUT]).get("type") == "object" for a in by_node.values() if INPUT in a)


async def test_X06_secrets_are_absent_from_events_and_observations() -> None:
    secret = {"apiKey": "sk-live-123456", "header": "Bearer abc.def.ghi", "nested": {"password": "hunter2"}}
    recorder = Recorder()
    kinds = {"output": lambda s, b: EchoKind(s, b, recorder, raw={"result": secret})}
    h = Harness(DEV, kinds, recorder=recorder)
    result, spans, _ = await traced(h, chain(output()))
    dumped = json.dumps([dict(s.attributes or {}) for s in spans])
    for value in ("sk-live-123456", "abc.def.ghi", "hunter2"):
        assert value not in dumped
    events = json.dumps([e for e in result.events if e["type"] != "run.completed"])
    for value in ("sk-live-123456", "abc.def.ghi", "hunter2"):
        assert value not in events


# --- L-12, L-13, L-16 ------------------------------------------------------------------------------


async def test_L12_with_sampling_zero_only_the_failing_run_is_exported() -> None:
    exporter = InMemorySpanExporter()
    lf = tracer(exporter, sample_rate=0.0, always_trace_errors=True)
    ok, _ = agent_harness([Turn.say("Fine.")], DEV)
    bad, document = failing("fail_run")
    ok_id, bad_id = str(uuid.uuid4()), str(uuid.uuid4())
    await DriverHarness(ok, tracer=lf).run(chain(llm(), output()), run_id=ok_id)
    await DriverHarness(bad, tracer=lf).run(document, run_id=bad_id)
    lf.flush()
    traces = {format(s.context.trace_id, "032x") for s in exporter.get_finished_spans()}
    lf.shutdown()
    assert traces == {trace_id(bad_id)}


async def test_L13_a_library_warning_during_a_run_is_a_warning_event_on_its_trace_and_nothing_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Warns(EchoKind):
        async def execute(self, ctx: Any, node_input: Any) -> Any:
            logging.getLogger("some.library").warning("disk almost full")
            logging.getLogger("some.library").info("below the threshold")
            return await super().execute(ctx, node_input)

    restore = logging_policy.install()
    try:
        recorder = Recorder()
        h = Harness(DEV, {"variable": lambda s, b: Warns(s, b, recorder)}, recorder=recorder)
        _, spans, _ = await traced(h, chain(variable("v")))
        logging.getLogger("some.library").warning("outside any run")
    finally:
        restore()
    events = [s for s in spans if s.name == "log.some.library"]
    assert len(events) == 1 and (events[0].attributes or {})[LEVEL] == "WARNING"
    assert meta(events[0], "message") == "disk almost full"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


async def test_L16_with_tracing_off_runs_are_unaffected() -> None:
    plain, _ = agent_harness([Turn.say("Same answer.")], DEV)
    traced_h, _ = agent_harness([Turn.say("Same answer.")], DEV)
    without = await DriverHarness(plain).run(chain(llm(), output()))
    with_trace, _, _ = await traced(traced_h, chain(llm(), output()))
    strip = [
        [
            {k: v for k, v in e.items() if k not in ("id", "ts", "runId")}
            | {"data": {k: v for k, v in e["data"].items() if k != "durationMs"}}
            for e in r.events
        ]
        for r in (without, with_trace)
    ]
    assert strip[0] == strip[1] and without.outcome.result == with_trace.outcome.result


# --- Cost, workflow graph, bound node input ---------------------------------------------------------


def priced_catalog() -> ModelCatalog:
    """The dev catalog plus an entry priced for the scripted model ($1 in, $3 out per 1M tokens)."""
    data = DEV.models.model_dump(by_alias=True, exclude_none=True)
    data["models"]["scripted-model"] = {
        **data["models"][DEV.models.default_model],
        "provider": "openai",
        "modelId": "scripted",
        "pricing": {"inputPerMillion": 1.0, "outputPerMillion": 3.0},
    }
    return ModelCatalog.model_validate(data)


async def test_a_generation_is_costed_from_the_model_catalog_price() -> None:
    h, _ = agent_harness([Turn.say("Hi.", usage={"input_tokens": 5, "output_tokens": 2})], DEV)
    _, spans, _ = await traced(h, chain(llm()), prices=priced_catalog())
    generation = next(s for s in spans if (s.attributes or {}).get(TYPE) == "generation")
    cost = json.loads((generation.attributes or {})["langfuse.observation.cost_details"])
    assert cost == {"input": 5e-06, "output": 6e-06, "total": 1.1e-05}

    h, _ = agent_harness([Turn.say("Hi.", usage={"input_tokens": 5, "output_tokens": 2})], DEV)
    _, spans, _ = await traced(h, chain(llm()))  # no catalog, or a model without a price: no cost
    generation = next(s for s in spans if (s.attributes or {}).get(TYPE) == "generation")
    assert "langfuse.observation.cost_details" not in (generation.attributes or {})


async def test_the_root_observation_carries_the_workflow_graph() -> None:
    h, _ = agent_harness([Turn.say("Hello.")], DEV)
    _, spans, _ = await traced(h, chain(llm(model="nemotron-3-super"), variable("v"), output()))
    root = next(s for s in spans if meta(s, "zonic_run_root"))
    graph = meta(root, "zonic_workflow_graph")
    assert graph["entry"] == "start"
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert set(by_id) == {"start", "n", "v", "out"}
    assert by_id["n"]["type"] == "llm" and by_id["n"]["model"] == "nemotron-3-super"
    assert {(e["from"], e["to"]) for e in graph["edges"]} == {("start", "n"), ("n", "v"), ("v", "out")}


async def test_a_node_input_is_its_bound_ports_captured_as_its_type_says() -> None:
    h, _ = agent_harness([Turn.say("Hello.")], DEV)
    _, spans, _ = await traced(h, chain(llm(), variable("v"), output()))
    by_node = {meta(s, "zonic_node_id"): s.attributes or {} for s in nodes(spans)}
    assert DEV.observability.capture_by_node_type["llm"].input == "full"
    bound = json.loads(by_node["n"][INPUT])
    assert "prompt" in bound and bound.get("type") != "object"  # the ports, not the run state
    assert DEV.observability.capture_by_node_type["variable"].input == "summary"
    assert json.loads(by_node["v"][INPUT])["type"] == "object"


def test_run_usage_is_costed_with_cached_tokens_and_dated_model_names() -> None:
    from langchain_core.messages import AIMessage

    from zonic_runtime.engine.agents.messages import usage_of

    message = AIMessage(
        content="ok",
        response_metadata={"model_name": "nvidia/nemotron-3-super-120b-a12b"},
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "input_token_details": {"cache_read": 200},
        },
    )
    usage = usage_of([message], DEV.models)
    # 800 fresh input x $0.30 + 200 cached x $0.06 + 500 output x $0.90, per 1M tokens
    assert usage is not None and usage.cost == pytest.approx(0.000702)
    assert usage_of([message]) is not None and usage_of([message]).cost is None  # type: ignore[union-attr]
    mini = DEV.models.price_for("gpt-4.1-mini-2025-04-14")
    assert mini is not None and mini.output_per_million == 1.6
    assert DEV.models.price_for("nemotron-3-ultra").input_per_million == 1.0  # type: ignore[union-attr]
    assert DEV.models.price_for("unknown-model") is None


def test_reasoning_tokens_are_costed_at_the_output_price() -> None:
    from zonic_runtime.integrations.langfuse.export import Enricher
    from zonic_runtime.integrations.langfuse.masker import Masker

    # A Nemotron 3 Ultra call as Langfuse records it: reasoning tokens outside ``output``.
    usage = {"input": 815, "output": 128, "output_reasoning": 77, "input_cache_read": 0, "total": 1020}
    attrs = {
        TYPE: "generation",
        "langfuse.observation.model.name": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "langfuse.observation.usage_details": json.dumps(usage),
    }
    cost = Enricher(Masker(DEV.observability), DEV.models).cost(attrs)
    # 815 x $1 + (128 + 77) x $3, per 1M tokens
    assert cost == {"input": 0.000815, "output": 0.000384, "output_reasoning": 0.000231, "total": 0.00143}
