"""The integration hook types (design 13; plan P3-10, cases K-08, K-14): ``mask``, ``guardrail``,
``cache``, ``rateLimit``, ``webhook``, ``mcpTool`` and ``annotate``. Ported from the Node.js
runtime's ``hooks/types/integration.ts``, so a binding behaves the same in both runtimes.

Ports, all optional at construction (a binding that needs a missing one fails ``check`` when the
bindings are resolved, never during a run): the egress guard (``webhook``), secrets (``webhook``
signatures), the MCP port (``mcpTool``), the cache store (``cache``, ``rateLimit``: this process
without it), the guardrail port (``guardrail`` with ``policy_model`` or ``bedrock``, P3-06).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel

from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.types import BaseHook, HookContext, NodeHook
from zonic_runtime.models.hooks.result import (
    AnnotateHookConfig,
    CacheHookConfig,
    GuardrailHookConfig,
    HookPatch,
    HookResult,
    MaskHookConfig,
    McpToolHookConfig,
    RateLimitHookConfig,
    WebhookHookConfig,
)
from zonic_runtime.models.resources.settings import GuardrailSettings, HookSettings
from zonic_runtime.ports.datasets import CacheStorePort
from zonic_runtime.ports.egress import EgressPort, EgressRequest
from zonic_runtime.ports.guardrail import GuardrailPort, ManagedGuardrailPort, Source
from zonic_runtime.ports.mcp import McpPort, mcp_content
from zonic_runtime.ports.secrets import SecretPort, secret_name
from zonic_runtime.runtime.lru import LruCache
from zonic_runtime.runtime.paths import get_path, set_path
from zonic_runtime.runtime.redact import compile_patterns, redact, unknown_patterns

#: The data a hook point owns, by namespace.
_INPUT, _OUTPUT, _RESULT = "input", "output", "result"  # principle: allow rule-1-node-type data-namespaces
_NAMESPACES = (_INPUT, _OUTPUT, _RESULT)
_HOOK_HEADER, _SIGNATURE_HEADER = "x-zonic-hook", "x-zonic-signature"
BLOCKED, FAILED, INVALID, LIMITED = "GUARDRAIL_BLOCKED", "HOOK_FAILED", "HOOK_RESULT_INVALID", "RATE_LIMITED"
FLAGGED, CACHE_UNAVAILABLE, TOOL_FAILED = "guardrail.flagged", "hook.cacheUnavailable", "TOOL_FAILED"
_TEMPLATE = "{{"


def _point_data(context: HookContext) -> dict[str, dict[str, Any]]:
    found = {}
    for namespace in _NAMESPACES:
        value = getattr(context, namespace)
        if value is not None:
            found[namespace] = copy.deepcopy(value)
    return found


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _replace_texts(value: Any, replacement: str) -> Any:
    if isinstance(value, str):
        return replacement
    if isinstance(value, list):
        return [_replace_texts(v, replacement) for v in value]
    if isinstance(value, Mapping):
        return {k: _replace_texts(v, replacement) for k, v in value.items()}
    return value


def _masked_value(current: Any, text: str, replacement: str) -> Any:
    if isinstance(current, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return _replace_texts(current, replacement)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _node_id(context: HookContext) -> str | None:
    return context.spec.node_id if context.spec is not None else None


class MaskHook(BaseHook):
    """``mask``: replaces the configured keys and patterns of personal data in the input, the
    output and its content parts, or the result, before they reach state, the stream and the trace."""

    name: ClassVar[str] = "mask"
    config: ClassVar[type[BaseModel]] = MaskHookConfig

    def __init__(self, settings: HookSettings) -> None:
        self._settings = settings

    def check(self, config: Any) -> str | None:
        unknown = unknown_patterns(config.patterns, self._settings.patterns)
        return f"unknown patterns {', '.join(unknown)}" if unknown else None

    async def run(self, context: HookContext, config: Any) -> HookResult:
        replacement = config.replacement or self._settings.mask_replacement
        patch: dict[str, Any] = {}
        named = self._settings.patterns
        for namespace, value in _point_data(context).items():
            masked, count = redact(
                value, named, keys=config.keys, patterns=config.patterns, replacement=replacement
            )
            if count:
                patch[namespace] = masked
        if context.parts:
            masked, count = redact(
                context.parts, named, keys=config.keys, patterns=config.patterns, replacement=replacement
            )
            if count:
                patch["parts"] = masked
        return HookResult(patch=HookPatch(**patch)) if patch else HookResult()


class GuardrailHook(BaseHook):
    """``guardrail``: checks the configured fields (the point's data by default, the user's message
    before the run) with the policy model, patterns or a Bedrock guardrail; ``block`` fails with
    ``GUARDRAIL_BLOCKED``, ``warn`` warns ``guardrail.flagged``, ``redact`` replaces the fields."""

    name: ClassVar[str] = "guardrail"  # principle: allow rule-1-node-type hook-type-name
    config: ClassVar[type[BaseModel]] = GuardrailHookConfig

    def __init__(
        self,
        settings: HookSettings,
        guardrails: GuardrailSettings,
        guard: GuardrailPort | None = None,
        managed: ManagedGuardrailPort | None = None,
    ) -> None:
        self._settings, self._guardrails, self._guard, self._managed = settings, guardrails, guard, managed

    def check(self, config: Any) -> str | None:
        if config.provider == "bedrock":
            if self._managed is None:
                return "the bedrock guardrail provider is not configured (settings.guardrails.bedrockRegion)"
            return None if config.guardrail_id else "guardrailId is required"
        if config.provider == "patterns":
            if not config.patterns:
                return "the patterns provider needs patterns"
            unknown = unknown_patterns(config.patterns, self._settings.patterns)
            return f"unknown patterns {', '.join(unknown)}" if unknown else None
        if self._guard is None:
            return "the policy model guardrail is not configured"
        if not config.policy_ref:
            return "policyRef is required"
        return (
            None if config.policy_ref in self._guardrails.policies else f"unknown policy {config.policy_ref}"
        )

    async def run(self, context: HookContext, config: Any) -> HookResult:
        data = _point_data(context)
        fields = list(config.fields) or list(data) or ["{{system.userQuery}}"]

        def value_of(field: str) -> Any:
            namespace, _, rest = field.partition(".")
            target = data.get(namespace)
            if target is not None:
                return get_path(target, rest) if rest else target
            return context.run.resolve(field, context.output)

        values = [(field, value_of(field)) for field in fields]
        replacement = config.replacement or self._settings.mask_replacement
        masked: dict[str, str] = {}
        if config.provider == "bedrock" and self._managed is not None:
            source: Source = "OUTPUT" if context.point.startswith("after") else "INPUT"
            reasons: list[str] = []
            flagged: list[str] = []
            version = config.guardrail_version or self._guardrails.default_version
            for field, value in values:
                applied = await self._managed.apply(config.guardrail_id or "", version, source, _text(value))
                if applied.intervened:
                    flagged.append(field)
                    reasons.append(applied.reason)
                    if applied.text is not None:
                        masked[field] = applied.text
            reason = "; ".join(dict.fromkeys(reasons))
        elif config.provider == "patterns":
            compiled = compile_patterns(config.patterns, self._settings.patterns)
            flagged = [f for f, v in values if any(p.search(_text(v)) for p in compiled)]
            reason = f"matched {', '.join(config.patterns)}"
        elif self._guard is not None:
            text = "\n".join(f"{field}: {_text(value)}" for field, value in values)
            decision = await self._guard.check(text, self._guardrails.policies[config.policy_ref or ""])
            flagged = [] if decision.allowed else fields
            reason = decision.reason or ", ".join(decision.categories)
        else:
            raise ZonicError(FAILED, reason="the policy model guardrail is not configured")
        if not flagged:
            return HookResult()
        if config.action == "block":
            return HookResult(action="fail", code=BLOCKED)
        if config.action == "warn":
            context.run.warn(FLAGGED, f"{context.binding.id}: {reason}", _node_id(context))
            return HookResult()
        patch: dict[str, Any] = {}
        for field in flagged:
            namespace, _, rest = field.partition(".")
            target = data.get(namespace)
            if target is None:
                context.run.warn(
                    FLAGGED, f"{context.binding.id}: {field} cannot be redacted", _node_id(context)
                )
                continue
            current = get_path(target, rest) if rest else target
            if config.provider == "patterns":
                replaced = redact(
                    current, self._settings.patterns, patterns=config.patterns, replacement=replacement
                )[0]
            elif field in masked:
                replaced = _masked_value(current, masked[field], replacement)
            else:
                replaced = _replace_texts(current, replacement)
            if rest:
                set_path(target, rest, replaced)
            else:
                data[namespace] = replaced
            patch[namespace] = data[namespace]
        return HookResult(patch=HookPatch(**patch)) if patch else HookResult()


class CacheHook(BaseHook):
    """``cache`` (beforeNode with afterNode): before the node, the key (the resolved ``key``, else
    the bound input) is looked up per scope, and a hit skips the node with the cached output and
    handles; after the node, a miss's output is kept ``ttlSeconds``. The cache service keeps it when
    one is configured, else this process. An unavailable cache is a miss, with a warning."""

    name: ClassVar[str] = "cache"
    config: ClassVar[type[BaseModel]] = CacheHookConfig

    def __init__(
        self,
        settings: HookSettings,
        store: CacheStorePort | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store, self._now = store, now
        self._misses: LruCache[str, str] = LruCache(settings.cache_max_entries)
        self._local: LruCache[str, tuple[float, dict[str, Any]]] = LruCache(settings.cache_max_entries)

    async def run(self, context: HookContext, config: Any) -> HookResult:
        spec, run, binding = context.spec, context.run, context.binding
        scope = run.scope_id(config.scope)
        if spec is None or not scope:
            return HookResult()
        visit = f"{run.run_id}|{run.path}|{spec.node_id}|{context.visit}|{binding.id}"
        if context.point == "beforeNode":
            material = run.resolve(config.key) if config.key is not None else (context.input or {})
            key = f"hook:{binding.id}:{config.scope}:{_sha(scope)}:{_sha(_canonical(material))}"
            hit = await self._get(context, key)
            if hit is not None:
                return HookResult(action="skip", patch=HookPatch(output=hit[_OUTPUT]), handles=hit["handles"])
            self._misses.put(visit, key)
            return HookResult()
        missed = self._misses.get(visit)
        if context.point != "afterNode" or missed is None:
            return HookResult()
        self._misses.pop(visit)
        key = missed
        await self._put(
            context, key, {_OUTPUT: context.output or {}, "handles": list(context.handles or [])}, config
        )
        return HookResult()

    async def _get(self, context: HookContext, key: str) -> dict[str, Any] | None:
        if self._store is None:
            entry = self._local.get(key)
            return entry[1] if entry is not None and entry[0] > self._now() else None
        try:
            value = await self._store.get(key)
        except ZonicError:
            context.run.warn(
                CACHE_UNAVAILABLE, f"{context.binding.id}: the cache is unavailable", _node_id(context)
            )
            return None
        ok = (
            isinstance(value, Mapping)
            and isinstance(value.get(_OUTPUT), Mapping)
            and isinstance(value.get("handles"), list)
        )
        return dict(value) if ok else None

    async def _put(self, context: HookContext, key: str, value: dict[str, Any], config: Any) -> None:
        if self._store is None:
            self._local.put(key, (self._now() + config.ttl_seconds, value))
            return
        try:
            await self._store.put(key, value, config.ttl_seconds)
        except ZonicError:
            context.run.warn(
                CACHE_UNAVAILABLE, f"{context.binding.id}: the cache is unavailable", _node_id(context)
            )


class RateLimitHook(BaseHook):
    """``rateLimit`` (beforeNode): at most ``limit`` node executions per ``windowSeconds`` per
    scope; the next one fails with ``RATE_LIMITED``. With the cache service the count is shared by
    every instance (a fixed window); without it, or while it is unavailable, a sliding window here."""

    name: ClassVar[str] = "rateLimit"
    config: ClassVar[type[BaseModel]] = RateLimitHookConfig

    def __init__(
        self,
        settings: HookSettings,
        store: CacheStorePort | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store, self._now = store, now
        self._windows: LruCache[str, list[float]] = LruCache(settings.rate_limit_max_keys)

    async def run(self, context: HookContext, config: Any) -> HookResult:
        scope = "global" if config.scope == "global" else context.run.scope_id(config.scope)
        if not scope:
            return HookResult()
        key = f"{context.binding.id}|{config.scope}|{scope}"
        if self._store is not None:
            shared = await self._shared(context, config, _sha(key))
            if shared is not None:
                return shared
        now = self._now()
        hits = [t for t in self._windows.get(key) or [] if t > now - config.window_seconds]
        if len(hits) >= config.limit:
            self._windows.put(key, hits)
            return HookResult(action="fail", code=LIMITED)
        self._windows.put(key, [*hits, now])
        return HookResult()

    async def _shared(self, context: HookContext, config: Any, scope: str) -> HookResult | None:
        window = int(self._now() // config.window_seconds)
        key = f"hook:rate:{scope}:{window}"
        try:
            count = int(await self._store.get(key) or 0)  # type: ignore[union-attr]
            if count >= config.limit:
                return HookResult(action="fail", code=LIMITED)
            await self._store.put(key, count + 1, config.window_seconds)  # type: ignore[union-attr]
            return HookResult()
        except ZonicError:
            context.run.warn(
                CACHE_UNAVAILABLE,
                f"{context.binding.id}: the cache is unavailable; counting here",
                _node_id(context),
            )
            return None


class WebhookHook(BaseHook):
    """``webhook``: posts the point's data, masked, to an allow-listed endpoint (checked when the
    bindings are resolved, K-08), signed with HMAC-SHA256 when ``secretRef`` names a secret.
    Asynchronous by default, so the run never waits; blocking, a non-2xx answer fails the hook
    and ``{"action": "fail", "code"?}`` vetoes."""

    name: ClassVar[str] = "webhook"
    config: ClassVar[type[BaseModel]] = WebhookHookConfig

    def __init__(self, settings: HookSettings, egress: EgressPort | None, secrets: SecretPort | None) -> None:
        self._settings, self._egress, self._secrets = settings, egress, secrets

    def mode(self, config: Any) -> str:
        return str(config.mode)

    def _patterns(self, config: Any) -> list[str]:
        return list(
            config.mask_patterns if config.mask_patterns is not None else self._settings.webhook_mask_patterns
        )

    def check(self, config: Any) -> str | None:
        url = self._settings.endpoints.get(config.endpoint_ref)
        if url is None:
            return f"unknown endpoint {config.endpoint_ref} (settings.hooks.endpoints)"
        allows = getattr(self._egress, "allows", None)
        if self._egress is None or not callable(allows) or not allows(url):
            return f"endpoint {config.endpoint_ref} is not on the egress allow list"
        unknown = unknown_patterns(self._patterns(config), self._settings.patterns)
        return f"unknown patterns {', '.join(unknown)}" if unknown else None

    async def run(self, context: HookContext, config: Any) -> HookResult:
        if self._egress is None:
            raise ZonicError(FAILED, reason="no egress")
        run, spec, binding = context.run, context.spec, context.binding
        payload: dict[str, Any] = {
            "bindingId": binding.id,
            "point": context.point,
            "runId": run.run_id,
            "agenticWorkflowId": run.blueprint.workflow_id,
        }
        if spec is not None:
            payload |= {
                "nodeId": spec.node_id,
                "nodeName": spec.name,
                "nodeType": spec.node_type,
                "visit": context.visit,
            }
        payload |= _point_data(context)
        if context.handles is not None:
            payload["handles"] = context.handles
        if context.error is not None:
            payload["error"] = context.error
        masked, _ = redact(
            payload,
            self._settings.patterns,
            keys=config.mask_keys,
            patterns=self._patterns(config),
            replacement=self._settings.mask_replacement,
        )
        body = json.dumps(masked, ensure_ascii=False, default=str)
        headers = [("content-type", "application/json"), (_HOOK_HEADER, binding.id)]
        if config.secret_ref:
            name = secret_name(config.secret_ref) or config.secret_ref
            secret = self._secrets.get(name) if self._secrets is not None else None
            if secret is None:
                raise ZonicError(FAILED, reason=f"secret {name} is not available")
            signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers.append((_SIGNATURE_HEADER, f"sha256={signature}"))
        url = self._settings.endpoints[config.endpoint_ref]
        seconds = config.timeout_ms / 1000 if config.timeout_ms else None
        response = await self._egress.send(
            EgressRequest(method="POST", url=url, headers=tuple(headers), body=body, timeout_seconds=seconds)
        )
        if not 200 <= response.status < 300:
            raise ZonicError(FAILED, status=response.status, reason=f"webhook {binding.id}")
        try:
            answer = json.loads(response.content) if response.content else None
        except ValueError:
            answer = None
        if isinstance(answer, Mapping) and answer.get("action") == "fail":
            code = answer.get("code")
            return HookResult(action="fail", code=code if isinstance(code, str) else FAILED)
        return HookResult()


class McpToolHook(BaseHook):
    """``mcpTool``: calls an MCP server's tool with the resolved ``inputMapping`` and sets its result
    (or the value at a path in it) at ``input.<port>``, ``output.<field>`` or ``result.<field>``."""

    name: ClassVar[str] = "mcpTool"
    config: ClassVar[type[BaseModel]] = McpToolHookConfig

    def __init__(self, mcp: McpPort | None) -> None:
        self._mcp = mcp

    def check(self, config: Any) -> str | None:
        return None if self._mcp is not None else "the MCP server is not configured"

    async def run(self, context: HookContext, config: Any) -> HookResult:
        arguments = {
            item.key: context.run.resolve(item.value, context.output) for item in config.input_mapping
        }
        if self._mcp is None:
            raise ZonicError(FAILED, reason="the MCP server is not configured")
        raw = await self._mcp.call(config.server, config.tool, arguments)
        value = mcp_content(raw)
        if getattr(raw, "is_error", False):
            raise ZonicError(TOOL_FAILED, node_id=_node_id(context), reason=_text(value)[:500])
        data = _point_data(context)
        patch: dict[str, Any] = {}
        for mapping in config.output_mapping:
            namespace, _, rest = mapping.target.partition(".")
            target = data.get(namespace)
            if target is None:
                raise ZonicError(
                    INVALID, reason=f"mcpTool target {mapping.target} does not apply at {context.point}"
                )
            set_path(target, rest, get_path(value, mapping.path) if mapping.path else value)
            patch[namespace] = target
        return HookResult(patch=HookPatch(**patch))


class AnnotateHook(BaseHook):
    """``annotate``: tags, metadata and a score (templates resolved) on the node's observation, or
    on the run's at run-level points. A number gives a ``NUMERIC`` score, a boolean a ``BOOLEAN``
    one, anything else a ``CATEGORICAL`` one."""

    name: ClassVar[str] = "annotate"
    config: ClassVar[type[BaseModel]] = AnnotateHookConfig

    async def run(self, context: HookContext, config: Any) -> HookResult:
        given = context.output if context.output is not None else context.result

        def resolve(value: Any) -> Any:
            return context.run.resolve(value, given)

        tags = [t for t in (_text(resolve(tag)) for tag in config.tags) if t]
        metadata = resolve(config.metadata)
        score = None
        if config.score is not None:
            value = resolve(config.score.value)
            if not isinstance(value, (int, float, bool)):
                value = _text(value)
            score = {
                "name": config.score.name,
                "value": value,
                **({"comment": config.score.comment} if config.score.comment else {}),
            }
        context.run.annotate(tags, metadata if isinstance(metadata, Mapping) else {}, score)
        return HookResult()


@dataclass(frozen=True)
class HookPorts:
    """What the integration hook types reach (the Node.js runtime's ``HookPorts``)."""

    hooks: HookSettings
    guardrails: GuardrailSettings
    egress: EgressPort | None = None
    secrets: SecretPort | None = None
    mcp: McpPort | None = None
    #: The cache service (P3-05); ``cache`` and ``rateLimit`` keep their state here without it.
    cache: CacheStorePort | None = None
    #: The policy model (P3-06) and Bedrock Guardrails; bindings that need them fail to load.
    guard: GuardrailPort | None = None
    managed_guard: ManagedGuardrailPort | None = None


def integration_hooks(ports: HookPorts) -> list[NodeHook]:
    return [
        MaskHook(ports.hooks),
        GuardrailHook(ports.hooks, ports.guardrails, ports.guard, ports.managed_guard),
        CacheHook(ports.hooks, ports.cache),
        RateLimitHook(ports.hooks, ports.cache),
        WebhookHook(ports.hooks, ports.egress, ports.secrets),
        McpToolHook(ports.mcp),
        AnnotateHook(),
    ]
