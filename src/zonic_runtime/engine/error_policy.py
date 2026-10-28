"""``ErrorPolicyHandler``: what a failed node does after its retries (design 8.8; plan P1-10).

LangGraph calls the handler with ``NodeError`` once the node's ``RetryPolicy`` gives up (a
timed-out attempt's writes are already discarded). The handler runs the ``onNodeError``
hook, which may recover with an outcome, then applies the node's policy:

* ``fail_run`` re-raises as a ``ZonicError`` with the node's code: the run ends with it;
* ``continue`` records the error and follows the ``default`` handle;
* ``route`` records the error and follows the ``error`` handle.

Interrupts never reach it (LangGraph's documented rule).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeError
from langgraph.runtime import Runtime
from langgraph.types import Command

from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.errors import RUN_FATAL, error_code
from zonic_runtime.engine.node_template import WorkflowNodeRunner
from zonic_runtime.engine.patches import to_update
from zonic_runtime.engine.routing import command
from zonic_runtime.engine.state import RunState
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeRecord, StatePatch, TimelineEntry


def _details(error: BaseException) -> dict[str, Any]:
    if isinstance(error, ZonicError):
        return dict(error.details)
    return {"message": str(error)[:500], "type": type(error).__name__}


class ErrorPolicyHandler:
    def __init__(self, runner: WorkflowNodeRunner) -> None:
        self.runner = runner
        self.spec = runner.spec
        self.blueprint = runner.blueprint
        self.services = runner.services

    async def __call__(
        self, state: RunState, error: NodeError, config: RunnableConfig, runtime: Runtime[RunContext]
    ) -> Command[Any]:
        failure = error.error
        code = error_code(failure)
        ctx = self.runner.open(state, config, runtime)
        hooks = runtime.context.hooks if runtime.context is not None else None
        fatal = code in RUN_FATAL
        recovered = await hooks.on_node_error(ctx, failure) if hooks is not None and not fatal else None
        if recovered is not None:
            output, warnings = self.runner.build_output(ctx, recovered)
            return self.runner.finish(ctx, recovered, output, warnings)
        policy = self.spec.error_policy
        if policy == "fail_run" or code in RUN_FATAL:
            if isinstance(failure, ZonicError):
                raise failure
            raise ZonicError(code, node_id=self.spec.node_id, **_details(failure)) from failure
        detail = {
            "code": code,
            "nodeId": self.spec.node_id,
            "visit": ctx.visit,
            "policy": policy,
            **_details(failure),
        }
        record = NodeRecord(
            node_id=self.spec.node_id,
            node_type=self.spec.node_type,
            name=self.spec.name,
            status="failed",
            visit=ctx.visit,
            error={"code": code, **_details(failure)},
        )
        handle = (
            self.services.resources.manifest.defaults.default_handle
            if policy == "continue"
            else (self.services.resources.manifest.defaults.error_handle)
        )
        timeline = TimelineEntry(
            node_id=self.spec.node_id,
            node_type=self.spec.node_type,
            visit=ctx.visit,
            status="failed",
            started_at=ctx.started_at,
            duration_ms=0.0,
            handles=[handle],
            error_code=code,
        )
        update = to_update(
            StatePatch(node=record, timeline=[timeline], errors=[detail]),
            step=ctx.step,
            writer=self.spec.node_id,
            writable_system_keys=(),
        )
        ctx.emit("node.failed", code=code, policy=policy, handles=[handle])
        return command(self.blueprint, self.spec, [handle], update)


def error_handler(spec: NodeSpec, blueprint: Blueprint, runner: WorkflowNodeRunner) -> ErrorPolicyHandler:
    """The compiler's ``error_handler_factory``: the handler shares its node's runner."""
    return ErrorPolicyHandler(runner)
