"""``HookRunner``: the resolved bindings of one hook point, executed (design 13; plan P2-06).

Ported from the Node.js runtime's ``hooks/runner.ts``. For each binding in order: ``when`` (false
means not run at all, case K-02); an asynchronous hook is dispatched on copies and never awaited
(dropped with a warning past ``hooks.asyncQueueSize``); a blocking hook runs under its timeout
with its failure policy (case K-07). Its result must allow its action at the point, and routing
needs ``canRoute`` (``afterNode``) and handles the node has (case K-05). Each hook sees the
changes of the hooks before it.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.resolver import HookResolver, ResolvedBinding
from zonic_runtime.hooks.types import HookContext, HookRun
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import StateUpdate
from zonic_runtime.models.hooks.binding import HookPoint
from zonic_runtime.models.hooks.result import HookResult
from zonic_runtime.models.resources.settings import HookSettings
from zonic_runtime.runtime.redact import StreamMaskRules

INVALID, FAILED, TIMEOUT = "HOOK_RESULT_INVALID", "HOOK_FAILED", "HOOK_TIMEOUT"
IGNORED, NOT_OVERRIDABLE, WARNING = "HOOK_IGNORED", "HOOK_NOT_OVERRIDABLE", "HOOK_WARNING"
_CONTINUE = "continue"
_MASK = "mask"
#: The hook type whose request is asked before the node or the tool call runs (P4-03).
APPROVAL = "approval"  # principle: allow rule-1-node-type hook-type-name
_ALLOWED: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "beforeRun": frozenset({"continue", "fail"}),
        "beforeNode": frozenset({"continue", "skip", "fail"}),
        "afterNode": frozenset({"continue", "fail"}),
        "onNodeError": frozenset({"continue", "retry", "recover", "fail"}),
        "afterRun": frozenset({"continue"}),
        "onRunError": frozenset({"continue"}),
        "beforeModelCall": frozenset({"continue", "cancel"}),
        "afterModelCall": frozenset({"continue", "retry"}),
        "beforeToolCall": frozenset({"continue", "cancel"}),
        "afterToolCall": frozenset({"continue"}),
        "beforeMemberCall": frozenset({"continue", "cancel"}),
        "onHandoff": frozenset({"continue"}),
        "onInterrupt": frozenset({"continue"}),
        "onResume": frozenset({"continue", "fail"}),
    }
)
#: A decision that ends the point: the hooks after it do not run.
_FINAL = frozenset({"skip", "fail", "retry", "recover", "cancel"})


@dataclass
class HookOutcome:
    """What the hooks of one point decided; absent fields leave the node, call or run unchanged."""

    action: str = _CONTINUE
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    parts: list[dict[str, Any]] | None = None
    handles: list[str] | None = None
    code: str | None = None
    #: ``fail`` ends the run whatever the node's error policy.
    fail_run: bool = False
    delay_ms: int = 0
    message: str | None = None
    updates: list[StateUpdate] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _Failure:
    code: str
    fail_run: bool


class HookRunner:
    def __init__(self, resolver: HookResolver, settings: HookSettings) -> None:
        self.resolver = resolver
        self._settings = settings
        self._background: set[asyncio.Task[Any]] = set()

    async def invoke(
        self,
        point: HookPoint,
        run: HookRun,
        *,
        spec: NodeSpec | None = None,
        visit: int | None = None,
        input: Mapping[str, Any] | None = None,
        output: Mapping[str, Any] | None = None,
        handles: list[str] | None = None,
        error: dict[str, str] | None = None,
        attempt: int = 0,
        result: Mapping[str, Any] | None = None,
        call: Mapping[str, Any] | None = None,
        parts: list[dict[str, Any]] | None = None,
    ) -> HookOutcome:
        resolved = self.resolver.of(run.blueprint)
        bindings = resolved.node(point, spec.node_id) if spec is not None else resolved.run(point)
        outcome = HookOutcome()
        if spec is not None and point == "beforeNode":
            for binding_id in resolved.not_overridable.get(spec.node_id, []):
                outcome.warnings.append(_warning(NOT_OVERRIDABLE, binding_id, spec))
        if not bindings:
            return outcome
        context = HookContext(
            point=point,
            binding=bindings[0].binding,
            run=run,
            spec=spec,
            visit=visit,
            input=copy.deepcopy(dict(input)) if input is not None else None,
            output=copy.deepcopy(dict(output)) if output is not None else None,
            handles=list(handles) if handles is not None else None,
            error=error,
            attempt=attempt,
            result=copy.deepcopy(dict(result)) if result is not None else None,
            call=dict(call or {}),
            parts=copy.deepcopy(list(parts)) if parts is not None else None,
        )
        for item in bindings:
            context = replace(context, binding=item.binding)
            when = item.binding.when
            if when is not None and not run.holds(when, context.output):
                continue
            if self._mode(item) == "async":
                self._dispatch(item, context, outcome)
                run.trace_hook(
                    f"hook:{item.binding.id}", "DEFAULT", {"point": point, "mode": "async"}, span=False
                )
                continue
            started = time.monotonic()
            executed = await self._execute(point, item, context)
            run.trace_hook(
                f"hook:{item.binding.id}",
                "WARNING" if isinstance(executed, _Failure) else "DEFAULT",
                {
                    "point": point,
                    "hook": item.binding.hook,
                    "action": executed.code if isinstance(executed, _Failure) else executed.action,
                    "durationMs": round((time.monotonic() - started) * 1000, 3),
                },
                span=True,
            )
            if isinstance(executed, _Failure):
                if item.binding.failure_policy == "ignore" and executed.code != INVALID:
                    outcome.warnings.append(_warning(IGNORED, item.binding.id, spec, cause=executed.code))
                    continue
                outcome.action, outcome.code, outcome.fail_run = "fail", executed.code, executed.fail_run
                return outcome
            if executed.handles is not None and not self._routable(point, item, executed, spec):
                outcome.action, outcome.code = "fail", INVALID
                return outcome
            outcome.updates += executed.updates
            if executed.message and executed.action == _CONTINUE:
                outcome.warnings.append(_warning(WARNING, item.binding.id, spec, message=executed.message))
            if executed.action in _FINAL:
                outcome.action = executed.action
                outcome.code = executed.code or (FAILED if executed.action == "fail" else None)
                # At onNodeError a hook's ``fail`` escalates: the run fails.
                outcome.fail_run = executed.action == "fail" and point == "onNodeError"
                outcome.delay_ms, outcome.message = executed.delay_ms, executed.message
                outcome.output = executed.patch.output
                outcome.handles = executed.handles
                return outcome
            patch = executed.patch
            if patch.input is not None:
                context.input = {**(context.input or {}), **patch.input}
                outcome.input = context.input
            if patch.output is not None:
                context.output = patch.output
                outcome.output = patch.output
            if patch.result is not None:
                context.result = patch.result
                outcome.result = patch.result
            if patch.parts is not None:
                context.parts = patch.parts
                outcome.parts = patch.parts
            if executed.handles is not None:
                context.handles = executed.handles
                outcome.handles = executed.handles
        return outcome

    async def _execute(
        self, point: HookPoint, item: ResolvedBinding, context: HookContext
    ) -> HookResult | _Failure:
        binding = item.binding
        seconds = (binding.timeout_ms or self._settings.default_timeout_milliseconds) / 1000
        fail_run = binding.failure_policy == "fail_run"
        try:
            async with asyncio.timeout(seconds):
                result = await item.hook.run(context, item.config)
        except TimeoutError:
            return _Failure(TIMEOUT, fail_run)
        except ZonicError as error:
            return _Failure(
                INVALID if error.code == INVALID else error.code, fail_run and error.code != INVALID
            )
        except Exception:  # noqa: BLE001  # a hook's own failure follows its failure policy
            return _Failure(FAILED, fail_run)
        if not isinstance(result, HookResult) or result.action not in _ALLOWED.get(
            point, frozenset({_CONTINUE})
        ):
            return _Failure(INVALID, False)
        return result

    @staticmethod
    def _routable(point: HookPoint, item: ResolvedBinding, result: HookResult, spec: NodeSpec | None) -> bool:
        if result.action == _CONTINUE and (point != "afterNode" or not item.binding.can_route):
            return False
        known = set(spec.handles) | set(spec.optional_handles) if spec is not None else set()
        return all(h in known for h in result.handles or [])

    @staticmethod
    def _mode(item: ResolvedBinding) -> str:
        """A hook type may decide its mode itself (``webhook``: ``config.mode``)."""
        decide = getattr(item.hook, "mode", None)
        return str(decide(item.config)) if callable(decide) else item.binding.mode

    def approvals(self, blueprint: Blueprint, point: HookPoint, node_id: str) -> list[ResolvedBinding]:
        """The node's ``approval`` bindings at ``point`` (``beforeNode``, ``beforeToolCall``): their
        requests are asked before anything runs, and the hook applies the answers (case K-11)."""
        return [b for b in self.resolver.of(blueprint).node(point, node_id) if b.binding.hook == APPROVAL]

    def stream_mask(self, blueprint: Blueprint, node_id: str) -> StreamMaskRules | None:
        """The patterns of a node's blocking ``mask`` bindings at ``afterNode``, for its streamed
        text (the Node.js runtime's deviation H-6). Their ``when`` conditions are not known while
        the node streams, so every binding counts."""
        masks = [
            item
            for item in self.resolver.of(blueprint).node("afterNode", node_id)
            if item.binding.hook == _MASK and self._mode(item) == "blocking"
        ]
        patterns = tuple(dict.fromkeys(p for item in masks for p in getattr(item.config, "patterns", ())))
        if not patterns:
            return None
        given = [r for r in (getattr(item.config, "replacement", None) for item in masks) if r]
        return StreamMaskRules(patterns, given[0] if given else self._settings.mask_replacement)

    def _dispatch(self, item: ResolvedBinding, context: HookContext, outcome: HookOutcome) -> None:
        """Not awaited, on copies; its result is discarded, so it cannot change anything."""
        if len(self._background) >= self._settings.async_queue_size:
            outcome.warnings.append(_warning(IGNORED, item.binding.id, context.spec, cause="queue full"))
            return
        seconds = (item.binding.timeout_ms or self._settings.default_timeout_milliseconds) / 1000
        copied = replace(
            context,
            input=copy.deepcopy(context.input),
            output=copy.deepcopy(context.output),
            result=copy.deepcopy(context.result),
            parts=copy.deepcopy(context.parts),
        )

        async def run() -> None:
            try:
                async with asyncio.timeout(seconds):
                    await item.hook.run(copied, item.config)
            except Exception:  # noqa: BLE001  # an asynchronous hook never affects the run
                return

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def idle(self) -> None:
        """Waits for the asynchronous hooks in flight (tests and shutdown)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)


def _warning(code: str, binding_id: str, spec: NodeSpec | None, **details: Any) -> dict[str, Any]:
    warning: dict[str, Any] = {"code": code, "binding": binding_id, **details}
    if spec is not None:
        warning["nodeId"] = spec.node_id
    return warning
