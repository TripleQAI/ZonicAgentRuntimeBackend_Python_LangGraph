"""``RunState``, reducers, patches and ``RunContext`` (P1-08: E-10, E-21, E-22, B-06, B-07)."""

from __future__ import annotations

import copy
import itertools
import operator
from functools import reduce
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from tests.fakes.clock import FakeClock
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.context import Deadline, RunEmitter, RunPermits
from zonic_runtime.engine.patches import flow_patch, to_update, update_messages
from zonic_runtime.engine.scope import template_scope
from zonic_runtime.engine.state import (
    RUN_VISITS,
    RunState,
    add_counts,
    add_usage,
    first_result,
    keyed_values,
    merge_entries,
    merge_keyed,
    merge_nodes,
    without_seq,
)
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.engine import FlowPatch, NodeRecord, StatePatch, ThreadMessage, TimelineEntry, Usage
from zonic_runtime.workflow.loader.loader import WorkflowLoader
from zonic_runtime.workflow.templates.conditions import ConditionEvaluator
from zonic_runtime.workflow.templates.resolver import TemplateContext, TemplateResolver
from zonic_runtime.workflow.templates.updates import UpdateWriter

DEV = load_resources(EnvironmentModel(env="dev"))
WRITABLE = DEV.settings.templates.writable_system_keys
RESOLVER = TemplateResolver(DEV.templates)
WRITER = UpdateWriter(
    RESOLVER,
    ConditionEvaluator(DEV.dialects.canonical_operators),
    writable=DEV.templates.writable,
    writable_system_keys=WRITABLE,
    state_ops=DEV.builder.state_ops,
)


def fold(reducer: Any, updates: list[Any], initial: Any = None) -> Any:
    return reduce(reducer, updates, initial)


# --- E-21 (DR-1): order-independent reducers ----------------------------------------------------

writers = st.lists(st.sampled_from(["a", "b", "c", "n10", "n2"]), min_size=1, max_size=5, unique=True)
keys = st.sampled_from(["x", "y", "z"])
values = st.one_of(st.integers(), st.text(max_size=3), st.none())


@st.composite
def same_step_patches(draw: Any) -> list[dict[str, Any]]:
    step = draw(st.integers(min_value=0, max_value=3))
    patches = []
    for writer in draw(writers):
        set_ = draw(st.dictionaries(keys, values, max_size=3))
        remove = draw(st.lists(keys.filter(lambda k, s=set_: k not in s), max_size=1, unique=True))
        patches.append({"step": step, "writer": writer, "set": set_, "remove": remove})
    return patches


@settings(max_examples=200, deadline=None)
@given(same_step_patches(), st.randoms(use_true_random=False))
def test_E21_flow_is_the_same_under_any_order_of_same_step_writes(patches: list[Any], rnd: Any) -> None:
    base = fold(merge_keyed, [{"step": 0, "writer": "start", "set": {"x": "old"}, "remove": []}])
    shuffled = patches[:]
    rnd.shuffle(shuffled)
    assert fold(merge_keyed, patches, copy.deepcopy(base)) == fold(merge_keyed, shuffled, copy.deepcopy(base))


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 3), st.sampled_from(["a", "b", "out_1", "out_2"])),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    st.randoms(use_true_random=False),
)
def test_E21_result_keeps_the_lowest_step_then_node_id(writes: list[tuple[int, str]], rnd: Any) -> None:
    updates = [{"value": {"by": w}, "step": s, "writer": w, "ignored": []} for s, w in writes]
    shuffled = updates[:]
    rnd.shuffle(shuffled)
    first, second = fold(first_result, updates), fold(first_result, shuffled)
    assert first == second
    step, writer = min(writes)
    assert (first["step"], first["writer"]) == (step, writer)
    assert sorted(first["ignored"]) == sorted({w for s, w in writes if (s, w) != (step, writer)})


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 4), st.sampled_from(["a", "b", "c"]), st.integers(0, 2)),
        max_size=12,
        unique=True,
    ),
    st.randoms(use_true_random=False),
)
def test_E21_list_namespaces_are_sorted_by_step_writer_and_index(
    seqs: list[tuple[int, str, int]], rnd: Any
) -> None:
    batches = [[{"_seq": list(s), "v": i}] for i, s in enumerate(seqs)]
    shuffled = batches[:]
    rnd.shuffle(shuffled)
    assert (
        fold(merge_entries, batches, [])
        == fold(merge_entries, shuffled, [])
        == sorted((b[0] for b in batches), key=lambda e: tuple(e["_seq"]))
    )


def test_E21_records_counts_and_usage_do_not_depend_on_order() -> None:
    nodes = [
        {"a": {"visit": 1, "status": "succeeded"}},
        {"b": {"visit": 1}},
        {"a": {"visit": 2, "status": "failed"}},
    ]
    counts = [{"a": 1, RUN_VISITS: 1}, {"b": 1, RUN_VISITS: 1}, {"a": 1, RUN_VISITS: 1}]
    usage = [
        {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4, "modelCalls": 1, "cost": 0.1},
        {"inputTokens": 2, "totalTokens": 2, "modelCalls": 1},
        {"cost": 0.2},
    ]
    for order in itertools.permutations(range(3)):
        assert fold(merge_nodes, [nodes[i] for i in order]) == {"a": nodes[2]["a"], "b": nodes[1]["b"]}
        assert fold(add_counts, [counts[i] for i in order]) == {"a": 2, "b": 1, RUN_VISITS: 3}
        assert fold(add_usage, [usage[i] for i in order]) == {
            "inputTokens": 5,
            "outputTokens": 1,
            "totalTokens": 6,
            "modelCalls": 2,
            "cost": 0.3,
        }


def test_E21_a_same_step_conflict_keeps_the_higher_node_id_and_is_recorded() -> None:
    patches = [
        {"step": 2, "writer": "branch_b", "set": {"k": "B"}, "remove": []},
        {"step": 2, "writer": "branch_a", "set": {"k": "A", "only_a": 1}, "remove": []},
    ]
    for order in (patches, patches[::-1]):
        channel = fold(merge_keyed, order)
        assert channel["values"] == {"k": "B", "only_a": 1}
        assert channel["conflicts"] == [
            {"key": "k", "step": 2, "writers": ["branch_a", "branch_b"], "kept": "branch_b"}
        ]
    later = merge_keyed(channel, {"step": 3, "writer": "branch_a", "set": {"k": "A2"}, "remove": []})
    assert later["values"]["k"] == "A2" and len(later["conflicts"]) == 1


def test_E21_the_turn_loader_channel_merges_with_its_provenance() -> None:
    loaded = {"values": {"topic": "old"}, "provenance": {"topic": [-1, "loader"]}, "conflicts": []}
    channel = merge_keyed(None, loaded)
    assert merge_keyed(channel, {"step": 0, "writer": "start", "set": {"topic": "new"}, "remove": []})[
        "values"
    ] == {"topic": "new"}


# --- E-10: a real parallel superstep ---------------------------------------------------------


def _writer_node(writer: str, set_: dict[str, Any]) -> Any:
    def run(state: RunState, config: RunnableConfig) -> dict[str, Any]:
        step = config["metadata"]["langgraph_step"]
        patch = StatePatch(flow=FlowPatch(step=step, writer=writer, set=set_))
        return to_update(patch, step=step, writer=writer, writable_system_keys=WRITABLE)

    return run


async def test_E10_parallel_branches_writing_flow_never_raise_and_keep_both() -> None:
    graph = StateGraph(RunState)
    graph.add_node("fork", lambda state: Command(goto=["branch_a", "branch_b"]))
    graph.add_node("branch_a", _writer_node("branch_a", {"a": 1, "same": "from a"}))
    graph.add_node("branch_b", _writer_node("branch_b", {"b": 2, "same": "from b"}))
    graph.add_edge(START, "fork")
    final = await graph.compile().ainvoke(
        {"flow": {"values": {"same": "initial"}, "provenance": {}, "conflicts": []}}
    )
    assert keyed_values(final["flow"]) == {"a": 1, "b": 2, "same": "from b"}
    conflict = final["flow"]["conflicts"][0]
    assert (conflict["key"], conflict["writers"], conflict["kept"]) == (
        "same",
        ["branch_a", "branch_b"],
        "branch_b",
    )


# --- E-22 (DR-4): system writes ----------------------------------------------------------------


def test_E22_system_writes_outside_the_writable_keys_fail_before_leaving_the_node() -> None:
    ok = to_update(
        StatePatch(system={"userQuery": "rewritten"}), step=1, writer="n", writable_system_keys=WRITABLE
    )
    assert ok["system"] == {"step": 1, "writer": "n", "set": {"userQuery": "rewritten"}, "remove": []}
    with pytest.raises(ZonicError) as caught:
        to_update(
            StatePatch(system={"userQuery": "x", "tenantId": "t2"}),
            step=1,
            writer="n",
            writable_system_keys=WRITABLE,
        )
    assert caught.value.code == "STATE_READ_ONLY" and caught.value.details["path"] == "system.tenantId"


def test_to_update_stamps_every_namespace() -> None:
    from datetime import UTC, datetime

    record = NodeRecord(node_id="n", node_type="variable", status="succeeded", visit=2, output={"v": 1})
    entry = TimelineEntry(
        node_id="n",
        node_type="variable",
        visit=2,
        status="succeeded",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        duration_ms=3,
    )
    message = ThreadMessage(role="assistant", kind="answer", text_preview="hi")
    update = to_update(
        StatePatch(
            node=record,
            timeline=[entry],
            messages=[message],
            result={"answer": 1},
            usage=Usage(input_tokens=2),
            warnings=[{"code": "W"}],
        ),
        step=4,
        writer="n",
        writable_system_keys=WRITABLE,
        cleared_threads=["notes"],
    )
    assert update["nodes"]["n"]["visit"] == 2 and update["visits"] == {"n": 1, RUN_VISITS: 1}
    assert update["timeline"][0]["_seq"] == [4, "n", 0]
    assert [m.get("clearThread") for m in update["turn_messages"]] == ["notes", None]
    assert update["turn_messages"][1]["_seq"] == [4, "n", 1]
    assert update["result"] == {"value": {"answer": 1}, "step": 4, "writer": "n", "ignored": []}
    assert update["usage"]["inputTokens"] == 2 and update["warnings"] == [{"code": "W", "_seq": [4, "n", 0]}]
    assert without_seq(update["timeline"])[0]["nodeId"] == "n"


# --- B-06, B-07: updates end to end --------------------------------------------------------------


def _apply(document: dict[str, Any], state: dict[str, Any], node_output: dict[str, Any]) -> dict[str, Any]:
    node = WorkflowLoader.from_resources(DEV).from_stored(document).document.node("v")
    assert node is not None
    context = TemplateContext(scope=template_scope(state), node_id="v", node_output=node_output)
    written = WRITER.apply(node.config.get("variableUpdates") or [], context)
    patch = StatePatch(
        flow=flow_patch(written, 1, "v"), system=written.system, messages=update_messages(written, "v", 1)
    )
    update = to_update(
        patch, step=1, writer="v", writable_system_keys=WRITABLE, cleared_threads=written.cleared_threads
    )
    final = dict(state)
    for name, value in update.items():
        reducer = {"flow": merge_keyed, "system": merge_keyed, "turn_messages": merge_entries}.get(
            name, operator.or_
        )
        final[name] = reducer(final.get(name), value)
    return final


def _doc(node: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": [{"id": "s", "type": "start"}, node], "edges": [{"source": "s", "target": "v"}]}


STATE = {
    "flow": merge_keyed(
        None, {"step": -1, "writer": "loader", "set": {"pick": "target", "n": 1}, "remove": []}
    ),
    "system": merge_keyed(
        None, {"step": -1, "writer": "loader", "set": {"userQuery": "q", "tenantId": "t"}, "remove": []}
    ),
}
OUTPUT = {"data": [1, 2], "empty": "", "msg": "hello"}


@pytest.mark.parametrize("data", [OUTPUT, {**OUTPUT, "data": []}], ids=["condition-true", "condition-false"])
def test_B06_platform_node_level_updates_give_the_same_state_as_canonical_ones(data: dict[str, Any]) -> None:
    rules = {
        "logicType": "AND",
        "conditions": [{"field": "{{nodeOutput.data}}", "operator": "is not empty", "value": ""}],
    }
    platform = {
        "id": "v",
        "type": "variable",
        "config": {},
        "variableUpdates": [
            {"fieldName": "flow.{{flow.pick}}", "operation": "set", "value": "{{nodeOutput.msg}}"},
            {"fieldName": "flow.n", "operation": "increment", "value": "2", "rules": rules},
        ],
    }
    when = {"conditionLogic": "AND", "conditions": [{"field": "{{nodeOutput.data}}", "op": "not_empty"}]}
    canonical = {
        "id": "v",
        "type": "variable",
        "config": {
            "variableUpdates": [
                {"path": "flow.{{flow.pick}}", "op": "set", "value": "{{nodeOutput.msg}}"},
                {"path": "flow.n", "op": "increment", "value": "2", "when": when},
            ]
        },
    }
    platform_state, canonical_state = (
        _apply(_doc(platform), STATE, data),
        _apply(_doc(canonical), STATE, data),
    )
    assert keyed_values(platform_state["flow"]) == keyed_values(canonical_state["flow"])
    assert keyed_values(platform_state["flow"]) == {
        "pick": "target",
        "target": "hello",
        "n": 3 if data["data"] else 1,
    }


def test_B07_thread_and_writable_system_targets_and_a_read_only_key() -> None:
    node = {
        "id": "v",
        "type": "variable",
        "config": {
            "variableUpdates": [
                {
                    "path": "thread.agent_messages",
                    "op": "append",
                    "value": "{{nodeOutput.msg}}",
                    "role": "user",
                },
                {"path": "system.userQuery", "op": "set", "value": "rewritten"},
                {"path": "system.attachments", "op": "set", "value": ["f1"]},
            ]
        },
    }
    final = _apply(_doc(node), STATE, OUTPUT)
    assert keyed_values(final["system"]) == {"userQuery": "rewritten", "attachments": ["f1"], "tenantId": "t"}
    message = without_seq(final["turn_messages"])[0]
    assert (message["thread"], message["role"], message["visibility"], message["textPreview"]) == (
        "agent_messages",
        "user",
        "internal",
        "hello",
    )
    scope = template_scope(final)
    assert scope.thread("agent_messages")[0]["textPreview"] == "hello"
    bad = {
        "id": "v",
        "type": "variable",
        "config": {"variableUpdates": [{"path": "system.tenantId", "op": "set", "value": "x"}]},
    }
    with pytest.raises(ZonicError, match="STATE_READ_ONLY"):
        _apply(_doc(bad), STATE, OUTPUT)


def test_thread_clear_hides_earlier_messages_of_that_thread() -> None:
    state = {
        **STATE,
        "history": {
            "notes": [{"role": "user", "textPreview": "old"}],
            "messages": [{"role": "user", "textPreview": "hi"}],
        },
    }
    node = {
        "id": "v",
        "type": "variable",
        "config": {
            "variableUpdates": [
                {"path": "thread.notes", "op": "clear"},
                {"path": "thread.notes", "op": "append", "value": "fresh"},
            ]
        },
    }
    scope = template_scope(_apply(_doc(node), state, OUTPUT))
    assert [m["textPreview"] for m in scope.thread("notes")] == ["fresh"]
    assert [m["textPreview"] for m in scope.thread("messages")] == ["hi"]


# --- RunContext parts -----------------------------------------------------------------------------


async def test_run_permits_bound_concurrency_and_the_emitter_never_raises() -> None:
    permits = RunPermits(2)
    async with permits.node(), permits.node():
        assert permits.available == 0
    assert permits.available == 2
    events: list[dict[str, Any]] = []
    RunEmitter(events.append).emit("state.conflict", key="k")
    assert events == [{"event": "state.conflict", "key": "k"}]

    def broken(_: dict[str, Any]) -> None:
        raise RuntimeError("stream closed")

    emitter = RunEmitter(broken)
    emitter.emit("x")
    assert emitter.dropped == 1


def test_deadline_on_a_fake_clock() -> None:
    clock = FakeClock()
    deadline = Deadline.after(5, clock.monotonic)
    assert deadline.remaining() == 5 and not deadline.expired()
    clock.advance(5)
    with pytest.raises(ZonicError, match="RUN_TIMEOUT"):
        deadline.check()
