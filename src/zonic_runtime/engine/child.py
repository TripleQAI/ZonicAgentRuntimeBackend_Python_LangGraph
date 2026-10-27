"""``invoke_child``: the one way a kind runs a compiled graph inside its node (DR-6, rule 18).

Sub-flows, agents and swarms run as child graphs inside a node. They must get the parent's
callbacks, tags, metadata and checkpoint namespace (``config``), the run's ``RunContext``
(``context``) and a superstep backstop; anything else would lose tracing, the deadline or
the owner-bound ports. The principle check rejects any other ``ainvoke`` or ``astream`` on a
graph in ``engine/kinds/``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from zonic_runtime.engine.context import RunContext


def child_config(config: RunnableConfig, recursion_limit: int) -> RunnableConfig:
    """The parent's config for a child graph: callbacks, tags and metadata kept; the child's
    own superstep limit. LangGraph adds the child's checkpoint namespace itself."""
    child: RunnableConfig = {**config, "recursion_limit": recursion_limit}
    return child


async def invoke_child(
    graph: Any,
    child_input: Any,
    runtime: Runtime[RunContext],
    config: RunnableConfig,
    *,
    recursion_limit: int | None = None,
) -> Any:
    limit = recursion_limit or runtime.context.settings.engine.child_supersteps
    return await graph.ainvoke(child_input, config=child_config(config, limit), context=runtime.context)
