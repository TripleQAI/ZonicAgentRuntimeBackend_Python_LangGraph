"""``ZonicAgentState``: the state of an agent graph (design 8.5).

LangChain's ``AgentState`` plus what the Zonic tools and middleware share during one visit:
the handoff decision, the routing tools' preset arguments (resolved per visit by the kind),
whether the iteration limit ended the loop, and a Swarm member's step. Reducers keep the last
write, so two tool calls in one model turn never raise ``InvalidUpdateError``.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentState


def _last(_: Any, new: Any) -> Any:
    return new


def _merge(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    return {**(old or {}), **(new or {})}


class ZonicAgentState(AgentState[Any]):
    #: ``{target, name, via, input}`` of the chosen handoff; the last valid call wins.
    zonic_handoff: NotRequired[Annotated[dict[str, Any] | None, _last]]
    #: Routing tool name -> preset arguments, merged over what the model supplies (case B-04).
    zonic_presets: NotRequired[Annotated[dict[str, dict[str, Any]], _last]]
    #: Routing tool name -> the arguments it was called with: ``toolResults[<name>]``.
    zonic_routed: NotRequired[Annotated[dict[str, Any], _last]]
    #: The iteration limit ended the loop (``AGENT_TURN_LIMIT``).
    zonic_limited: NotRequired[Annotated[bool, _last]]
    #: The parent run's ``flow`` and ``system`` values for this visit, and the visit number: what
    #: the tools send as the MCP ``execute`` context (P3-02). Tools' flow updates merge into it.
    zonic_flow: NotRequired[Annotated[dict[str, Any], _merge]]
    zonic_system: NotRequired[Annotated[dict[str, Any], _last]]
    zonic_visit: NotRequired[Annotated[int, _last]]
    #: ``flow`` changes the tools made (MCP flow updates); the kind writes them to the run.
    zonic_flow_updates: NotRequired[Annotated[dict[str, Any], _merge]]
    #: A Swarm member's step (P4-01): ``member``, ``step``, ``history`` (the members that finished
    #: their steps) and ``inputs`` (how many messages the step started with); the handoff tools
    #: check their limits and build the handoff from it.
    zonic_swarm: NotRequired[Annotated[dict[str, Any], _last]]
