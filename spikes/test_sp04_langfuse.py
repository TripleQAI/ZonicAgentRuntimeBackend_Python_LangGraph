"""SP-4: Langfuse v5 ``CallbackHandler`` under ``propagate_attributes`` on an isolated provider.

Questions (design 20.1, DR-5):
1. Every span of a run (graph, nodes, agent graph invoked in a node, model, tool) is one
   trace under the runtime's root observation, with user, session and trace name set.
2. Which observation types the handler assigns, and whether they are reliable.
3. Node metadata from ``add_node(metadata=...)`` reaches the node span and its children,
   so the exporter can attribute every observation to a builder node.
4. A node returning ``Command`` is recorded with ``update`` and ``goto`` (enrichment by
   unwrapping ``Command``).
5. ``should_export_span`` filters the agent's internal wrapper spans; ``mask_otel_spans``
   patches the observation type at export.
6. Nothing reaches the global tracer provider or the network.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn

TYPE = "langfuse.observation.type"
META = "langfuse.observation.metadata."


@tool
def lookup(city: str) -> str:
    """Look up the weather."""
    return f"sunny in {city}"


class R(TypedDict, total=False):
    log: Annotated[list[str], operator.add]


def _graph() -> Any:
    model = ScriptedChatModel(script=[Turn.call("lookup", {"city": "Oslo"}), Turn.say("It is sunny.")])
    agent = create_agent(model, tools=[lookup])

    async def n_agent(state: R) -> Command[str]:
        await agent.ainvoke({"messages": [HumanMessage("weather?")]}, {"run_name": "agent:agent-1"})
        return Command(update={"log": ["agent"]}, goto="n_end")

    g = StateGraph(R)
    g.add_node(
        "n_agent",
        n_agent,
        destinations=("n_end",),
        metadata={"zonic_node_id": "agent-1", "zonic_node_type": "agent", "zonic_kind": "agent"},
    )
    g.add_node(
        "n_end",
        lambda s: {"log": ["end"]},
        metadata={"zonic_node_id": "end-1", "zonic_node_type": "output", "zonic_kind": "output"},
    )
    g.add_edge(START, "n_agent")
    g.add_edge("n_end", END)
    return g.compile()


def _meta(span: ReadableSpan, key: str) -> Any:
    raw = (span.attributes or {}).get(META + key)
    return json.loads(raw) if isinstance(raw, str) else raw


def _is_agent_wrapper(span: ReadableSpan) -> bool:
    """The agent graph's internal ``model``/``tools`` chain spans duplicate their children."""
    attrs = span.attributes or {}
    return (
        attrs.get(TYPE) == "chain"
        and _meta(span, "langgraph_node") in {"model", "tools"}
        and span.name
        in {
            "model",
            "tools",
        }
    )


def _retype(*, params: Any) -> MaskOtelSpansResult:
    patches = {}
    for identifier, span in params.spans.items():
        kind = (span.attributes or {}).get(META + "zonic_kind")
        if kind and (span.attributes or {}).get(TYPE) in {"chain", "agent"}:
            mapped = {"agent": "agent", "output": "span"}.get(json.loads(kind), "span")
            patches[identifier] = OtelSpanPatch(set_attributes={TYPE: mapped})
    return MaskOtelSpansResult(span_patches=patches)


async def _run(public_key: str, **client: Any) -> list[ReadableSpan]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    lf = Langfuse(
        public_key=public_key,
        secret_key="sk-lf-spike",
        base_url="http://127.0.0.1:9",
        tracer_provider=provider,
        span_exporter=exporter,
        **client,
    )
    handler = CallbackHandler(public_key=public_key)
    with (
        lf.start_as_current_observation(as_type="span", name="run r1"),
        propagate_attributes(
            user_id="u1", session_id="s1", trace_name="weather-wf", metadata={"run_id": "r1"}, tags=["api"]
        ),
    ):
        await _graph().ainvoke({"log": []}, {"callbacks": [handler]})
    lf.flush()
    return list(exporter.get_finished_spans())


async def test_one_trace_with_propagated_attributes_and_types() -> None:
    spans = await _run("pk-lf-sp4a")
    print("\nSP-4.1 spans:")
    for s in spans:
        print(f"  {s.name:<18} type={(s.attributes or {}).get(TYPE):<10} node={_meta(s, 'zonic_node_id')}")
    assert len({s.context.trace_id for s in spans}) == 1
    roots = [s for s in spans if s.parent is None]
    assert [r.name for r in roots] == ["run r1"]
    attrs = [s.attributes or {} for s in spans]
    assert all(a.get("user.id") == "u1" and a.get("session.id") == "s1" for a in attrs)
    types = {s.name: (s.attributes or {}).get(TYPE) for s in spans}
    assert types["ScriptedChatModel"] == "generation" and types["lookup"] == "tool"
    assert types["n_agent"] == "agent"  # the handler's name heuristic, not the node's kind
    assert types["n_end"] == "chain"


async def test_node_metadata_reaches_node_and_child_spans() -> None:
    spans = await _run("pk-lf-sp4b")
    by_node: dict[str, list[str]] = {}
    for s in spans:
        node = _meta(s, "zonic_node_id")
        if node:
            by_node.setdefault(node, []).append(s.name)
    print("\nSP-4.2 spans per builder node:", by_node)
    assert {"n_agent", "ScriptedChatModel", "lookup"} <= set(by_node["agent-1"])
    assert by_node["end-1"] == ["n_end"]


async def test_command_output_is_recorded_with_update_and_goto() -> None:
    spans = await _run("pk-lf-sp4c")
    node = next(s for s in spans if s.name == "n_agent")
    output = json.loads((node.attributes or {})["langfuse.observation.output"])
    print("\nSP-4.3 Command output:", output)
    assert output["update"] == {"log": ["agent"]} and output["goto"] == "n_end"


async def test_export_filter_and_type_patch() -> None:
    spans = await _run(
        "pk-lf-sp4d", should_export_span=lambda s: not _is_agent_wrapper(s), mask_otel_spans=_retype
    )
    names = [s.name for s in spans]
    types = {s.name: (s.attributes or {}).get(TYPE) for s in spans}
    print("\nSP-4.4 exported after filter:", names)
    print("SP-4.4 types after patch:", types)
    assert "model" not in names and "tools" not in names
    assert {"ScriptedChatModel", "lookup", "n_agent", "n_end"} <= set(names)
    assert types["n_end"] == "span" and types["n_agent"] == "agent"


async def test_global_tracer_provider_untouched() -> None:
    await _run("pk-lf-sp4e")
    provider = otel_trace.get_tracer_provider()
    print("\nSP-4.5 global provider:", type(provider).__name__)
    assert not isinstance(provider, TracerProvider)
