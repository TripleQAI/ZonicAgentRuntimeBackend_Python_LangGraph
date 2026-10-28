"""The kinds the runtime ships, by manifest kind name (design 8.4).

A kind's name comes from its class name (``LlmKind`` serves ``llm``, ``RouterKind`` serves
``router``), so the manifest stays the only place kind names are written (design rule 1).
Later tasks add their kinds here.
"""

from __future__ import annotations

import re

from zonic_runtime.engine.kinds.database_kind import McpExecuteKind
from zonic_runtime.engine.kinds.flow_kinds import EntryKind, IgnoreKind, OutputKind, RouterKind, VariableKind
from zonic_runtime.engine.kinds.integration_kinds import ExternalAgentKind, GuardrailKind, ToolKind
from zonic_runtime.engine.kinds.interrupt_kind import InterruptKind
from zonic_runtime.engine.kinds.io_kinds import HttpKind, SandboxKind, ViewKind
from zonic_runtime.engine.kinds.model_kinds import AgentKind, LlmKind
from zonic_runtime.engine.kinds.registry import KindFactory, KindRegistry
from zonic_runtime.engine.kinds.subflow_kind import SubflowKind
from zonic_runtime.engine.kinds.swarm_kind import MultiAgentKind

_SUFFIX = "Kind"


def kind_name(cls: type) -> str:
    stem = cls.__name__.removesuffix(_SUFFIX)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()


def core_kinds() -> dict[str, KindFactory]:
    classes: tuple[type, ...] = (
        EntryKind,
        LlmKind,
        AgentKind,
        RouterKind,
        VariableKind,
        OutputKind,
        IgnoreKind,
        HttpKind,
        SandboxKind,
        ViewKind,
        SubflowKind,
        McpExecuteKind,
        ToolKind,
        ExternalAgentKind,
        GuardrailKind,
        MultiAgentKind,
        InterruptKind,
    )
    return {kind_name(cls): cls for cls in classes}


def core_registry() -> KindRegistry:
    return KindRegistry(core_kinds())
