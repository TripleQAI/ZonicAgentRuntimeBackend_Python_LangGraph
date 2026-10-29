"""The ``multi_agent`` kind with pattern ``swarm`` (node type ``swarm``; design 8.6; plan P4-01).

On the node's first visit the kind builds each member's agent graph with ``AgentFactory`` (an LLM
member is an agent with no tools of its own; every member gets a transfer tool for each other
member) and compiles the Swarm subgraph once (``engine/agents/swarm/builder.py``). Each visit:

* resolves each member's own input (system prompt, configured messages) against the run state;
* registers the members' blueprint as this node's child, so member events and observations find
  their place (a member's text and tool events are this node's, with ``member``);
* runs the subgraph through ``invoke_child`` under the Swarm's own deadline
  (``executionTimeoutSeconds``, ``SWARM_TIMEOUT``): the kind owns it, so the node has no run
  timeout of its own; a member past ``nodeTimeoutSeconds`` fails the Swarm with ``NODE_TIMEOUT``;
* returns ``{status, text, finalAgent, nodeHistory, executionTimeMs}`` with the members' usage,
  messages and flow changes.

Repetitive handoff detection needs both settings (rule SW-11); with one it is off and the node
warns ``SWARM_DETECTION_OFF``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, ClassVar

from langgraph.errors import GraphRecursionError, NodeTimeoutError

from zonic_runtime.engine.agents.messages import split_system, text_messages
from zonic_runtime.engine.agents.swarm.builder import MEMBER_FINISHED, SwarmMember, SwarmSpec, build_swarm
from zonic_runtime.engine.agents.swarm.handoff_tools import (
    STEP_LIMIT,
    SwarmLimits,
    SwarmMemberRef,
    transfer_tool,
)
from zonic_runtime.engine.agents.swarm.member_context import brief
from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.kinds.model_kinds import ModelKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.engine.state import keyed_values
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, EdgeIndex, NodeSpec
from zonic_runtime.models.engine import NodeOutcome, StateUpdate, ThreadMessage, Usage
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.models.nodes.configs import AgentNodeConfig, MultiAgentNodeConfig

#: The manifest's ``options.pattern`` this kind runs.
SWARM_PATTERN = "swarm"  # principle: allow rule-1-node-type multi-agent-pattern-option
_TASK, _MESSAGES = "task", "messages"
_COMPLETED, _FAILED = "completed", "failed"
DETECTION_OFF, TIMEOUT, NODE_TIMEOUT = "SWARM_DETECTION_OFF", "SWARM_TIMEOUT", "NODE_TIMEOUT"


class MultiAgentKind(BaseKind):
    owns_deadline: ClassVar[bool] = True

    def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
        self.spec, self.blueprint = spec, blueprint
        config = spec.config
        assert isinstance(config, MultiAgentNodeConfig)
        self.config = config
        self.member_specs = [blueprint.nodes[m] for m in config.members if m in blueprint.nodes]
        named = [s for s in self.member_specs if config.entry_point in (s.node_id, s.name)]
        self.entry = named[0] if named else (self.member_specs[0] if self.member_specs else None)
        #: The members as a child blueprint of this node: the tracer and the event mapper read it.
        self.members = Blueprint(
            workflow_id=blueprint.workflow_id,
            revision=blueprint.revision,
            document_version=blueprint.document_version,
            graph_id=f"{blueprint.graph_id}.{spec.graph_name}",
            entry=self.entry.node_id if self.entry is not None else spec.node_id,
            nodes={s.node_id: s for s in self.member_specs},
            edge_index=EdgeIndex(),
            limits=blueprint.limits,
        )
        # Process-wide, built on the first visit: they hold no run data (rule 14).
        self._graph: Any = None
        self._limits: SwarmLimits | None = None
        self._detection_off = False

    def refs(self) -> list[SwarmMemberRef]:
        return [
            SwarmMemberRef(
                node_id=s.node_id,
                name=s.name,
                graph_name=s.graph_name,
                description=s.description,
            )
            for s in self.member_specs
        ]

    async def graph(self, ctx: NodeContext) -> Any:
        if self._graph is not None:
            return self._graph
        settings = ctx.services.settings
        limits, off = SwarmLimits.of(self.config, settings.swarm, len(self.member_specs))
        refs = self.refs()
        ack = settings.swarm.handoff_acknowledgement
        members = []
        for spec, ref in zip(self.member_specs, refs, strict=True):
            kind = ModelKind(spec, self.blueprint)
            agents = kind.agents(ctx)
            is_agent = isinstance(spec.config, AgentNodeConfig)
            own = list(await agents.tools.tools(spec)) if is_agent else []  # an LLM member has none (W-13)
            transfers = [transfer_tool(r, limits, self.spec.node_id, ack) for r in refs if r is not ref]
            turns = (
                getattr(spec.config, "max_iterations", None) if is_agent else settings.models.llm_max_turns
            )
            agent = agents.factory.build(
                spec=spec,
                model=kind.model(ctx),
                tools=[*own, *transfers],
                max_iterations=turns,
                governor=kind.governor(ctx),
            )
            seconds = (
                self.config.node_timeout_seconds
                or spec.timeout_seconds
                or settings.engine.node_timeout_seconds
            )
            members.append(SwarmMember(ref=ref, spec=spec, agent=agent, timeout_seconds=seconds))
        swarm = SwarmSpec(node=self.spec, members=tuple(refs), settings=settings.swarm, steps=limits.steps)
        entry = next(m for m in members if self.entry is not None and m.spec.node_id == self.entry.node_id)
        self._limits, self._detection_off = limits, off
        self._graph = build_swarm(self.members.graph_id, swarm, members, entry)
        return self._graph

    def own_input(self, ctx: NodeContext, spec: NodeSpec) -> dict[str, Any]:
        """A member's own part of its input, bound as its node would bind it at this visit."""
        member = replace(ctx, spec=spec)
        config = spec.config.model_dump(by_alias=True, exclude_none=True)
        bound = ctx.services.binder.bind(
            spec.node_id, spec.io, config, ctx.visit, member.resolve, member.read
        )
        system, rest = split_system(text_messages(bound.get(_MESSAGES)))
        raw = getattr(spec.config, "system_prompt", None)  # its prompt only, not its update templates
        prompt = member.resolve(raw) if isinstance(raw, str) else None
        return brief([prompt, *system] if isinstance(prompt, str) else system, rest)

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        node_id = self.spec.node_id
        entry = ctx.services.resources.manifest.node_types.get(self.spec.node_type)
        if entry is None or entry.options.get("pattern") != SWARM_PATTERN:
            raise ZonicError("NODE_TYPE_UNSUPPORTED", node_id=node_id, kind=self.spec.kind)
        if self.entry is None:
            raise ZonicError("DOCUMENT_INVALID", node_id=node_id, reason="the swarm has no members")
        graph = await self.graph(ctx)
        assert self._limits is not None
        ctx.run.children.add(self.blueprint, node_id, self.members)
        task = node_input.get(_TASK)
        swarm_input = {
            "task": task if isinstance(task, str) else "" if task is None else str(task),
            "step": 1,
            "active_member": self.entry.name,
            "briefs": {s.node_id: self.own_input(ctx, s) for s in self.member_specs},
            "zonic_flow": keyed_values(ctx.state.get("flow")),
            "zonic_system": keyed_values(ctx.state.get("system")),
            "zonic_visit": ctx.visit,
        }
        clock = ctx.services.clock
        started = clock.monotonic()
        seconds = self.spec.timeout_seconds or ctx.services.settings.engine.node_timeout_seconds
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                final = await invoke_child(
                    graph, swarm_input, ctx.runtime, ctx.config, recursion_limit=self._limits.steps + 1
                )
        except TimeoutError:
            if deadline.expired():
                raise ZonicError(TIMEOUT, node_id=node_id, seconds=seconds) from None
            raise
        except NodeTimeoutError as error:  # a member past nodeTimeoutSeconds (W-05)
            member = self.members.by_graph_name(error.node)
            name = member.name if member else error.node
            ctx.emit(MEMBER_FINISHED, member=name, status=_FAILED)
            raise ZonicError(NODE_TIMEOUT, node_id=node_id, member=name) from error
        except GraphRecursionError as error:  # the backstop of the step limit
            raise ZonicError(STEP_LIMIT, node_id=node_id, limit=self._limits.steps) from error

        history = list(final.get("history") or [])
        output = {
            "status": _COMPLETED,
            "text": final.get("answer") or "",
            "finalAgent": final.get("final_member") or (history[-1] if history else None),
            "nodeHistory": history,
            "executionTimeMs": int(max(0.0, clock.monotonic() - started) * 1000),
        }
        warnings = []
        if self._detection_off:
            details = ctx.services.resources.errors.errors.get(DETECTION_OFF)
            warnings.append(
                {"code": DETECTION_OFF, "nodeId": node_id, "message": details.message if details else ""}
            )
        usage = final.get("usage")
        return NodeOutcome(
            output={k: v for k, v in output.items() if v is not None},
            activate=[ctx.services.resources.manifest.defaults.default_handle],
            usage=Usage.model_validate(usage) if usage else None,
            messages=[ThreadMessage.model_validate(m) for m in final.get("turn_messages") or []],
            warnings=warnings,
            updates=[
                StateUpdate(path=f"flow.{key}", op="set", value=value)
                for key, value in (final.get("flow_updates") or {}).items()
            ],
        )
