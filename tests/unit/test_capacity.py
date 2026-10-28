"""Capacity inside one run and one process (P2-05: Q-03, Q-04, Q-06 to Q-09, Q-16 pool part, R-02).

Timing here is coarse on purpose: these tests prove the bounds (permits, queues, coalescing
windows); the budgets on the target task are P5-02's.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import replace
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
from langchain_core.runnables.config import run_in_executor
from langchain_core.tools import tool

from tests.fakes.clock import FakeClock
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.harness import DEFAULT_INPUT, Harness, Recorder, SlowKind
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.governor import ModelGovernor, ModelGovernors
from zonic_runtime.engine.agents.model_factory import provider_builders
from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.errors import error_code
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.http.pools import BedrockClients, install_blocking_executor
from zonic_runtime.integrations.langfuse.masker import Masker
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeOutcome
from zonic_runtime.models.events import EventDraft
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.models.resources.catalogs import ModelQuota
from zonic_runtime.run.events.bus import Coalescing, RunStream
from zonic_runtime.runtime.process_pool import CpuPool

DEV = load_resources(EnvironmentModel(env="dev"))
USER = [{"role": "user", "content": "{{system.userQuery}}"}]


def settings(resources: RuntimeResources, **sections: dict[str, Any]) -> RuntimeResources:
    updates = {k: getattr(resources.settings, k).model_copy(update=v) for k, v in sections.items()}
    return replace(resources, settings=resources.settings.model_copy(update=updates))


def quota(resources: RuntimeResources, **values: Any) -> RuntimeResources:
    """Every catalog model with the given quota fields."""
    models = {
        code: entry.model_copy(update={"quota": entry.quota.model_copy(update=values)})
        for code, entry in resources.models.models.items()
    }
    return replace(resources, models=resources.models.model_copy(update={"models": models}))


def fan(node_ids: list[str], kind: str, **config: Any) -> dict[str, Any]:
    """Start, the nodes in parallel, an Output joining them."""
    nodes = [{"id": n, "type": kind, "name": n, "config": dict(config)} for n in node_ids]
    return {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            *nodes,
            {"id": "out", "type": "output", "config": {"outputMapping": []}},
        ],
        "edges": [
            *({"source": "start", "target": n} for n in node_ids),
            *({"source": n, "target": "out"} for n in node_ids),
        ],
    }


# --- Q-03 --------------------------------------------------------------------------------------


def fifty_branches() -> dict[str, Any]:
    """Start, a Rule with 49 conditions and a default, and one Variable node per branch."""
    rules = [
        {"ruleId": f"r_{i}", "conditions": [{"field": "{{flow.n}}", "op": "eq", "value": i}]}
        for i in range(49)
    ]
    rules.append({"ruleId": "r_else", "isDefault": True, "conditions": []})
    branches = [f"b{i}" for i in range(49)] + ["b_else"]
    return {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "r", "type": "rule", "config": {"rules": rules}},
            *(
                {
                    "id": b,
                    "type": "variable",
                    "name": b,
                    "config": {"updates": [{"path": f"flow.{b}", "value": 1}]},
                }
                for b in branches
            ),
        ],
        "edges": [
            {"source": "start", "target": "r"},
            *(
                {"source": "r", "target": b, "sourceHandle": r["ruleId"]}
                for b, r in zip(branches, rules, strict=True)
            ),
        ],
    }


async def test_Q03_a_rule_with_50_branches_runs_only_the_taken_branch() -> None:
    document = fifty_branches()
    branches = [f"b{i}" for i in range(49)] + ["b_else"]
    h, _ = agent_harness([], DEV)
    inputs = {**DEFAULT_INPUT, "flow": {"values": {"n": 37}, "provenance": {}, "conflicts": []}}
    final, *_ = await h.run(document, inputs)
    ran = set(final["nodes"]) & set(branches)
    assert ran == {"b37"}
    assert final["nodes"]["r"]["output"]["handle"] == "r_37"


# --- Q-04 --------------------------------------------------------------------------------------


def _delta(text: str, node: str = "llm") -> EventDraft:
    return EventDraft(type="token.delta", data={"text": text}, node_id=node, path=node)


async def test_Q04_200_tokens_a_second_give_at_most_one_frame_per_window_after_the_first() -> None:
    window = 0.05
    stream = RunStream(
        "r", DEV.settings.stream, DEV.settings.sse.max_event_bytes, coalescing=Coalescing(50, 4096)
    )
    first = stream.publish(_delta("t0 "))
    assert first is not None and first["seq"] == 1  # the first delta is not held
    started = time.monotonic()
    for i in range(1, 200):
        stream.publish(_delta(f"t{i} "))
        await asyncio.sleep(0.005)
    elapsed = time.monotonic() - started
    stream.publish(EventDraft(type="node.finished", data={}, node_id="llm", path="llm"))
    stream.close()
    events = [e async for e in stream.subscribe(1)]
    deltas = [e for e in events if e["type"] == "token.delta"]
    assert len(deltas) - 1 <= elapsed / window + 2
    assert len(deltas) < 200 / 2  # joined, not one frame per token
    assert "".join(e["data"]["text"] for e in deltas) == "".join(f"t{i} " for i in range(200))
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))  # no gaps
    assert events[-1]["type"] == "node.finished"  # the held text went out before it


async def test_Q04_a_held_delta_goes_out_at_the_byte_cap_and_before_another_nodes_delta() -> None:
    stream = RunStream(
        "r", DEV.settings.stream, DEV.settings.sse.max_event_bytes, coalescing=Coalescing(10_000, 8)
    )
    stream.publish(_delta("a"))
    stream.publish(_delta("bb"))
    stream.publish(_delta("cc"))
    assert stream.last_seq == 1  # held: under 8 bytes, inside the window
    stream.publish(_delta("dddd"))  # 8 bytes held: out
    assert stream.last_seq == 2
    stream.publish(_delta("e"))
    stream.publish(_delta("x", node="other"))  # another node's first delta flushes "e" first
    texts = [(e["nodeId"], e["data"]["text"]) for e in list(stream._buffer)]
    assert texts == [("llm", "a"), ("llm", "bbccdddd"), ("llm", "e"), ("other", "x")]
    stream.close()


async def test_Q04_without_coalescing_every_delta_is_its_own_event() -> None:
    stream = RunStream("r", DEV.settings.stream, DEV.settings.sse.max_event_bytes, clock=FakeClock())
    for i in range(5):
        assert stream.publish(_delta(str(i))) is not None
    assert stream.last_seq == 5


# --- Q-06 --------------------------------------------------------------------------------------


class ContainerKind(BaseKind):
    """A sub-flow stand-in: runs a fan-out child graph and holds no node permit meanwhile."""

    holds_permit: ClassVar[bool] = False

    def __init__(self, spec: NodeSpec, blueprint: Blueprint, child: Any) -> None:
        self.spec, self.child = spec, child

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        await invoke_child(self.child, dict(DEFAULT_INPUT), ctx.runtime, ctx.config)
        return NodeOutcome(output={"text": "done"}, activate=["default"])


class Probe:
    """Counts leaf nodes executing at once across the run."""

    def __init__(self) -> None:
        self.now = 0
        self.peak = 0


class LeafKind(BaseKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint, probe: Probe) -> None:
        self.spec, self.probe = spec, probe

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.probe.now += 1
        self.probe.peak = max(self.probe.peak, self.probe.now)
        await asyncio.sleep(0.02)
        self.probe.now -= 1
        return NodeOutcome(output={"text": "leaf"}, activate=["default"])


async def test_Q06_nested_fan_outs_never_run_more_leaves_than_max_concurrency_per_run_and_never_deadlock() -> (
    None
):
    resources = settings(DEV, engine={"max_concurrency_per_run": 3})
    probe = Probe()
    leaves = Harness(resources, {"variable": lambda s, b: LeafKind(s, b, probe)})
    child = leaves.compile(leaves.plan(fan([f"leaf{i}" for i in range(4)], "variable", updates=[])))
    root = Harness(resources, {"llm": lambda s, b: ContainerKind(s, b, child)})
    blueprint = root.plan(fan([f"sub{i}" for i in range(4)], "llm", messages=USER))
    graph = root.compile(blueprint)
    context = root.context(blueprint)
    await asyncio.wait_for(
        graph.ainvoke(dict(DEFAULT_INPUT), root.config(blueprint), context=context), timeout=10
    )
    assert probe.peak == 3 and context.permits.peak_nodes == 3  # 16 leaves, 4 containers
    assert context.permits.available == 3


async def test_Q06_slow_parallel_leaves_wait_for_a_permit() -> None:
    resources = settings(DEV, engine={"max_concurrency_per_run": 2})
    recorder = Recorder()
    h = Harness(resources, {"variable": lambda s, b: SlowKind(s, b, recorder, 0.02)}, recorder=recorder)
    blueprint = h.plan(fan([f"v{i}" for i in range(6)], "variable", updates=[]))
    context = h.context(blueprint)
    await h.compile(blueprint).ainvoke(dict(DEFAULT_INPUT), h.config(blueprint), context=context)
    assert len(recorder.calls) == 6 and context.permits.peak_nodes == 2


# --- Q-07 --------------------------------------------------------------------------------------


class _InFlight:
    now = 0
    peak = 0


@tool
async def slow_lookup(i: int) -> str:
    """Look something up, slowly."""
    _InFlight.now += 1
    _InFlight.peak = max(_InFlight.peak, _InFlight.now)
    await asyncio.sleep(0.03)
    _InFlight.now -= 1
    return f"result {i}"


async def test_Q07_ten_tool_calls_in_one_turn_run_at_most_max_concurrent_per_turn_at_once() -> None:
    _InFlight.now = _InFlight.peak = 0
    script = [Turn.calls(*[("slow_lookup", {"i": i}) for i in range(10)]), Turn.say("All done.")]
    resources = settings(DEV, tools={"max_concurrent_per_turn": 4})
    h, _ = agent_harness(script, resources, tools={"ag": [slow_lookup]})
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "ag", "type": "agent", "name": "ag", "config": {"systemPrompt": "You help."}},
        ],
        "edges": [{"source": "start", "target": "ag"}],
    }
    blueprint = h.plan(document)
    context = h.context(blueprint)
    final = await h.compile(blueprint).ainvoke(dict(DEFAULT_INPUT), h.config(blueprint), context=context)
    assert len(final["nodes"]["ag"]["output"]["toolCalls"]) == 10
    assert _InFlight.peak == 4 and context.permits.peak_tools == 4


# --- Q-08, R-02 ----------------------------------------------------------------------------------


def _quota(**values: Any) -> ModelQuota:
    base = {
        "max_in_flight": 20,
        "requests_per_minute": 100_000,
        "tokens_per_minute": 10_000_000,
        "max_queue_wait_milliseconds": 10_000,
    }
    return ModelQuota.model_validate({**base, **values})


async def test_Q08_100_concurrent_calls_on_one_model_never_exceed_max_in_flight() -> None:
    governor = ModelGovernor("m", _quota())

    async def call() -> None:
        async with governor.permit(50):
            await asyncio.sleep(0.01)

    await asyncio.gather(*(call() for _ in range(100)))
    assert governor.peak == 20 and governor.in_flight == 0 and governor.queued == 0


async def test_Q08_a_call_still_queued_at_the_deadline_is_model_busy() -> None:
    governor = ModelGovernor("m", _quota(max_in_flight=1, max_queue_wait_milliseconds=50))
    held = asyncio.Event()

    async def holder() -> None:
        async with governor.permit():
            held.set()
            await asyncio.sleep(0.3)

    task = asyncio.create_task(holder())
    await held.wait()
    with pytest.raises(ZonicError) as raised:
        async with governor.permit():
            pass
    assert raised.value.code == "MODEL_BUSY" and raised.value.details["reason"] == "in flight"
    assert governor.queued == 0
    await task
    async with governor.permit():  # the refused wait gave nothing away
        assert governor.in_flight == 1


async def test_Q08_the_request_and_token_buckets_refuse_what_cannot_fit_before_the_deadline() -> None:
    now = [0.0]
    governor = ModelGovernor(
        "m", _quota(requests_per_minute=2, max_queue_wait_milliseconds=1000), monotonic=lambda: now[0]
    )
    for _ in range(2):
        async with governor.permit():
            pass
    with pytest.raises(ZonicError) as raised:  # the third request needs 30 s of refill
        async with governor.permit():
            pass
    assert raised.value.details["reason"] == "rate" and governor.in_flight == 0
    now[0] = 30.0
    async with governor.permit():
        pass


def test_Q08_governors_are_shared_per_model_id() -> None:
    governors = ModelGovernors()
    assert governors.get("a", _quota()) is governors.get("a", _quota()) is not governors.get("b", _quota())


async def test_Q08_an_llm_node_calls_its_model_under_the_models_governor() -> None:
    h, _ = agent_harness([Turn.say("Hi.")], DEV)
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "n", "type": "llm", "name": "n", "config": {"messages": USER}},
        ],
        "edges": [{"source": "start", "target": "n"}],
    }
    await h.run(document)
    [governor] = h.agents.models.governors.all()
    assert governor.model_id == DEV.models.default_model and governor.peak == 1 and governor.in_flight == 0


async def test_R02_a_throttling_burst_across_runs_retries_with_backoff_and_no_run_hangs() -> None:
    resources = quota(DEV, max_in_flight=1, max_queue_wait_milliseconds=60)
    script = [Turn.say(f"Answer {i}.", delay=0.05) for i in range(8)]
    h, _ = agent_harness(script, resources)
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "n", "type": "llm", "name": "n", "config": {"messages": USER}},
        ],
        "edges": [{"source": "start", "target": "n"}],
    }
    started = time.monotonic()
    outcomes = await asyncio.wait_for(
        asyncio.gather(*(h.run(document, run_id=f"run-{i}") for i in range(8)), return_exceptions=True),
        timeout=30,
    )
    [governor] = h.agents.models.governors.all()
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert all(error_code(f) == "MODEL_BUSY" for f in failures), failures
    assert len(failures) < len(outcomes)  # retries got most of the burst through
    assert governor.peak == 1 and governor.in_flight == 0 and governor.queued == 0
    assert time.monotonic() - started < 30


# --- Q-09 --------------------------------------------------------------------------------------


def test_Q09_100_bedrock_models_share_one_client_per_region() -> None:
    created: list[str | None] = []

    def create(region: str | None, _: Any) -> Any:
        created.append(region)
        return MagicMock(name=f"bedrock-runtime {region}")

    clients = BedrockClients(DEV.settings.models, create)
    found = {id(clients("us-east-1")) for _ in range(100)}
    assert len(found) == 1 and created == ["us-east-1"]
    assert clients("eu-west-1") is not clients("us-east-1") and created == ["us-east-1", "eu-west-1"]


def test_Q09_the_model_factory_hands_the_shared_client_to_every_bedrock_model() -> None:
    clients = BedrockClients(DEV.settings.models, lambda region, _: MagicMock(name=f"client {region}"))
    build = provider_builders(lambda _: None, clients)["bedrock_converse"]
    entries = [e for e in DEV.models.models.values() if e.provider == "bedrock_converse"]
    assert entries, "the catalog has Bedrock models"
    built = [build(entries[0], {"temperature": 0.1}) for _ in range(3)]
    assert len({id(m.client) for m in built}) == 1  # type: ignore[attr-defined]
    assert clients.regions == [entries[0].region]


async def test_Q09_blocking_calls_never_use_more_threads_than_executor_threads() -> None:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    names: set[str] = set()

    def blocking() -> None:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            names.add(threading.current_thread().name)
        time.sleep(0.01)
        with lock:
            state["now"] -= 1

    executor = install_blocking_executor(DEV.settings.models.model_copy(update={"executor_threads": 8}))
    try:
        await asyncio.gather(*(run_in_executor(None, blocking) for _ in range(100)))  # LangChain's path
    finally:
        executor.shutdown(wait=True)
    assert state["peak"] == 8 and len(names) <= 8
    assert all(n.startswith("zonic-blocking") for n in names)


# --- Q-16 (the process pool; the AST rule is in tests/principles) ---------------------------------


async def test_Q16_a_cpu_heavy_mask_goes_to_the_process_pool_and_a_small_one_stays_inline() -> None:
    pool = CpuPool(DEV.settings.cpu.model_copy(update={"offload_bytes": 1024, "workers": 1}))
    masker = Masker(DEV.observability)
    big = {"rows": [{"email": f"user{i}@example.test", "password": "x"} for i in range(200)]}
    small = {"password": "x"}
    try:
        masked_small = await pool.run(20, masker.mask, small)
        assert pool.offloaded == 0
        masked_big = await pool.run(20_000, masker.mask, big)
        assert pool.offloaded == 1 and masked_big == masker.mask(big) and masked_small == masker.mask(small)
        assert await pool.run(20_000, os.getpid) != os.getpid()
    finally:
        pool.shutdown()
