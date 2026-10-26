"""SP-3: ``astream_events(version="v3")`` from graphs invoked inside nodes.

Questions (design 20.1, Section 10, DR-11):
1. Token, tool and custom events of an agent graph and a child graph invoked inside root
   nodes reach the root stream, each with its namespace.
2. The namespace rule: ``["<root node>:<task id>", "<child node>:<task id>", ...]``.
3. ``context``, ``durability`` and ``control`` are accepted; the child graph sees the
   root run's context.
4. An interrupt inside a child graph surfaces as ``run.interrupted`` / ``run.interrupts``.
"""

from __future__ import annotations

import operator
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolCallTransformer
from langgraph.runtime import RunControl, get_runtime
from langgraph.stream import CustomTransformer
from langgraph.types import interrupt
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn

pytestmark = pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning")


@dataclass
class Ctx:
    tenant: str


class R(TypedDict, total=False):
    log: Annotated[list[str], operator.add]


class C(TypedDict, total=False):
    depth: int
    tenant: str


@tool
def lookup(city: str) -> str:
    """Look up the weather."""
    get_stream_writer()({"tool_progress": city})
    return f"sunny in {city}"


def _grandchild() -> Any:
    def leaf(state: C) -> C:
        get_stream_writer()({"leaf": True})
        return {"depth": 2, "tenant": get_runtime(Ctx).context.tenant}

    g = StateGraph(C)
    g.add_node("leaf", leaf)
    g.add_edge(START, "leaf")
    g.add_edge("leaf", END)
    return g.compile()


def _child(ask: bool) -> Any:
    grandchild = _grandchild()

    async def nested(state: C) -> C:
        get_stream_writer()({"child": True})
        out = await grandchild.ainvoke({"depth": 1})
        if ask:
            interrupt({"prompt": "continue?"})
        return {"depth": out["depth"], "tenant": out["tenant"]}

    g = StateGraph(C)
    g.add_node("nested", nested)
    g.add_edge(START, "nested")
    g.add_edge("nested", END)
    return g.compile()


def _root(*, ask: bool = False) -> Any:
    model = ScriptedChatModel(
        script=[Turn.call("lookup", {"city": "Oslo"}), Turn.say("It is sunny in Oslo.")]
    )
    agent = create_agent(model, tools=[lookup])
    child = _child(ask)

    async def agent_1(state: R) -> R:
        await agent.ainvoke({"messages": [HumanMessage("weather?")]})
        return {"log": ["agent"]}

    async def sub_1(state: R) -> R:
        out = await child.ainvoke({"depth": 0})
        return {"log": [f"child depth={out['depth']} tenant={out['tenant']}"]}

    g = StateGraph(R, context_schema=Ctx)
    g.add_node("agent_1", agent_1)
    g.add_node("sub_1", sub_1)
    g.add_edge(START, "agent_1")
    g.add_edge("agent_1", "sub_1")
    g.add_edge("sub_1", END)
    return g.compile(checkpointer=InMemorySaver())


async def _collect(graph: Any, thread: str) -> tuple[list[dict[str, Any]], Any]:
    run = await graph.astream_events(
        {"log": []},
        {"configurable": {"thread_id": thread}},
        version="v3",
        transformers=[CustomTransformer, ToolCallTransformer],
        context=Ctx(tenant="t1"),
        durability="exit",
        control=RunControl(),
    )
    events = [event async for event in run]
    return events, run


def _names(namespace: list[str]) -> list[str]:
    return [segment.split(":", 1)[0] for segment in namespace]


async def test_tokens_tools_and_custom_events_with_namespaces() -> None:
    events, run = await _collect(_root(), "sp3")
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_method[event["method"]].append(event)
    print("\nSP-3.1 event counts:", {k: len(v) for k, v in by_method.items()})

    tokens = [
        e["params"]["data"][0]["delta"]["text"]
        for e in by_method["messages"]
        if e["params"]["data"][0].get("event") == "content-block-delta"
        and e["params"]["data"][0]["delta"].get("type") == "text-delta"
    ]
    assert "".join(tokens) == "It is sunny in Oslo."
    token_ns = {tuple(_names(e["params"]["namespace"])) for e in by_method["messages"]}
    assert token_ns == {("agent_1",)}

    tools = [(e["params"]["data"]["event"], _names(e["params"]["namespace"])) for e in by_method["tools"]]
    print("SP-3.1 tools:", tools)
    assert tools == [("tool-started", ["agent_1"]), ("tool-finished", ["agent_1"])]

    custom = [(e["params"]["data"], _names(e["params"]["namespace"])) for e in by_method["custom"]]
    print("SP-3.2 custom with namespaces:", custom)
    assert custom == [
        ({"tool_progress": "Oslo"}, ["agent_1"]),
        ({"child": True}, ["sub_1"]),
        ({"leaf": True}, ["sub_1", "nested"]),
    ]

    lifecycle = [
        (e["params"]["data"]["event"], _names(e["params"]["data"]["namespace"]))
        for e in by_method["lifecycle"]
    ]
    print("SP-3.2 lifecycle:", lifecycle)
    assert ("started", ["sub_1", "nested"]) in lifecycle

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    output = await run.output()
    print("SP-3.3 output:", output)
    assert output["log"] == ["agent", "child depth=2 tenant=t1"]


async def test_message_metadata_names_the_node() -> None:
    events, _ = await _collect(_root(), "sp3-meta")
    meta = next(e["params"]["data"][1] for e in events if e["method"] == "messages")
    print("\nSP-3.4 message metadata keys:", sorted(meta))
    assert meta["langgraph_node"] == "model"


async def test_interrupt_inside_a_child_graph_surfaces_on_the_run() -> None:
    events, run = await _collect(_root(ask=True), "sp3-int")
    interrupted = await run.interrupted()
    interrupts = await run.interrupts()
    print("\nSP-3.5 interrupted:", interrupted, interrupts)
    assert interrupted
    assert interrupts and interrupts[0].value == {"prompt": "continue?"}
