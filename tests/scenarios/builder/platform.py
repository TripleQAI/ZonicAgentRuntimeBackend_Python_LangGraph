"""The platform workflows' scenarios (plan Section 20.4, cases F-17, F-18, B-12, B-13), after the
Node.js runtime's ``platform-loops.ts`` and ``platform-workflows.test.ts``.

Platform agents run in an order their rules decide, so the model answers by what each call offers
(the tools it is given tell the agents apart). A sub-agent's loop is a routing tool whose handoff
comes back to its agent through tool and variable nodes (F-18).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import ToolMessage

from tests.fakes.scripted_chat_model import Request, Turn
from tests.workflow_fixtures import BUILDER, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.agents.structured import node_schema
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.dialects.refs import RoutingToolRef
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.loader.loader import WorkflowLoader

_LOADED = load_resources(EnvironmentModel(env="dev"))
#: As the scenario runner plans: the script node's feature on (the platform's script nodes).
_DEV = replace(
    _LOADED,
    settings=_LOADED.settings.model_copy(
        update={"features": _LOADED.settings.features.model_copy(update={"script_node": True})}
    ),
)
_TOOL = "tool"  # principle: allow rule-1-node-type platform-tool-nodes-in-loops
Responder = Callable[[Request], Turn]


def plan(fixture: str) -> Blueprint:
    loader = WorkflowLoader.from_resources(_DEV)
    return BlueprintBuilder(_DEV).build(loader.from_stored(read_json(BUILDER / f"{fixture}.json"), "wf"))


@dataclass(frozen=True)
class AgentLoop:
    agent: NodeSpec
    tool: RoutingToolRef
    #: The nodes from the routing target back to the agent (exclusive), by name.
    path: tuple[str, ...]


def _next(blueprint: Blueprint, node_id: str) -> list[NodeSpec]:
    edges = blueprint.edge_index.edges.get(node_id, {})
    targets = [
        t
        for handle, found in edges.items()
        if not handle.startswith("handoff") and handle != "fail"
        for t in found
    ]
    return [blueprint.nodes[t] for t in dict.fromkeys(targets) if t in blueprint.nodes]


def _path_back(blueprint: Blueprint, start: NodeSpec, agent: NodeSpec) -> list[NodeSpec] | None:
    seen: set[str] = set()

    def walk(spec: NodeSpec, trail: list[NodeSpec]) -> list[NodeSpec] | None:
        if spec.node_id == agent.node_id:
            return trail
        if spec.node_id in seen:
            return None
        seen.add(spec.node_id)
        for following in _next(blueprint, spec.node_id):
            found = walk(following, [*trail, spec])
            if found is not None:
                return found
        return None

    return walk(start, [])


def agent_loops(blueprint: Blueprint) -> list[AgentLoop]:
    """For each agent with routing tools, the tools whose handoff comes back to the agent,
    preferring paths through tool nodes."""
    loops = []
    for agent in blueprint.nodes.values():
        for raw in getattr(agent.config, "routing_tools", None) or []:
            tool = RoutingToolRef.model_validate(raw)
            target = blueprint.nodes.get(tool.target)
            path = _path_back(blueprint, target, agent) if target is not None else None
            if path is not None:
                loops.append(AgentLoop(agent, tool, tuple(s.name for s in path)))

    types = {spec.name: spec.node_type for spec in blueprint.nodes.values()}

    def tools_on(loop: AgentLoop) -> int:
        return sum(1 for name in loop.path if types.get(name) == _TOOL)

    return sorted(loops, key=tools_on, reverse=True)


def sample_arguments(tool: RoutingToolRef) -> dict[str, Any]:
    """A value for every property the routing tool's schema declares."""
    properties = (tool.schema_ or {}).get("properties") or {}
    samples = {
        "object": lambda k: {"sample": k},
        "array": lambda k: [k],
        "number": lambda _: 1,
        "integer": lambda _: 1,
    }
    return {
        k: samples.get(str((p or {}).get("type")), lambda key: f"{key}-value")(k)
        for k, p in properties.items()
    }


def structured_names(blueprint: Blueprint) -> set[str]:
    """The structured-output tool names of the workflow's model nodes."""
    found = set()
    for spec in blueprint.nodes.values():
        schema = node_schema(spec.config, spec.name) if hasattr(spec.config, "output_schema") else None
        if schema is not None:
            found.add(str(schema["title"]))
    return found


def sample(schema: Any) -> Any:
    """A value of a JSON Schema: every required property filled, by type."""
    if not isinstance(schema, dict):
        return None
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) and kind else kind
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {k: sample(properties.get(k)) for k in schema.get("required") or [] if k in properties}
    return {"array": [], "boolean": True, "integer": 1, "number": 1, "null": None}.get(str(kind), "sample")


def structured_turn(request: Request, structured: set[str], values: dict[str, Any]) -> Turn | None:
    """The structured answer when the call asks for one: the schema's tool when it is offered,
    JSON text under native structured output (``response_format``); else ``None``. Required
    properties the given values leave out get sample values."""
    offered = [t["function"] for t in request.tools if t["function"]["name"] in structured]
    if offered:
        return Turn.structured(offered[0]["name"], {**sample(offered[0].get("parameters")), **values})
    found = request.options.get("response_format")
    if isinstance(found, dict):
        schema = (found.get("json_schema") or {}).get("schema") if "json_schema" in found else found
        return Turn.say(json.dumps({**(sample(schema) or {}), **values}))
    return None


def _answered(request: Request, name: str) -> bool:
    last = request.messages[-1] if request.messages else None
    return isinstance(last, ToolMessage) and last.name == name


def loop_responder(loop: AgentLoop, structured: set[str]) -> Callable[[], Responder]:
    """Two routing calls through the loop, each acknowledged, then the answer (F-18)."""
    name = loop.tool.call_name
    arguments = sample_arguments(loop.tool)

    def make() -> Responder:
        routed = [0]

        def answer(request: Request) -> Turn:
            if name in request.tool_names:
                if _answered(request, name):
                    return Turn.say(f"Step {routed[0]} recorded.")
                if routed[0] < 2:
                    routed[0] += 1
                    return Turn.call(name, arguments, id=f"route-{routed[0]}")
                return Turn.say("All done: the request is complete.")
            return structured_turn(request, structured, {}) or Turn.say("Noted.")

        return answer

    return make


def plain_responder(
    structured: set[str], text: str = "Noted.", values: dict[str, Any] | None = None
) -> Callable[[], Responder]:
    def make() -> Responder:
        def answer(request: Request) -> Turn:
            return structured_turn(request, structured, dict(values or {})) or Turn.say(text)

        return answer

    return make


#: The orchestrator's user context, from its context agent (structured).
USER_CONTEXT = {
    "userContextFetched": True,
    "userDateFormat": "dd/MM/yyyy",
    "userCultureCode": "en-GB",
    "userTimeZone": "UTC",
    "userCurrencyCode": "GBP",
}


def orchestrator_responder(structured: set[str]) -> Callable[[], Responder]:
    """Turn 1: the context agent, the entity extraction and the policy agent, whose
    ``get_next_questions`` sets ``current_state``; then 'Noted.' everywhere (F-17)."""

    def make() -> Responder:
        def answer(request: Request) -> Turn:
            names = request.tool_names
            # What the tools answered this visit (the system prompt names every state).
            said = json.dumps(
                [m.content for m in request.messages if isinstance(m, ToolMessage)], default=str
            )
            if "getusercontext" in names:
                return structured_turn(request, structured, USER_CONTEXT) or Turn.say("Noted.")
            if "get_next_questions" in names:
                if "quickPaySetup" in said:
                    return structured_turn(request, structured, {}) or Turn.say(
                        "Let us set up the quick payment."
                    )
                return Turn.call("get_next_questions", {"data": {}}, id="next-1")
            entities = {"extractedEntities": [{"entityKey": "item", "entityValue": "laptop"}]}
            return structured_turn(request, structured, entities) or Turn.say("Noted.")

        return answer

    return make
