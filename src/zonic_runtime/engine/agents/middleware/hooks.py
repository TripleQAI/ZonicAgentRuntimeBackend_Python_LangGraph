"""``HookMiddleware``: the model and tool hook points inside an agent loop (design 8.5, 13; case K-12).

* ``beforeModelCall``: ``cancel`` answers in place of the model with the hook's message.
* ``afterModelCall``: ``retry`` calls the model once more.
* ``beforeToolCall``: ``cancel`` returns the hook's message as the tool's (error) result, so the
  model reads why the call did not run.
* ``afterToolCall``: ``patch.result.content`` replaces the tool's result.
* ``approval`` bindings at ``beforeToolCall`` (P4-03): their requests are asked first, through
  ``interrupt()``, before any hook or the tool runs; the ``approval`` hook then cancels a
  rejected call.

The run's hook runner comes from ``runtime.context.hooks``; a run without hooks passes through.
Templates are not resolved here (the agent state is not the run state): a hook reads the call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command, interrupt

from zonic_runtime.engine.approvals import TOOL_KEY, ApprovalAnswer, answer_value, approval_request
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.nodes.base import ConditionGroup

_CANCEL, _RETRY, _ERROR = "cancel", "retry", "error"
_TOOL_CALL = "beforeToolCall"
_CONTENT = "content"


class CallRun:
    """A model, tool or Swarm member call as hooks read it: the call itself, no run templates."""

    def __init__(self, context: Any, approvals: Mapping[str, Any] | None = None) -> None:
        self._context = context
        self._approvals = dict(approvals or {})

    def approval(self, binding_id: str) -> Any:
        return self._approvals.get(binding_id)

    @property
    def run_id(self) -> str:
        return str(self._context.run.run_id)

    @property
    def blueprint(self) -> Blueprint:
        return self._context.blueprint  # type: ignore[no-any-return]

    def resolve(self, value: Any, node_output: Mapping[str, Any] | None = None) -> Any:
        return value

    def holds(self, when: ConditionGroup, node_output: Mapping[str, Any] | None = None) -> bool:
        return True

    def validate(self, schema_id: str, value: Any) -> list[str]:
        return []

    @property
    def path(self) -> str:
        return ""

    def warn(self, code: str, message: str, node_id: str | None = None) -> None:
        return None

    def scope_id(self, scope: str) -> str | None:
        run = self._context.run
        return {
            "run": run.run_id,
            "session": run.session_id,
            "user": run.owner_sub,
            "tenant": run.tenant_id,
        }.get(scope)

    def trace_hook(self, name: str, level: str, metadata: Mapping[str, Any], *, span: bool) -> None:
        return None

    def annotate(
        self, tags: Sequence[str], metadata: Mapping[str, Any], score: Mapping[str, Any] | None
    ) -> None:
        return None


def _runner(context: Any) -> Any:
    hooks = getattr(context, "hooks", None)
    return getattr(hooks, "runner", None)


def _text(messages: list[BaseMessage]) -> str:
    return next((m.text for m in reversed(messages) if isinstance(m, AIMessage)), "")


class HookMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, spec: NodeSpec) -> None:
        super().__init__()
        self.spec = spec

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        context = request.runtime.context
        runner = _runner(context)
        if runner is None:
            return await handler(request)
        run = CallRun(context)
        before = await runner.invoke(
            "beforeModelCall", run, spec=self.spec, call={"messages": len(request.messages)}
        )
        if before.action == _CANCEL:
            return AIMessage(before.message or "")
        response = await handler(request)
        after = await runner.invoke(
            "afterModelCall", run, spec=self.spec, call={"text": _text(response.result)}
        )
        if after.action == _RETRY:
            response = await handler(request)
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        context: Any = request.runtime.context
        runner = _runner(context)
        if runner is None:
            return await handler(request)
        call = {"name": request.tool_call["name"], "args": dict(request.tool_call.get("args") or {})}
        hours = context.settings.interrupts.default_expiry_hours
        approvals = {}
        for item in runner.approvals(context.blueprint, _TOOL_CALL, self.spec.node_id):
            asked = approval_request(
                item.binding.id,
                item.config,
                self.spec.node_id,
                item.config.prompt,
                datetime.now(UTC),
                hours,
                {TOOL_KEY: call["name"], "args": call["args"]},
            )
            approvals[item.binding.id] = answer_value(interrupt(asked.wire(), response_schema=ApprovalAnswer))
        run = CallRun(context, approvals)
        before = await runner.invoke("beforeToolCall", run, spec=self.spec, call=call)
        if before.action == _CANCEL:
            return ToolMessage(
                before.message or "",
                tool_call_id=request.tool_call.get("id") or "",
                name=call["name"],
                status=_ERROR,
            )
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result
        after = await runner.invoke(
            "afterToolCall", run, spec=self.spec, call={**call, "result": result.content}
        )
        replaced = (after.result or {}).get(_CONTENT)
        return result.model_copy(update={_CONTENT: replaced}) if replaced is not None else result
