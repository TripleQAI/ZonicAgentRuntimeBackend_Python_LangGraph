"""The hook type contract (design 13, "Adding a hook type"; case K-16).

A hook type is one class with a configuration model, registered by name; bindings in
configuration or documents use it without compiler or kind changes. A hook sees copies of the
point's values through ``HookContext`` and returns a ``HookResult``: changes travel in its
``patch``, ``handles`` and ``updates``, never by mutating what it was given.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.hooks.binding import HookBinding, HookPoint
from zonic_runtime.models.hooks.result import HookResult
from zonic_runtime.models.nodes.base import ConditionGroup


class HookRun(Protocol):
    """What a hook may read in its run: templates, ``when`` conditions, schemas."""

    @property
    def run_id(self) -> str: ...

    @property
    def blueprint(self) -> Blueprint: ...

    def resolve(self, value: Any, node_output: Mapping[str, Any] | None = None) -> Any: ...

    def holds(self, when: ConditionGroup, node_output: Mapping[str, Any] | None = None) -> bool: ...

    def validate(self, schema_id: str, value: Any) -> list[str]:
        """Problems of ``value`` against a named schema; ``SCHEMA_UNKNOWN`` raises."""
        ...

    @property
    def path(self) -> str:
        """The sub-flow chain the hook runs in (workflow IDs); empty at the root (P3-10)."""
        ...

    def warn(self, code: str, message: str, node_id: str | None = None) -> None:
        """A warning of the hook (``guardrail.flagged``, ``hook.cacheUnavailable``) on the run."""
        ...

    def scope_id(self, scope: str) -> str | None:
        """The ID of ``run``, ``session``, ``user`` or ``tenant`` for scoped hooks (``cache``)."""
        ...

    def trace_hook(self, name: str, level: str, metadata: Mapping[str, Any], *, span: bool) -> None:
        """The hook's observation under its node: a ``span`` per blocking execution, an ``event``
        per asynchronous dispatch (design 13)."""
        ...

    def annotate(
        self, tags: Sequence[str], metadata: Mapping[str, Any], score: Mapping[str, Any] | None
    ) -> None:
        """Tags, metadata and a score on the node's observation, or on the run's at run-level
        points (the ``annotate`` hook)."""
        ...


@dataclass
class HookContext:
    point: HookPoint
    binding: HookBinding
    run: HookRun
    spec: NodeSpec | None = None
    visit: int | None = None
    #: ``beforeNode``: the input port values, after earlier hooks' changes.
    input: dict[str, Any] | None = None
    #: ``afterNode``: the output data and handles, after earlier hooks' changes.
    output: dict[str, Any] | None = None
    #: ``afterNode``: the node's content parts (views), after earlier hooks' changes.
    parts: list[dict[str, Any]] | None = None
    handles: list[str] | None = None
    #: ``onNodeError``, ``onRunError``: the error; ``onNodeError``: hook retries so far this visit.
    error: dict[str, str] | None = None
    attempt: int = 0
    #: ``afterRun``: the run result.
    result: dict[str, Any] | None = None
    #: Model and tool points: the call (tool name and arguments, or the model's last message).
    call: dict[str, Any] = field(default_factory=dict)


class NodeHook(Protocol):
    name: ClassVar[str]
    config: ClassVar[type[BaseModel]]

    def check(self, config: Any) -> str | None:
        """A problem a valid configuration can still have, found when bindings are resolved."""
        ...

    async def run(self, context: HookContext, config: Any) -> HookResult: ...


class BaseHook:
    """Defaults for hook types: nothing to check beyond the configuration model."""

    def check(self, config: Any) -> str | None:
        return None
