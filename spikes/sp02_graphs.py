"""SP-2 graphs, shared by the spike and the resume subprocess (``python -m spikes.sp02_resume``).

* ``child_flow``: the root node ``subflow`` invokes a compiled child graph whose node calls
  ``interrupt()`` — the runtime's sub-flow pattern (``invoke_child``).
* ``agent_flow``: the root node ``agent`` invokes a ``create_agent`` graph with
  ``HumanInTheLoopMiddleware`` gating a tool — the runtime's agent node pattern.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt


class Root(TypedDict, total=False):
    log: Annotated[list[str], operator.add]
    answer: Any


class Child(TypedDict, total=False):
    question: str
    answer: Any


def _child() -> CompiledStateGraph[Any, Any, Any, Any]:
    def ask(state: Child) -> Child:
        return {"answer": interrupt({"prompt": state["question"]})}

    g = StateGraph(Child)
    g.add_node("ask", ask)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g.compile()


def child_flow(saver: BaseCheckpointSaver[Any]) -> CompiledStateGraph[Any, Any, Any, Any]:
    child = _child()

    async def subflow(state: Root) -> Root:
        result = await child.ainvoke({"question": "approve the order?"})
        return {"log": ["subflow"], "answer": result["answer"]}

    g = StateGraph(Root)
    g.add_node("pre", lambda s: {"log": ["pre"]})
    g.add_node("subflow", subflow)
    g.add_node("post", lambda s: {"log": ["post"]})
    g.add_edge(START, "pre")
    g.add_edge("pre", "subflow")
    g.add_edge("subflow", "post")
    g.add_edge("post", END)
    return g.compile(checkpointer=saver)


@tool
def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def agent_flow(
    saver: BaseCheckpointSaver[Any], model: BaseChatModel
) -> CompiledStateGraph[Any, Any, Any, Any]:
    agent = create_agent(
        model,
        tools=[send_email],
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"send_email": True})],
    )

    async def run_agent(state: Root) -> Root:
        result = await agent.ainvoke({"messages": [HumanMessage("email bob the report")]})
        return {"log": ["agent"], "answer": result["messages"][-1].text}

    g = StateGraph(Root)
    g.add_node("pre", lambda s: {"log": ["pre"]})
    g.add_node("agent", run_agent)
    g.add_edge(START, "pre")
    g.add_edge("pre", "agent")
    g.add_edge("agent", END)
    return g.compile(checkpointer=saver)
