"""A scripted node action for compiler tests (plan P1-09), until the real kinds (P2-02).

Each node activates the handles its script returns and records what it saw. The action
reads the run only through ``runtime.context``, as every real kind must (rule 14), so a
compiled graph can be shared by concurrent runs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.context import Deadline, RunContext, RunIdentity, RunPermits
from zonic_runtime.engine.patches import to_update
from zonic_runtime.engine.routing import command
from zonic_runtime.engine.state import RunState
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeRecord, StatePatch

#: ``(spec, visit, state) -> handles``; the default activates the manifest's default handle.
Script = Callable[[NodeSpec, int, Mapping[str, Any]], Sequence[str]]


@dataclass(frozen=True)
class Execution:
    node_id: str
    step: int
    visit: int
    run_id: str
    seen: tuple[str, ...]  # node IDs with a record when this node ran


def default_script(spec: NodeSpec, visit: int, state: Mapping[str, Any]) -> Sequence[str]:
    return [] if spec.terminal else ["default"]


@dataclass
class ScriptedRuns:
    script: Script = default_script
    by_node: Mapping[str, Sequence[str]] = field(default_factory=dict)
    log: list[Execution] = field(default_factory=list)

    def factory(self, spec: NodeSpec, blueprint: Blueprint) -> Callable[..., Any]:
        async def run(state: RunState, config: RunnableConfig, runtime: Runtime[RunContext]) -> Command[Any]:
            step = int(config["metadata"]["langgraph_step"])
            visit = (state.get("visits") or {}).get(spec.node_id, 0) + 1
            run_id = runtime.context.run.run_id if runtime.context is not None else ""
            seen = tuple(sorted(state.get("nodes") or {}))
            self.log.append(Execution(spec.node_id, step, visit, run_id, seen))
            activate = self.by_node.get(spec.node_id) or self.script(spec, visit, state)
            record = NodeRecord(
                node_id=spec.node_id,
                node_type=spec.node_type,
                status="succeeded",
                visit=visit,
                output={"seen": list(seen)},
            )
            update = to_update(
                StatePatch(node=record), step=step, writer=spec.node_id, writable_system_keys=()
            )
            return command(blueprint, spec, activate, update)

        return run

    def executed(self, run_id: str | None = None) -> list[str]:
        return [e.node_id for e in self.log if run_id is None or e.run_id == run_id]


def run_context(blueprint: Blueprint, resources: RuntimeResources, run_id: str = "run-1") -> RunContext:
    identity = RunIdentity(
        run_id=run_id,
        session_id="s-1",
        owner_sub="owner",
        tenant_id=None,
        workflow_id=blueprint.workflow_id,
        revision=blueprint.revision,
    )
    engine = resources.settings.engine
    return RunContext(
        run=identity,
        deadline=Deadline.after(engine.run_timeout_seconds),
        blueprint=blueprint,
        settings=resources.settings,
        permits=RunPermits(engine.max_concurrency_per_run),
    )
