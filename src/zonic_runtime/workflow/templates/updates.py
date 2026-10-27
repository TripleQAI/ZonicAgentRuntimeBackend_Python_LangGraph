"""State operations and output fields (design 7.1, 8.1 step 10; plan P1-06, cases E-12, F-04).

Ported from the Node.js ``state-writer.ts`` and ``output-mapping.ts``, with one change for
LangGraph: nodes never write state (design rule 15). ``UpdateWriter.apply`` evaluates a
node's ``variableUpdates`` and ``stateUpdates`` on a copy-on-write view and returns the
writes per namespace; the template turns them into a ``StatePatch`` (P1-08). Later
updates of the same node see the earlier ones. The operations are the builder's
``stateOps``: set, append, extend, merge, increment, remove, clear. The normalizer has
already mapped the ``operation`` spelling onto ``op`` and ``fieldName`` onto ``path``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import ValidationError

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.dialects import OutputMappingEntry
from zonic_runtime.models.engine import StateUpdate
from zonic_runtime.runtime.paths import delete_path, get_path, set_path
from zonic_runtime.workflow.templates.conditions import ConditionEvaluator
from zonic_runtime.workflow.templates.resolver import (
    DEFAULT_THREAD,
    TemplateContext,
    TemplateIssues,
    TemplateResolver,
)

#: A message written by an update gets this role unless the update names one.
DEFAULT_ROLE = "assistant"


class _Removed:
    def __repr__(self) -> str:
        return "REMOVED"


#: The value of a path an operation deleted.
REMOVED: Any = _Removed()


def _unquote(value: Any) -> Any:
    """A string holding a JSON string literal (``"\\"\\""`` in stored documents) reads as that string."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return parsed if isinstance(parsed, str) else value
    return value


def _deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    merged = dict(base)
    for key, value in patch.items():
        merged[key] = _deep_merge(base.get(key), value)
    return merged


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return [] if value is None else [value]


def apply_op(current: Any, op: str, value: Any) -> Any:
    """The new value of a path; ``REMOVED`` when the operation deletes it."""
    match op:
        case "set":
            return value
        case "append":
            return [*_as_list(current), value]
        case "extend":
            return [*_as_list(current), *(value if isinstance(value, list) else [value])]
        case "merge":
            return _deep_merge(current if current is not None else {}, value)
        case "increment":
            step = 1.0 if value in (None, "") else _float(value, 1.0)
            total = _float(current, 0.0) + step
            return int(total) if total.is_integer() else total
        case "remove":
            if isinstance(current, list) and value is not None:
                key = _key(value)
                return [item for item in current if _key(item) != key]
            return REMOVED
        case "clear":
            if isinstance(current, list):
                return []
            if isinstance(current, dict):
                return {}
            return REMOVED
    raise ZonicError("PLAN_INVALID", reason=f"unknown state operation {op!r}")


def _float(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    try:
        found = float(value)
    except (TypeError, ValueError):
        return fallback
    return found if found == found and found not in (float("inf"), float("-inf")) else fallback


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


@dataclass
class UpdateResult:
    """What a node's updates write, per namespace (top-level ``flow`` keys)."""

    flow: dict[str, Any] = field(default_factory=dict)
    flow_removed: list[str] = field(default_factory=list)
    system: dict[str, Any] = field(default_factory=dict)
    #: ``{role, content, thread}``; ``thread`` is ``messages`` for the chat thread.
    messages: list[dict[str, Any]] = field(default_factory=list)
    cleared_threads: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class UpdateWriter:
    def __init__(
        self,
        resolver: TemplateResolver,
        evaluator: ConditionEvaluator,
        *,
        writable: Collection[str],
        writable_system_keys: Collection[str],
        state_ops: Collection[str],
    ) -> None:
        self._resolver = resolver
        self._evaluator = evaluator
        self._writable = frozenset(writable)
        self._system_keys = frozenset(writable_system_keys)
        self._ops = frozenset(state_ops)

    def apply(
        self, updates: Sequence[StateUpdate | Mapping[str, Any]], context: TemplateContext
    ) -> UpdateResult:
        result = UpdateResult()
        flow = dict(context.scope.flow)
        system = dict(context.scope.system)
        messages = list(context.scope.messages)
        for raw in updates:
            try:
                update = raw if isinstance(raw, StateUpdate) else StateUpdate.model_validate(raw)
            except ValidationError as exc:
                raise ZonicError(
                    "PLAN_INVALID", reason=exc.errors()[0]["msg"], node_id=context.node_id
                ) from exc
            view = replace(context, scope=replace(context.scope, flow=flow, system=system, messages=messages))
            issues = TemplateIssues()

            def resolve(value: Any, view: TemplateContext = view, issues: TemplateIssues = issues) -> Any:
                return self._resolver.resolve(value, view, issues)

            if update.when is not None and not self._evaluator.all(
                update.when.conditions, update.when.condition_logic, resolve
            ):
                continue
            path = update.path if "{{" not in update.path else resolve(update.path)
            if not isinstance(path, str) or not path.strip():
                result.warnings.append(
                    _warning(
                        "STATE_PATH_EMPTY",
                        f"the path {update.path} resolved to nothing; the state operation is skipped",
                        context.node_id,
                    )
                )
                continue
            value = _unquote(resolve(update.value))
            self._write(path.strip(), update.op, value, update.role, flow, system, messages, result, context)
            for missing in issues.missing:
                if missing not in result.missing:
                    result.missing.append(missing)
        return result

    def _write(
        self,
        path: str,
        op: str,
        value: Any,
        role: str | None,
        flow: dict[str, Any],
        system: dict[str, Any],
        messages: list[Mapping[str, Any]],
        result: UpdateResult,
        context: TemplateContext,
    ) -> None:
        if op not in self._ops:
            raise ZonicError("PLAN_INVALID", reason=f"unknown state operation {op!r}", path=path)
        namespace, segments = self._resolver.canonical(path)
        if namespace not in self._writable and namespace != "system":
            raise ZonicError("STATE_READ_ONLY", path=path)
        if namespace in ("messages", "thread"):
            thread = segments[0] if namespace == "thread" and segments else DEFAULT_THREAD
            self._write_messages(op, value, role, thread, messages, result, context)
            return
        if not segments:
            raise ZonicError("STATE_READ_ONLY", path=path)
        if namespace == "system":
            if segments[0] not in self._system_keys:
                raise ZonicError("STATE_READ_ONLY", path=path)
            target, written = system, result.system
        else:
            target, written = flow, result.flow
        top = segments[0]
        # Copy on write: the scope's own value is never changed.
        if top in target:
            target[top] = copy.deepcopy(target[top])
        key = ".".join(segments)
        new = apply_op(get_path(target, key), op, value)
        if new is REMOVED:
            delete_path(target, key)
        else:
            set_path(target, key, new)
        if top in target:
            written[top] = target[top]
            if target is flow and top in result.flow_removed:
                result.flow_removed.remove(top)
        else:
            written.pop(top, None)
            if target is flow and top not in result.flow_removed:
                result.flow_removed.append(top)

    def _write_messages(
        self,
        op: str,
        value: Any,
        role: str | None,
        thread: str,
        messages: list[Mapping[str, Any]],
        result: UpdateResult,
        context: TemplateContext,
    ) -> None:
        def message(content: Any, own_role: str | None = None) -> dict[str, Any]:
            found = {"role": own_role or role or DEFAULT_ROLE, "content": content, "thread": thread}
            return found | ({"nodeId": context.node_id} if context.node_id else {})

        match op:
            case "set" | "append":
                added = [message(value)]
            case "extend":
                added = [
                    message(item.get("content"), item.get("role"))
                    if isinstance(item, dict) and isinstance(item.get("role"), str)
                    else message(item)
                    for item in (value if isinstance(value, list) else [value])
                ]
            case "clear":
                messages[:] = [m for m in messages if m.get("thread", DEFAULT_THREAD) != thread]
                result.messages = [m for m in result.messages if m["thread"] != thread]
                if thread not in result.cleared_threads:
                    result.cleared_threads.append(thread)
                return
            case _:
                raise ZonicError("PLAN_INVALID", reason=f"state operation {op} does not apply to messages")
        messages.extend(added)
        result.messages.extend(added)


def convert_mapped(value: Any, type_: str | None) -> Any:
    """A resolved value as a builder mapping type (``mappingTypes``); ``variable`` and unknown
    types keep the value as it is."""
    match type_:
        case "string":
            return (
                ""
                if value is None
                else value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )
        case "number":
            if isinstance(value, int | float) and not isinstance(value, bool):
                return value
            try:
                number = float(str("" if value is None else value).strip())
            except ValueError:
                return value
            return int(number) if number.is_integer() else number
        case "boolean":
            return value if isinstance(value, bool) else str(value).strip().lower() == "true"
        case "array" | "object":
            if not isinstance(value, str):
                return value
            try:
                return json.loads(value)
            except ValueError:
                return value
    return value


def build_mapping(
    entries: Sequence[OutputMappingEntry | Mapping[str, Any]],
    resolver: TemplateResolver,
    context: TemplateContext,
    issues: TemplateIssues | None = None,
) -> dict[str, Any]:
    """Named fields from mapping entries (``outputStructure``, Output ``outputMapping``)."""
    issues = issues if issues is not None else TemplateIssues()
    built: dict[str, Any] = {}
    for raw in entries:
        entry = raw if isinstance(raw, OutputMappingEntry) else OutputMappingEntry.model_validate(raw)
        built[entry.target] = convert_mapped(resolver.resolve(entry.value, context, issues), entry.type)
    return built


def _warning(code: str, message: str, node_id: str | None) -> dict[str, Any]:
    return {"code": code, "message": message} | ({"nodeId": node_id} if node_id else {})
