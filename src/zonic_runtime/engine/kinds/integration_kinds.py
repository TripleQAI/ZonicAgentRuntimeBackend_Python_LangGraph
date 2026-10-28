"""The ``tool``, ``external_agent`` and ``guardrail`` kinds (design 7.5; plan P3-06, cases N-10, N-15,
N-16, B-05), after the Node.js runtime's ``tool-node.ts``, ``external-agent-node.ts`` and
``guardrail-node.ts``.

* ``tool``: one configured tool invoked directly, without a model, built by the same tool factory
  as agents' tools and called with the node's resolved ``parameters`` and ``inputs``. Its result is
  the output; with ``pass`` and ``fail`` edges, success activates ``pass`` (and the default edges),
  a tool error ``fail``.
* ``external_agent``: an agent endpoint called with the mapped input through the egress guard: A2A
  ``message/send`` over JSON-RPC by default, or the input posted as JSON (``protocol: http``). The
  credential is a secret reference resolved at call time; a ``5xx`` answer or no answer is retried
  up to ``retries`` times. The answer is the text, with ``result`` the parsed JSON when it is JSON.
* ``guardrail``: the content is checked against the node's ``policy`` by the policy model (or a
  provisioned Bedrock guardrail, ``guardrailId``). Compliant content activates ``pass``; other
  content ``fail``, or with ``action: warn`` passes with a ``guardrail.flagged`` warning. Blocked
  content without a ``fail`` edge fails the node with ``GUARDRAIL_BLOCKED``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import ToolException
from langgraph.types import Command

from zonic_runtime.engine.agents.errors import provider_errors
from zonic_runtime.engine.agents.messages import usage_of
from zonic_runtime.engine.agents.policy_guard import decide, decision_messages, policy_ref
from zonic_runtime.engine.kinds.flow_kinds import _Kind
from zonic_runtime.engine.kinds.io_kinds import mapping_record
from zonic_runtime.engine.kinds.model_kinds import ModelKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.engine.state import keyed_values
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.dialects.refs import ModelRef, ToolRef
from zonic_runtime.models.engine import NodeOutcome, StateUpdate, Usage
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.ports.egress import EgressRequest, EgressResponse
from zonic_runtime.ports.guardrail import GuardrailDecision
from zonic_runtime.ports.secrets import resolve_secrets

PASS, FAIL = "pass", "fail"
TOOL_FAILED, AGENT_ERROR, BLOCKED = "TOOL_FAILED", "EXTERNAL_AGENT_ERROR", "GUARDRAIL_BLOCKED"
FLAGGED = "guardrail.flagged"
_UPSTREAM = "UPSTREAM_5XX"
_REASON_CHARS = 500
#: The fields a message's text may be in, in order; else the whole input as JSON.
_TEXT_FIELDS = ("text", "message", "question", "query", "input", "prompt")
_HTTP = "http"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else json.dumps(value, ensure_ascii=False)


def _parsed(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def tool_name_of(config: Any, fallback: str) -> str:
    """The MCP name of a platform tool node's tool: ``toolName``, else its ``toolId`` without the
    ``mcp-<server>-`` prefix."""
    name, tool_id, server = (getattr(config, k, None) for k in ("tool_name", "tool_id", "server_id"))
    if name:
        return str(name)
    if not tool_id:
        return fallback
    prefix = f"mcp-{server or ''}-"
    return str(tool_id)[len(prefix) :] if server and str(tool_id).startswith(prefix) else str(tool_id)


def _reply(found: Any) -> tuple[Any, str | None, dict[str, Any]]:
    """A factory tool's answer: its value, the error the model would see, and its flow updates."""
    if isinstance(found, Command):
        update = found.update if isinstance(found.update, Mapping) else {}
        messages = [m for m in update.get("messages") or [] if isinstance(m, ToolMessage)]
        flow = dict(update.get("zonic_flow_updates") or {})
        if messages and messages[0].status == "error":
            return None, str(messages[0].content).removeprefix("error: "), flow
        return (_parsed(str(messages[0].content)) if messages else None), None, flow
    # A server tool answers with its text content: JSON is the value it holds (``mcpContent``).
    return (_parsed(found) if isinstance(found, str) else found), None, {}


class ToolKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        node_id, config = self.spec.node_id, self.spec.config
        agents = ctx.services.agents
        if agents is None:
            raise ZonicError("NODE_TYPE_UNSUPPORTED", node_id=node_id, reason="no agent services")
        ref = ToolRef(
            tool_type=str(getattr(config, "tool_type", None) or "mcp"),
            name=tool_name_of(config, self.spec.name),
            tool_id=getattr(config, "tool_id", None),
            server_id=getattr(config, "server_id", None),
            title=getattr(config, "title", None),
            settings=dict(getattr(config, "settings", None) or {}),
            schema_=getattr(config, "schema_", None),
        )
        tool = agents.tools.create(self.spec, ref)
        parameters = node_input.get("parameters")
        args = {
            **(dict(parameters) if isinstance(parameters, Mapping) else {}),
            **mapping_record(node_input.get("inputs")),
        }
        call_id = f"{node_id}:{ctx.visit}:{uuid.uuid4().hex[:8]}"
        ctx.run.tool_calls.start(node_id, ctx.visit)
        ctx.run.tool_calls.assign(node_id, ctx.visit, call_id)
        runtime = ToolRuntime(
            state={
                "zonic_flow": keyed_values(ctx.state.get("flow")),
                "zonic_system": keyed_values(ctx.state.get("system")),
                "zonic_visit": ctx.visit,
            },
            context=ctx.run,
            config=ctx.config,
            stream_writer=lambda _: None,
            tool_call_id=call_id,
            store=None,
        )
        handles = self.blueprint.edge_index.handles(node_id)
        try:
            value, failure, flow = _reply(await tool.coroutine(runtime, **args))  # type: ignore[attr-defined]
        except (ToolException, ZonicError) as error:
            value, failure, flow = None, str(error) if isinstance(error, ToolException) else error.code, {}
            if isinstance(error, ZonicError) and FAIL not in handles:
                raise
        updates = [StateUpdate(path=f"flow.{k}", op="set", value=v) for k, v in flow.items()]
        if failure is not None:
            if FAIL not in handles:
                raise ZonicError(TOOL_FAILED, node_id=node_id, reason=failure[:_REASON_CHARS])
            return NodeOutcome(output={"result": {"error": failure}}, activate=[FAIL], updates=updates)
        result = dict(value) if isinstance(value, Mapping) else {"value": value}
        # ``pass`` with the default edges, those of them that are connected.
        activate = (
            [h for h in (*self.default(ctx), PASS) if h in handles] if PASS in handles else self.default(ctx)
        )
        return NodeOutcome(output={"result": result}, activate=activate, updates=updates)


class ExternalAgentKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        node_id, config = self.spec.node_id, self.spec.config
        egress = ctx.services.ports.egress
        if egress is None:
            raise ZonicError("RESOURCE_MISSING", resource="egress", node_id=node_id)
        endpoint = _text(ctx.resolve(getattr(config, "endpoint", None))).strip()
        if not endpoint:
            raise ZonicError("INPUT_PORT_MISSING", node_id=node_id, port="endpoint")
        given = mapping_record(node_input.get("input"))
        headers = [("content-type", "application/json"), ("accept", "application/json")]
        api_key = getattr(config, "api_key", None)
        if api_key:
            secrets = ctx.run.ports.secrets or ctx.services.ports.secrets
            key = str(resolve_secrets(api_key, secrets.get if secrets is not None else (lambda _: None)))
            headers.append(
                ("authorization", f"Bearer {key}")
                if getattr(config, "auth_type", None) == "bearer"
                else ("x-api-key", key)
            )
        field = next((f for f in _TEXT_FIELDS if isinstance(given.get(f), str)), None)
        text = str(given[field]) if field else json.dumps(given, ensure_ascii=False)
        plain = getattr(config, "protocol", None) == _HTTP
        body = (
            json.dumps(given, ensure_ascii=False)
            if plain
            else json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": f"{ctx.run.run.run_id}:{node_id}:{ctx.visit}",
                    "method": "message/send",
                    "params": {
                        "message": {
                            "kind": "message",
                            "role": "user",
                            "messageId": str(uuid.uuid4()),
                            "parts": [{"kind": "text", "text": text}],
                            "metadata": {"input": given},
                        }
                    },
                },
                ensure_ascii=False,
            )
        )
        request = EgressRequest(
            method="POST",
            url=endpoint,
            headers=tuple(headers),
            body=body,
            timeout_seconds=self.spec.timeout_seconds or None,
        )
        response = await self._send(ctx, egress, request, int(getattr(config, "retries", None) or 0))
        if response.status >= 400:
            raise ZonicError(
                AGENT_ERROR, node_id=node_id, status=response.status, reason=response.text()[:_REASON_CHARS]
            )
        content = response.text()
        parsed = _parsed(content) if content else None
        if plain:
            answer = content if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)
        else:
            if not isinstance(parsed, Mapping):
                raise ZonicError(AGENT_ERROR, node_id=node_id, reason="the answer is not JSON-RPC")
            error = parsed.get("error")
            if isinstance(error, Mapping):
                raise ZonicError(
                    AGENT_ERROR,
                    node_id=node_id,
                    reason=f"{error.get('code', '')} {error.get('message', '')}".strip(),
                )
            answer = a2a_text(parsed.get("result"))
        result = _parsed(answer)
        return NodeOutcome(
            output={
                "text": answer,
                "result": result,
                "fields": dict(result) if isinstance(result, Mapping) else {},
            },
            activate=self.default(ctx),
        )

    async def _send(
        self, ctx: NodeContext, egress: Any, request: EgressRequest, retries: int
    ) -> EgressResponse:
        backoff = ctx.services.resources.settings.external_agents.retry_backoff_milliseconds / 1000
        for attempt in range(retries + 1):
            try:
                found: EgressResponse = await egress.send(request)
                return found
            except ZonicError as error:
                if error.code != _UPSTREAM or attempt >= retries:
                    if error.code in (_UPSTREAM, "TIMEOUT"):
                        raise ZonicError(AGENT_ERROR, node_id=self.spec.node_id, reason=error.code) from error
                    raise
            await asyncio.sleep(backoff * (attempt + 1))
        raise ZonicError(AGENT_ERROR, node_id=self.spec.node_id, reason="no answer")


def _parts_text(parts: Any) -> str:
    texts = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, Mapping):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif part.get("data") is not None:
            texts.append(json.dumps(part["data"], ensure_ascii=False))
    return "\n".join(t for t in texts if t)


def a2a_text(result: Any) -> str:
    """The answer of an A2A result: a message's parts, or a task's artifacts or status message."""
    if not isinstance(result, Mapping):
        return ""
    if isinstance(result.get("parts"), list):
        return _parts_text(result["parts"])
    artifacts = "\n".join(
        t
        for t in (
            _parts_text(a.get("parts")) for a in result.get("artifacts") or [] if isinstance(a, Mapping)
        )
        if t
    )
    if artifacts:
        return artifacts
    status = result.get("status") if isinstance(result.get("status"), Mapping) else {}
    message = status.get("message") if isinstance(status, Mapping) else None
    return _parts_text(message.get("parts")) if isinstance(message, Mapping) else ""


class GuardrailKind(ModelKind):
    def _ref(self) -> ModelRef | None:
        return self._policy_ref

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        node_id, config = self.spec.node_id, self.spec.config
        settings = ctx.services.resources.settings.guardrails
        self._policy_ref = policy_ref(settings)
        content = mapping_record(node_input.get("content"))
        values = list(content.values())
        text = _text(values[0]) if len(values) == 1 else json.dumps(content, ensure_ascii=False)
        usage: Usage | None = None
        guardrail_id = getattr(config, "guardrail_id", None)
        if guardrail_id:
            managed = ctx.services.ports.guardrails
            if managed is None:
                raise ZonicError("FEATURE_DISABLED", feature="bedrock-guardrails", node_id=node_id)
            applied = await managed.apply(
                guardrail_id,
                getattr(config, "guardrail_version", None) or settings.default_version,
                "INPUT",
                text,
            )
            decision = GuardrailDecision(not applied.intervened, applied.reason, list(applied.categories))
        else:
            messages = decision_messages(
                settings, getattr(config, "policy", None) or settings.default_policy, text
            )
            governor = self.agents(ctx).models.governor(self._ref())
            async with governor.permit(count_tokens_approximately(messages)), provider_errors(node_id):
                decision, raw = await decide(self.model(ctx), self.native(ctx), messages, ctx.config, node_id)
            usage = usage_of([raw]) if isinstance(raw, AIMessage) else None
        findings = (
            [] if decision.allowed else [{"reason": decision.reason, "categories": decision.categories}]
        )
        handle = PASS if decision.allowed else FAIL
        warnings: list[dict[str, Any]] = []
        if not decision.allowed and getattr(config, "action", None) == "warn":
            warnings.append(
                {"code": FLAGGED, "message": decision.reason or "the content was flagged", "nodeId": node_id}
            )
            handle = PASS
        if handle == FAIL and FAIL not in self.blueprint.edge_index.handles(node_id):
            raise ZonicError(BLOCKED, node_id=node_id, findings=findings)
        return NodeOutcome(
            output={"handle": handle, "passed": handle == PASS, "findings": findings},
            activate=[handle],
            usage=usage,
            warnings=warnings,
        )

    _policy_ref: ModelRef | None = None
