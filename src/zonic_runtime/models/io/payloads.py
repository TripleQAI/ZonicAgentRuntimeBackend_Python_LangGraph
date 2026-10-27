"""The 12 built-in payload models (design 8.2; plan P1-07), ported from the Node.js ``payloads.ts``.

Each model is registered under ``<name>@<version>`` by ``@payload(name, version)``. A new
version of a payload is a new model with version ``n + 1``; manifest entries and documents
name the version they use (case I-08). A payload may add fields to the builder's
``outputSchema`` of a node type, never remove or retype them (case I-02).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.engine.records import DatasetRef

M = TypeVar("M", bound=type[BaseModel])

_PAYLOADS: dict[str, type[BaseModel]] = {}


def payload(name: str, version: int) -> Callable[[M], M]:
    def apply(cls: M) -> M:
        key = f"{name}@{version}"
        if _PAYLOADS.get(key, cls) is not cls:
            raise TypeError(f"two payload models registered as {key}")
        _PAYLOADS[key] = cls
        return cls

    return apply


def payload_models() -> dict[str, type[BaseModel]]:
    return dict(sorted(_PAYLOADS.items()))


class _Open(ZonicModel):
    """A payload whose JSON Schema accepts any property."""

    model_config = ConfigDict(extra="allow")


@payload("zonic.text", 1)
class TextPayload(ZonicModel):
    """Text from a model: LLM nodes."""

    text: str = ""
    #: The parsed answer when the node asks for structured output; absent otherwise, and then
    #: ``{{nodes.X.output.result}}`` reads ``text`` through the type's alias.
    result: Any = None
    finish_reason: str | None = None
    structured_output: Any = None


class ChatMessage(ZonicModel):
    role: str
    content: Any = None


@payload("zonic.chat-messages", 1)
class ChatMessagesPayload(ZonicModel):
    messages: list[ChatMessage] = Field(default_factory=list)


@payload("zonic.rows", 1)
class RowsPayload(ZonicModel):
    """Database rows: an inline preview, with the full result as a dataset (design 9.3)."""

    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    columns: list[str] = Field(default_factory=list)
    dataset_ref: DatasetRef | None = None


@payload("zonic.http-request", 1)
class HttpRequestPayload(ZonicModel):
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


@payload("zonic.http-response", 1)
class HttpResponsePayload(ZonicModel):
    """``body`` is the text, ``json`` the parsed body when it is JSON."""

    status: int = 0
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    json_: Any = Field(default=None, alias="json")
    dataset_ref: DatasetRef | None = None


@payload("zonic.agent-result", 1)
class AgentResultPayload(ZonicModel):
    text: str = ""
    #: The answer as the builder names it: the parsed JSON when the text is JSON, else the text.
    result: Any = None
    structured_output: Any = None
    stop_reason: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    #: Tool name -> its result; templates match the names case-insensitively (design 5.2).
    tool_results: dict[str, Any] = Field(default_factory=dict)
    #: The chosen handoff target, if any.
    handoff: str | None = None


@payload("zonic.swarm-result", 1)
class SwarmResultPayload(ZonicModel):
    status: str = ""
    text: str = ""
    final_agent: str | None = None
    node_history: list[str] = Field(default_factory=list)
    execution_time_ms: int = Field(default=0, ge=0)


@payload("zonic.decision", 1)
class DecisionPayload(ZonicModel):
    """A routing decision: Rule and Guardrail nodes. ``handle`` is the activated handle."""

    handle: str = ""
    matched_rule: str | None = None
    passed: bool | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)


@payload("zonic.approval", 1)
class ApprovalPayload(ZonicModel):
    approved: bool = False
    comment: str | None = None
    decided_by: str | None = None


@payload("zonic.value", 1)
class ValuePayload(ZonicModel):
    """One value: the answer of a Human Input node."""

    value: Any = None


@payload("zonic.mapping", 1)
class MappingPayload(_Open):
    """A free-form mapping: start, variable, script, view and tool nodes; a node's own
    ``outputSchema`` or a manifest extension narrows it."""


@payload("zonic.run-result", 1)
class RunResultPayload(_Open):
    """The run result built by an Output node, or by a child workflow for its sub-flow node."""
