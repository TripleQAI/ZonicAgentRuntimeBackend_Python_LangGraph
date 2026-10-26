"""Hook bindings (design Section 13)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicResourceModel
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
    tags: list[str] = Field(default_factory=list)


class HookCondition(ZonicResourceModel):
    field: str
    op: str
    value: Any = None


class HookWhen(ZonicResourceModel):
    condition_logic: Literal["AND", "OR"]
    conditions: list[HookCondition]


class HookBinding(ZonicResourceModel):
    id: str
    hook: str
    on: list[HookPoint] = Field(min_length=1)
    match: HookMatch = Field(default_factory=HookMatch)
    when: HookWhen | None = None
    order: int
    mode: Literal["blocking", "async"]
    timeout_ms: int = Field(gt=0)
    failure_policy: Literal["fail_node", "fail_run", "ignore"]
    can_route: bool
    overridable: bool
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)


@exported("resources")
class HookBindings(ZonicResourceModel):
    bindings: list[HookBinding]
