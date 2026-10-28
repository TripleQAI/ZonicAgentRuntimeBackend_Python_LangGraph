"""The core hook types (design 13; plan P2-06): ``setVariables``, ``transform``, ``validate``,
``retry`` and ``fallback``; ``approval`` (P4-03). Ported from the Node.js runtime's
``hooks/types/built-in.ts``; the integration types (``mask``, ``guardrail``, ``cache``,
``rateLimit``, ``webhook``, ``mcpTool``, ``annotate``) are in ``hooks/integration.py`` (P3-10).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel

from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.types import BaseHook, HookContext, NodeHook
from zonic_runtime.io.schema_registry import SchemaRegistry
from zonic_runtime.models.hooks.result import (
    ApprovalHookConfig,
    FallbackHookConfig,
    HookPatch,
    HookResult,
    RetryHookConfig,
    SetVariablesHookConfig,
    TransformHookConfig,
    ValidateHookConfig,
)
from zonic_runtime.runtime.paths import set_path

#: The parts of the data each hook point owns, as ``transform`` paths name them.
_INPUT, _OUTPUT, _RESULT = "input", "output", "result"  # principle: allow rule-1-node-type data-namespaces
INVALID, FAILED = "HOOK_RESULT_INVALID", "HOOK_FAILED"
_TOOL_CALL = "beforeToolCall"
#: The hook run's method that gives an ``approval`` binding's answer (``NodeHookRun``, ``CallRun``).
_ANSWERS = "approval"  # principle: allow rule-1-node-type hook-run-method


class ApprovalHook(BaseHook):
    """``approval`` (beforeNode, beforeToolCall; case K-11). The template (or, for a tool call,
    ``HookMiddleware``) asked the approver before anything ran; the answer decides here. Approved,
    or not asked (its ``when`` did not hold), the node or the call goes on. Rejected, the node is
    skipped with ``rejectedOutput`` and ``rejectedHandles``, or fails (``onReject: fail``); a tool
    call is cancelled, and the model reads ``message``."""

    name: ClassVar[str] = "approval"  # principle: allow rule-1-node-type hook-type-name
    config: ClassVar[type[BaseModel]] = ApprovalHookConfig

    async def run(self, context: HookContext, config: Any) -> HookResult:
        lookup = getattr(context.run, _ANSWERS, None)
        answer = lookup(context.binding.id) if callable(lookup) else None
        if not isinstance(answer, Mapping) or answer.get("approved") is True:
            return HookResult()
        if context.point == _TOOL_CALL:
            return HookResult(action="cancel", message=config.message)
        if config.on_reject == "fail":
            return HookResult(action="fail", code=config.code or FAILED)
        return HookResult(
            action="skip",
            handles=config.rejected_handles or None,
            patch=HookPatch(output=config.rejected_output),
        )


class SetVariablesHook(BaseHook):
    """The builder's state operations; the template applies them with the node's own updates."""

    name: ClassVar[str] = "setVariables"
    config: ClassVar[type[BaseModel]] = SetVariablesHookConfig

    async def run(self, context: HookContext, config: Any) -> HookResult:
        return HookResult(updates=list(config.updates))


class TransformHook(BaseHook):
    """Sets input ports (``beforeNode``), output fields (``afterNode``) or result fields
    (``afterRun``) from templates; the template revalidates the change."""

    name: ClassVar[str] = "transform"
    config: ClassVar[type[BaseModel]] = TransformHookConfig

    async def run(self, context: HookContext, config: Any) -> HookResult:
        targets: dict[str, dict[str, Any] | None] = {
            _INPUT: copy.deepcopy(context.input) if context.input is not None else None,
            _OUTPUT: copy.deepcopy(context.output) if context.output is not None else None,
            _RESULT: copy.deepcopy(context.result) if context.result is not None else None,
        }
        touched: set[str] = set()
        for change in config.set:
            namespace, _, rest = change.path.partition(".")
            target = targets.get(namespace)
            if target is None or not rest:
                raise ZonicError(
                    INVALID, reason=f"transform path {change.path} does not apply at {context.point}"
                )
            set_path(target, rest, context.run.resolve(change.value, context.output))
            touched.add(namespace)
        return HookResult(patch=HookPatch(**{n: targets[n] for n in touched}))


class ValidateHook(BaseHook):
    """Checks the input, the output or the result (or the value its template names) against a
    named schema; ``fail`` with the configured code, or ``warn``."""

    name: ClassVar[str] = "validate"
    config: ClassVar[type[BaseModel]] = ValidateHookConfig

    def __init__(self, schemas: SchemaRegistry) -> None:
        self._schemas = schemas

    def check(self, config: Any) -> str | None:
        return None if self._schemas.has(config.model) else f"unknown schema {config.model}"

    async def run(self, context: HookContext, config: Any) -> HookResult:
        if config.value is not None:
            value = context.run.resolve(config.value, context.output)
        else:
            value = next(v for v in (context.input, context.output, context.result, {}) if v is not None)
        problems = context.run.validate(config.model, value)
        if not problems:
            return HookResult()
        if config.on_invalid == "warn":
            return HookResult(message="; ".join(problems))
        return HookResult(action="fail", code=config.code, message="; ".join(problems))


class RetryHook(BaseHook):
    """``onNodeError``: the listed codes, up to ``attempts`` times, waiting longer each time."""

    name: ClassVar[str] = "retry"
    config: ClassVar[type[BaseModel]] = RetryHookConfig

    async def run(self, context: HookContext, config: Any) -> HookResult:
        code = (context.error or {}).get("code")
        if config.codes and code not in config.codes:
            return HookResult()
        if context.attempt >= config.attempts:
            return HookResult()
        return HookResult(action="retry", delay_ms=config.backoff_milliseconds * (context.attempt + 1))


class FallbackHook(BaseHook):
    """``onNodeError``: the node completes with the configured output and handles."""

    name: ClassVar[str] = "fallback"
    config: ClassVar[type[BaseModel]] = FallbackHookConfig

    async def run(self, context: HookContext, config: Any) -> HookResult:
        code = (context.error or {}).get("code")
        if config.codes and code not in config.codes:
            return HookResult()
        output = context.run.resolve(config.output)
        return HookResult(
            action="recover",
            patch=HookPatch(output=dict(output) if isinstance(output, Mapping) else {}),
            handles=list(config.activate) or None,
        )


def builtin_hooks(schemas: SchemaRegistry) -> list[NodeHook]:
    return [
        SetVariablesHook(),
        TransformHook(),
        ValidateHook(schemas),
        RetryHook(),
        FallbackHook(),
        ApprovalHook(),
    ]
