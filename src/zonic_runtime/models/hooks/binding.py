"""Hook bindings (design Section 13; plan P2-06).

A binding names a registered hook type, the hook points it runs at, which nodes it matches (every
non-empty list must match), a ``when`` condition (the Rule node's condition group), its order,
mode, timeout and failure policy, and a configuration validated against the hook type's model.
Bindings come from ``resources/hooks.json`` (global), a document's ``runtimeHooks`` (workflow) and
a node's ``properties.hooks`` (node); the defaults are the Node.js runtime's.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicResourceModel
from zonic_runtime.models.nodes.base import ConditionGroup
from zonic_runtime.models.registry import exported

HookPoint = Literal[
    "beforeRun",
    "afterRun",
    "onRunError",
    "beforeNode",
    "afterNode",
    "onNodeError",
    "beforeModelCall",
    "afterModelCall",
    "beforeToolCall",
    "afterToolCall",
    "beforeMemberCall",
    "onHandoff",
    "onInterrupt",
    "onResume",
]


class HookMatch(ZonicResourceModel):
    node_kinds: list[str] = Field(default_factory=list)
    node_types: list[str] = Field(default_factory=list)
    node_names: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    #: The node's ``properties.tags``; any one matches.
    tags: list[str] = Field(default_factory=list)


class HookBinding(ZonicResourceModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$")
    #: The hook type registered in the hook registry.
    hook: str = Field(min_length=1)
    on: list[HookPoint] = Field(min_length=1)
    match: HookMatch = Field(default_factory=HookMatch)
    when: ConditionGroup | None = None
    order: int = 100
    mode: Literal["blocking", "async"] = "blocking"
    #: Per execution; absent means ``settings.hooks.defaultTimeoutMilliseconds``.
    timeout_ms: int | None = Field(default=None, gt=0)
    failure_policy: Literal["fail_node", "fail_run", "ignore"] = "fail_node"
    #: ``afterNode`` may change the activated handles.
    can_route: bool = False
    #: A node's ``properties.disabledHooks`` may turn it off.
    overridable: bool = True
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


@exported("resources")
class HookBindings(ZonicResourceModel):
    bindings: list[HookBinding] = Field(default_factory=list)
