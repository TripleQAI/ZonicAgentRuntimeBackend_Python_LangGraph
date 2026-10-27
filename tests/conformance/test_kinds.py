"""The node kind conformance suite (P1-10: C-01 to C-16).

Every kind runs through the real node template in a compiled one-node graph and in a join
graph. Until the real kinds arrive (P2-02 onward) the suite runs the test kinds of
``tests/kinds/harness.py``; each real kind adds itself to ``SUBJECTS``.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Any

import pytest
from langgraph.types import Command

from tests.kinds.harness import (
    AskKind,
    EchoKind,
    FailingKind,
    Harness,
    PassKind,
    Recorder,
    RecordingHooks,
    SlowKind,
    with_checkpointer,
)
from tests.unit.test_compiler import _reaches
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.context import RunContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.engine import Usage
from zonic_runtime.models.nodes import config_model_for
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest

DEV = load_resources(EnvironmentModel(env="dev"))
MESSAGES = [{"role": "user", "content": "{{flow.topic}}"}]


def doc(
    target: dict[str, Any],
    shape: str,
    extra: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    nodes = [{"id": "start", "type": "start", "config": {}}, target, *(extra or [])]
    if shape == "one":
        links = [{"source": "start", "target": target["id"]}]
    else:  # a join: two parallel branches into the node under test
        nodes += [{"id": "a", "type": "variable"}, {"id": "b", "type": "variable"}]
        links = [
            {"source": "start", "target": "a"},
            {"source": "start", "target": "b"},
            {"source": "a", "target": target["id"]},
            {"source": "b", "target": target["id"]},
        ]
    return {"schemaVersion": "2.0", "nodes": nodes, "edges": [*links, *(edges or [])]}


def llm(node_id: str = "n", **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "llm", "name": node_id, "config": {"messages": MESSAGES, **config}}


def harness(factory: Any, kind: str = "llm", resources: Any = DEV, **kwargs: Any) -> Harness:
    recorder = Recorder()
    return Harness(resources, {kind: lambda s, b: factory(s, b, recorder)}, recorder=recorder, **kwargs)


SUBJECTS = {"echo": lambda s, b, r: EchoKind(s, b, r)}
SHAPES = ["one", "join"]
INPUT = {"flow": {"values": {"topic": "billing"}, "provenance": {}, "conflicts": []}}


@pytest.fixture(params=sorted(SUBJECTS))
def subject(request: pytest.FixtureRequest) -> Any:
    return SUBJECTS[request.param]


@pytest.mark.parametrize("shape", SHAPES)
async def test_C01_configuration_model_and_manifest_defaults(subject: Any, shape: str) -> None:
    h = harness(subject)
    final, _, blueprint, _ = await h.run(doc(llm(), shape), INPUT)
    spec = blueprint.nodes["n"]
    assert isinstance(spec.config, config_model_for("llm"))
    assert spec.timeout_seconds == DEV.manifest.defaults.timeout_seconds
    assert spec.retry == DEV.manifest.node_types["llm"].retry
    assert final["nodes"]["n"]["status"] == "succeeded"


@pytest.mark.parametrize("shape", SHAPES)
async def test_C02_template_fields_are_resolved_and_others_untouched(subject: Any, shape: str) -> None:
    h = harness(subject)
    target = llm(responseFormat="{{flow.topic}}")
    final, *_ = await h.run(doc(target, shape), INPUT)
    config = final["nodes"]["n"]["output"]["config"]
    assert config["messages"][0]["content"] == "billing"
    assert config["response_format"] == "{{flow.topic}}"


async def test_C03_a_timeout_is_node_timeout_through_the_timeout_policy() -> None:
    h = harness(lambda s, b, r: SlowKind(s, b, r, 5), "variable")
    with pytest.raises(ZonicError) as caught:
        await h.run(doc({"id": "n", "type": "variable", "config": {"timeoutSeconds": 0.05}}, "one"))
    assert caught.value.code == "NODE_TIMEOUT"


async def test_C04_cancellation_reaches_the_kind() -> None:
    h = harness(lambda s, b, r: SlowKind(s, b, r, 30), "variable")
    task = asyncio.create_task(h.run(doc({"id": "n", "type": "variable"}, "one")))
    await asyncio.wait_for(h.recorder.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.recorder.cancelled == ["n"]


def failing(policy: str) -> tuple[Harness, dict[str, Any]]:
    h = harness(lambda s, b, r: FailingKind(s, b, r, "TOOL_UNAVAILABLE"), "variable")
    target = {"id": "n", "type": "variable", "config": {"errorHandling": {"policy": policy}}}
    extra = [{"id": "ok", "type": "output"}, {"id": "err", "type": "output"}]
    edges = [{"source": "n", "target": "ok"}, {"source": "n", "target": "err", "sourceHandle": "error"}]
    return h, doc(target, "one", extra, edges)


async def test_C05_fail_run_ends_the_run_with_the_node_code() -> None:
    h, document = failing("fail_run")
    with pytest.raises(ZonicError) as caught:
        await h.run(document)
    assert caught.value.code == "TOOL_UNAVAILABLE"


async def test_C06_continue_follows_default_and_records_the_error() -> None:
    await _policy_follows("continue", "ok")


async def test_C07_route_follows_error_and_records_the_error() -> None:
    await _policy_follows("route", "err")


async def _policy_follows(policy: str, next_node: str) -> None:
    h, document = failing(policy)
    final, events, *_ = await h.run(document)
    assert next_node in final["nodes"] and ({"ok", "err"} - {next_node}).isdisjoint(final["nodes"])
    assert (
        final["nodes"]["n"]["status"] == "failed"
        and final["nodes"]["n"]["error"]["code"] == "TOOL_UNAVAILABLE"
    )
    assert [e["code"] for e in final["errors"]] == ["TOOL_UNAVAILABLE"]
    assert any(e["event"] == "node.failed" for e in events)


def fast_retry(resources: Any = DEV) -> Any:
    data = resources.manifest.model_dump(by_alias=True, exclude_none=True)
    data["nodeTypes"]["llm"]["retry"].update(
        initialIntervalSeconds=0.001, maxIntervalSeconds=0.002, jitter=False
    )
    return replace(resources, manifest=NodeRuntimeManifest.model_validate(data))


@pytest.mark.parametrize(("code", "attempts", "ok"), [("MODEL_BUSY", 3, True), ("INPUT_INVALID", 1, False)])
async def test_C08_retry_only_for_retryable_codes(code: str, attempts: int, ok: bool) -> None:
    h = harness(lambda s, b, r: FailingKind(s, b, r, code, failures=2), "llm", fast_retry())
    if ok:
        final, *_ = await h.run(doc(llm(), "one"))
        assert final["nodes"]["n"]["output"]["text"] == "recovered"
    else:
        with pytest.raises(ZonicError, match=code):
            await h.run(doc(llm(), "one"))
    assert h.recorder.attempts["n"] == attempts


@pytest.mark.parametrize("shape", SHAPES)
async def test_C09_node_started_then_node_finished(subject: Any, shape: str) -> None:
    _, events, *_ = await harness(subject).run(doc(llm(), shape), INPUT)
    mine = [e["event"] for e in events if e["nodeId"] == "n"]
    assert mine == ["node.started", "node.finished"]
    finished = next(e for e in events if e["nodeId"] == "n" and e["event"] == "node.finished")
    assert finished["status"] == "succeeded" and finished["visit"] == 1


async def test_C10_output_map_schema_and_aliases() -> None:
    reader = {
        "id": "r",
        "type": "variable",
        "config": {"variableUpdates": [{"path": "flow.got", "value": "{{nodes.n.output.result}}"}]},
    }
    h = harness(lambda s, b, r: EchoKind(s, b, r, raw={"text": "mapped", "stopReason": "stop", "ignored": 1}))
    final, *_ = await h.run(doc(llm(), "one", [reader], [{"source": "n", "target": "r"}]), INPUT)
    output = final["nodes"]["n"]["output"]
    assert output["text"] == "mapped" and output["finishReason"] == "stop" and "ignored" not in output
    assert final["flow"]["values"]["got"] == "mapped"
    bad = harness(lambda s, b, r: EchoKind(s, b, r, raw={"text": 5}))
    with pytest.raises(ZonicError, match="OUTPUT_SCHEMA_INVALID"):
        await bad.run(doc(llm(), "one"))


@pytest.mark.parametrize("shape", SHAPES)
async def test_C11_usage_is_reported(shape: str) -> None:
    usage = Usage(input_tokens=7, output_tokens=3, total_tokens=10, model_calls=1)
    final, events, *_ = await harness(lambda s, b, r: EchoKind(s, b, r, usage=usage)).run(
        doc(llm(), shape), INPUT
    )
    assert final["usage"]["totalTokens"] == 10 and final["nodes"]["n"]["usage"]["inputTokens"] == 7
    assert (
        next(e for e in events if e["nodeId"] == "n" and e["event"] == "node.finished")["usage"][
            "totalTokens"
        ]
        == 10
    )


async def test_C12_activated_handles_become_goto_targets() -> None:
    h = harness(lambda s, b, r: EchoKind(s, b, r, activate=["error"]))
    extra = [{"id": "ok", "type": "output"}, {"id": "err", "type": "output"}]
    edges = [{"source": "n", "target": "ok"}, {"source": "n", "target": "err", "sourceHandle": "error"}]
    final, *_ = await h.run(doc(llm(), "one", extra, edges), INPUT)
    assert "err" in final["nodes"] and "ok" not in final["nodes"]


async def test_C13_observation_type_travels_in_the_node_metadata(subject: Any) -> None:
    _, _, _, graph = await harness(subject).run(doc(llm(), "one"), INPUT)
    observation = DEV.manifest.node_types["llm"].observation
    assert observation is not None
    assert graph.builder.nodes["n"].metadata["zonic_observation_type"] == observation.type
    assert graph.builder.nodes["start"].metadata["zonic_observation_type"] == "span"


@pytest.mark.parametrize("shape", SHAPES)
async def test_C14_every_hook_point_in_order(subject: Any, shape: str) -> None:
    h = harness(subject)
    h.hooks = RecordingHooks(h.recorder)
    await h.run(doc(llm(), shape), INPUT)
    mine = [point for point, node_id in h.recorder.hooks if node_id == "n"]
    assert mine == ["before", "after"]
    failing_h, document = failing("continue")
    failing_h.hooks = RecordingHooks(failing_h.recorder)
    await failing_h.run(document)
    assert [p for p, n in failing_h.recorder.hooks if n == "n"] == ["before", "error"]


async def test_C15_no_run_data_is_retained_in_the_kind_after_the_run(subject: Any) -> None:
    h = harness(subject)
    _, _, _, graph = await h.run(doc(llm(), "join"), INPUT)
    runner = graph.builder.nodes["n"].runnable
    assert not _reaches(runner, RunContext)


async def test_C16_a_completed_sibling_is_not_repeated_after_an_interrupt_in_its_superstep() -> None:
    recorder = Recorder()
    kinds = {
        "variable": lambda s, b: EchoKind(s, b, recorder) if s.node_id == "a" else PassKind(s, b),
        "interrupt": lambda s, b: AskKind(s, b, recorder),
    }
    h = with_checkpointer(Harness(DEV, kinds, recorder=recorder))
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "variable"},
            {"id": "ask", "type": "human_input"},
        ],
        "edges": [{"source": "start", "target": "a"}, {"source": "start", "target": "ask"}],
    }
    blueprint = h.plan(document)
    graph = h.compile(blueprint)
    config, context = h.config(blueprint), h.context(blueprint)
    first = await graph.ainvoke({}, config=config, context=context)
    assert "__interrupt__" in first and recorder.calls == ["a"]
    final = await graph.ainvoke(Command(resume="yes"), config=config, context=context)
    assert recorder.calls == ["a", "ask"]
    assert final["nodes"]["ask"]["output"]["value"] == "yes"
    assert copy.deepcopy(final["visits"])["a"] == 1
