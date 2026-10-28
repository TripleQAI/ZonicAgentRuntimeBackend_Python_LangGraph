"""What a blocking hook returns, and the configurations of the built-in hook types (design 13)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.engine import StateUpdate

#: Decisions a hook may return; each hook point allows some of them.
HookAction = Literal["continue", "skip", "fail", "retry", "recover", "cancel"]


class HookPatch(ZonicModel):
    #: Port values (``beforeNode``).
    input: dict[str, Any] | None = None
    #: Output fields (``afterNode``, ``recover``).
    output: dict[str, Any] | None = None
    #: Result fields (``afterRun``).
    result: dict[str, Any] | None = None
    #: The node's content parts (``afterNode``; a ``mask`` hook masks them too).
    parts: list[dict[str, Any]] | None = None


class HookResult(ZonicModel):
    action: HookAction = "continue"
    patch: HookPatch = Field(default_factory=HookPatch)
    #: Activated handles (``skip``, ``recover``, and ``afterNode`` with ``canRoute``).
    handles: list[str] | None = None
    #: The error code of ``fail``.
    code: str | None = None
    #: The wait before a ``retry``.
    delay_ms: int = Field(default=0, ge=0)
    #: ``cancel`` at a model or tool call: what the model is told instead.
    message: str | None = None
    #: State operations (``setVariables``); the template applies them with the node's updates.
    updates: list[StateUpdate] = Field(default_factory=list)


class SetVariablesHookConfig(ZonicModel):
    updates: list[StateUpdate] = Field(default_factory=list)


class TransformSet(ZonicModel):
    #: ``input.<port>``, ``output.<field>`` or ``result.<field>``.
    path: str
    value: Any = None


class TransformHookConfig(ZonicModel):
    set: list[TransformSet] = Field(default_factory=list)


class ValidateHookConfig(ZonicModel):
    #: A payload schema ID (``zonic.text@1``) or a port schema (``port.text@1``).
    model: str
    #: The value to check (a template); absent means the input, output or result of the point.
    value: Any = None
    on_invalid: Literal["fail", "warn"] = "fail"
    #: The error code of ``fail``; it must be in the error catalog.
    code: str = "INPUT_INVALID"


class RetryHookConfig(ZonicModel):
    attempts: int = Field(default=1, ge=1)
    backoff_milliseconds: int = Field(default=0, ge=0)
    #: Retried codes; empty means any.
    codes: list[str] = Field(default_factory=list)


class FallbackHookConfig(ZonicModel):
    output: dict[str, Any] = Field(default_factory=dict)
    activate: list[str] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)


class ApprovalHookConfig(ZonicModel):
    """``approval`` (beforeNode, beforeToolCall; P4-03, case K-11): a person approves before the
    node or the tool call runs. The request is asked before any side effect (the template's step
    2, the tool call's wrapper); the hook then applies the answer. Rejected, a node is skipped with
    ``rejectedOutput`` and ``rejectedHandles`` (or fails with ``onReject: fail``), and a tool call
    is cancelled with ``message`` as its result."""

    #: The request shown to the approver; a node's templates are resolved in its context.
    prompt: str
    approvers: list[str] = Field(default_factory=list)
    #: When the request expires; ``interrupts.defaultExpiryHours`` when absent.
    timeout_hours: float | None = Field(default=None, gt=0)
    on_reject: Literal["skip", "fail"] = "skip"
    rejected_handles: list[str] = Field(default_factory=list)
    rejected_output: dict[str, Any] = Field(default_factory=dict)
    code: str | None = None
    message: str = "The approver rejected this tool call."


# -- P3-10: the integration hook types (the Node.js runtime's ``hook-configs.ts``) ------------------


class MaskHookConfig(ZonicModel):
    """``mask`` (beforeNode, afterNode, afterRun): the values of ``keys`` (any depth) and the text
    matching ``patterns`` (``settings.hooks.patterns`` names or ``/source/flags``) replaced in the
    input, the output and its content parts, or the result, before state, the stream and the trace."""

    keys: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    #: ``settings.hooks.maskReplacement`` when absent.
    replacement: str | None = None


class GuardrailHookConfig(ZonicModel):
    """``guardrail``: ``policy_model`` asks the policy model about
    ``settings.guardrails.policies[policyRef]``; ``patterns`` flags matching text; ``bedrock``
    applies the provisioned guardrail. ``fields`` are paths in the point's data (``input.<port>``,
    ``output.<field>``, ``result.<field>``) or templates; empty means all of it. ``block`` fails
    (``GUARDRAIL_BLOCKED``), ``warn`` warns, ``redact`` replaces."""

    provider: Literal["policy_model", "patterns", "bedrock"] = "policy_model"
    policy_ref: str | None = None
    guardrail_id: str | None = Field(default=None, min_length=1)
    guardrail_version: str | None = None
    patterns: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    action: Literal["block", "warn", "redact"] = "block"
    replacement: str | None = None


class CacheHookConfig(ZonicModel):
    """``cache`` (beforeNode with afterNode): a node's output and handles kept ``ttlSeconds`` under
    the resolved ``key`` (by default the bound input), per ``scope``; a hit before the node skips it."""

    key: Any = None
    ttl_seconds: int = Field(ge=1)
    scope: Literal["session", "user", "tenant"] = "session"


class RateLimitHookConfig(ZonicModel):
    """``rateLimit`` (beforeNode): at most ``limit`` executions per ``windowSeconds`` per ``scope``."""

    limit: int = Field(ge=1)
    window_seconds: int = Field(ge=1)
    scope: Literal["session", "user", "tenant", "global"] = "user"


class WebhookHookConfig(ZonicModel):
    """``webhook`` (any point): the masked payload posted to ``settings.hooks.endpoints[endpointRef]``
    (on the egress allow list), signed ``x-zonic-signature: sha256=<HMAC>`` with ``secretRef``.
    Asynchronous by default (``mode``, not the binding's); blocking, ``{"action": "fail"}`` vetoes."""

    endpoint_ref: str = Field(min_length=1)
    secret_ref: str | None = Field(default=None, min_length=1)
    mode: Literal["async", "blocking"] = "async"
    timeout_ms: int | None = Field(default=None, ge=1)
    mask_keys: list[str] = Field(default_factory=list)
    #: ``settings.hooks.webhookMaskPatterns`` when absent.
    mask_patterns: list[str] | None = None


class McpToolInput(ZonicModel):
    key: str
    value: Any = None


class McpToolOutput(ZonicModel):
    target: str = Field(pattern=r"^(input|output|result)\.[^.]")
    path: str = ""


class McpToolHookConfig(ZonicModel):
    """``mcpTool`` (any point): an MCP server's tool called with the mapped arguments; its result
    (or the value at ``path``) set at ``input.<port>``, ``output.<field>`` or ``result.<field>``."""

    server: str
    tool: str
    input_mapping: list[McpToolInput] = Field(default_factory=list)
    output_mapping: list[McpToolOutput] = Field(default_factory=list)


class AnnotateScore(ZonicModel):
    name: str
    value: Any = None
    comment: str | None = None


class AnnotateHookConfig(ZonicModel):
    """``annotate`` (afterNode, afterRun): tags, metadata and a score on the node's observation, or
    on the trace at run-level points; templates resolved."""

    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: AnnotateScore | None = None
