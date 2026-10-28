"""The ``llm`` and ``agent`` kinds (design 8.4, 8.5; plan P2-02).

Both build the conversation from their ports (system prompt, trimmed history, the node's own
messages, the input), get their model from ``ModelFactory`` once per node, and read the
answer's fields like the Node.js runtime: the structured output, else the fields the node
declares (its ``outputSchema`` properties and ``outputStructure`` targets) from an answer that
is a JSON object.

* ``llm``: one model call, or, when the node has tools, a short agent loop of at most
  ``models.llmMaxTurns`` model calls. Structured output when ``responseFormat`` is ``json`` with
  an ``outputSchema``, or the platform's ``structuredOutput``. ``json`` without a schema (the
  builder's request parsers): the answer's JSON object is ``result`` and its fields, as the
  Node.js runtime reads it; an answer that is not one warns ``LLM_NOT_JSON``.
* ``agent``: the ``create_agent`` graph of the node through ``invoke_child``, with its tools,
  the handoff tool for ``handoffTarget``, the platform's routing tools and ``maxIterations``.
  A chosen handoff activates ``handoff-<target>``, else ``default``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool

from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.agents.errors import provider_errors
from zonic_runtime.engine.agents.handoff import HandoffTarget, handoff_tool, routing_tool
from zonic_runtime.engine.agents.messages import (
    conversation,
    read_visit,
    split_system,
    text_messages,
    trimmed_history,
    usage_of,
    visit_messages,
)
from zonic_runtime.engine.agents.structured import JSON_FORMAT, check, node_schema, strategy
from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.engine.state import keyed_values
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.dialects.refs import ModelRef, RoutingToolRef
from zonic_runtime.models.engine import NodeOutcome, StateUpdate
from zonic_runtime.models.io.node_io import NodeInput

#: Native structured output by JSON Schema (OpenAI and Azure OpenAI).
_NATIVE_METHOD = "json_schema"
_LIMIT_STOP = "limit"
TURN_LIMIT = "AGENT_TURN_LIMIT"
NOT_JSON = "LLM_NOT_JSON"


def _answer(text: str) -> Any:
    """The builder's ``result``: the parsed JSON when the text is JSON, else the text."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


class ModelKind(BaseKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
        self.spec, self.blueprint = spec, blueprint
        # Process-wide, built on the first visit: they hold no run data (rule 14).
        self._model: BaseChatModel | None = None
        self._agent: Any = None

    @staticmethod
    def agents(ctx: NodeContext) -> AgentServices:
        agents = ctx.services.agents
        if agents is None:
            raise ZonicError("NODE_TYPE_UNSUPPORTED", node_id=ctx.spec.node_id, reason="no agent services")
        return agents

    def _ref(self) -> ModelRef | None:
        raw = getattr(self.spec.config, "model", None)
        return ModelRef.model_validate(raw) if isinstance(raw, Mapping) else None

    def streams(self, ctx: NodeContext) -> bool:
        entry = ctx.services.resources.manifest.node_types.get(self.spec.node_type)
        manifest = entry.stream_tokens if entry is not None and entry.stream_tokens is not None else True
        return manifest and getattr(self.spec.config, "stream_response", None) is not False

    def model(self, ctx: NodeContext) -> BaseChatModel:
        if self._model is None:
            config = self.spec.config
            self._model = self.agents(ctx).models.create(
                self._ref(),
                temperature=getattr(config, "temperature", None),
                top_p=getattr(config, "top_p", None),
                max_tokens=getattr(config, "max_tokens", None),
                stream=self.streams(ctx),
            )
        return self._model

    def governor(self, ctx: NodeContext) -> Any:
        return self.agents(ctx).models.governor(self._ref())

    def native(self, ctx: NodeContext) -> bool:
        return self.agents(ctx).models.entry(self._ref()).supports_native_structured_output

    def messages(
        self, ctx: NodeContext, node_input: NodeInput, system_prompt: str | None = None
    ) -> list[BaseMessage]:
        system, own = split_system(text_messages(node_input.get("messages")))
        if system_prompt and system_prompt.strip():
            system = [system_prompt, *system]
        history = trimmed_history(text_messages(node_input.get("history")), ctx.services.settings.history)
        prompt = node_input.get("prompt")
        return conversation(system, history, own, prompt if isinstance(prompt, str) else None)

    def fields(self, text: str, structured: Any) -> dict[str, Any]:
        if isinstance(structured, Mapping):
            return dict(structured)
        config = self.spec.config
        own = config.output_schema if isinstance(config.output_schema, Mapping) else {}
        declared = set((own.get("properties") or {}) if isinstance(own.get("properties"), Mapping) else {})
        declared |= {
            e["target"]
            for e in config.output_structure or []
            if isinstance(e, Mapping) and isinstance(e.get("target"), str)
        }
        answer = _answer(text) if declared else None
        if not isinstance(answer, Mapping):
            return {}
        return {key: answer[key] for key in declared if key in answer}

    async def run_agent(
        self,
        ctx: NodeContext,
        messages: list[BaseMessage],
        tools: Sequence[BaseTool],
        schema: dict[str, Any] | None,
        max_iterations: int | None,
        extra_input: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[BaseMessage]]:
        if self._agent is None:
            self._agent = self.agents(ctx).factory.build(
                spec=self.spec,
                model=self.model(ctx),
                tools=tools,
                response_format=strategy(schema, self.native(ctx)) if schema else None,
                max_iterations=max_iterations,
                governor=self.governor(ctx),
            )
        ctx.run.tool_calls.start(self.spec.node_id, ctx.visit)  # an attempt counts its calls from 0
        visit_input = {
            "zonic_flow": keyed_values(ctx.state.get("flow")),
            "zonic_system": keyed_values(ctx.state.get("system")),
            "zonic_visit": ctx.visit,
        }
        async with provider_errors(self.spec.node_id):
            final = await invoke_child(
                self._agent,
                {"messages": messages, **visit_input, **(extra_input or {})},
                ctx.runtime,
                ctx.config,
            )
        return final, list(final["messages"][len(messages) :])

    @staticmethod
    def flow_updates(final: Mapping[str, Any]) -> list[StateUpdate]:
        """The ``flow`` changes the visit's tools made (MCP flow updates, case N-05)."""
        return [
            StateUpdate(path=f"flow.{key}", op="set", value=value)
            for key, value in (final.get("zonic_flow_updates") or {}).items()
        ]


class LlmKind(ModelKind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        messages = self.messages(ctx, node_input)
        schema = node_schema(self.spec.config, self.spec.name)
        tools = await self.agents(ctx).tools.tools(self.spec)
        structured: Any = None
        turn: list[Any] = []
        updates: list[StateUpdate] = []
        if tools:
            final, new = await self.run_agent(
                ctx, messages, tools, schema, ctx.services.settings.models.llm_max_turns
            )
            visit = read_visit(new)
            text, stop, usage = visit.text, visit.stop_reason, visit.usage
            if schema is not None:
                structured = check(final.get("structured_response"), schema, self.spec.node_id)
            turn = visit_messages(new, "", self.spec.node_id, ctx.visit)
            updates = self.flow_updates(final)
        else:
            governor = self.governor(ctx)
            async with governor.permit(count_tokens_approximately(messages)):
                raw, structured = await self._call(ctx, messages, schema)
            text = raw.text if raw.text or structured is None else json.dumps(structured, ensure_ascii=False)
            meta = raw.response_metadata or {}
            stop = meta.get("stop_reason") or meta.get("finish_reason")
            usage = usage_of([raw]) if isinstance(raw, AIMessage) else None
        warnings = []
        if schema is None and getattr(self.spec.config, "response_format", None) == JSON_FORMAT:
            parsed = _answer(text)
            if isinstance(parsed, Mapping):
                structured = dict(parsed)
            else:
                entry = ctx.services.resources.errors.errors.get(NOT_JSON)
                warnings.append(
                    {"code": NOT_JSON, "nodeId": self.spec.node_id, "message": entry.message if entry else ""}
                )
        output = {
            "text": text,
            "stopReason": stop,
            "structuredOutput": structured,
            "result": structured,
            "fields": self.fields(text, structured),
        }
        return NodeOutcome(
            output={k: v for k, v in output.items() if v is not None},
            activate=[ctx.services.resources.manifest.defaults.default_handle],
            usage=usage,
            messages=turn,
            updates=updates,
            warnings=warnings,
        )

    async def _call(
        self, ctx: NodeContext, messages: list[BaseMessage], schema: dict[str, Any] | None
    ) -> tuple[BaseMessage, Any]:
        """One model call; with a schema, its structured answer checked against it."""
        model = self.model(ctx)
        if schema is None:
            async with provider_errors(self.spec.node_id):
                return await model.ainvoke(messages, config=ctx.config), None
        options = {"method": _NATIVE_METHOD} if self.native(ctx) else {}
        async with provider_errors(self.spec.node_id):
            found = await model.with_structured_output(schema, include_raw=True, **options).ainvoke(
                messages, config=ctx.config
            )
        assert isinstance(found, dict)  # include_raw=True returns {raw, parsed, parsing_error}
        parsed = None if found.get("parsing_error") else found.get("parsed")
        return found["raw"], check(parsed, schema, self.spec.node_id)


class AgentKind(ModelKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
        super().__init__(spec, blueprint)
        config = spec.config
        self.targets = [
            HandoffTarget(node_id=t, name=blueprint.nodes[t].name)
            for t in getattr(config, "handoff_target", None) or []
            if t in blueprint.nodes
        ]
        self.routes = [
            ref
            for ref in (
                RoutingToolRef.model_validate(r) for r in getattr(config, "routing_tools", None) or []
            )
            if ref.enabled
        ]

    def handoff_tools(self) -> list[BaseTool]:
        tools = [handoff_tool(self.targets)] if self.targets else []
        for ref in self.routes:
            target = self.blueprint.nodes.get(ref.target)
            tools.append(routing_tool(ref, target.name if target is not None else ref.target))
        return tools

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        config = self.spec.config
        resolved = ctx.config_values()
        system_prompt = resolved.get("system_prompt")
        messages = self.messages(ctx, node_input, system_prompt if isinstance(system_prompt, str) else None)
        schema = node_schema(config, self.spec.name)
        tools: list[BaseTool] = []
        if self._agent is None:
            tools = [*await self.agents(ctx).tools.tools(self.spec), *self.handoff_tools()]
        presets = {ref.call_name: ctx.resolve(ref.parameters) for ref in self.routes if ref.parameters}
        final, new = await self.run_agent(
            ctx,
            messages,
            tools,
            schema,
            getattr(config, "max_iterations", None),
            {"zonic_presets": presets},
        )
        visit = read_visit(new)
        handoff = final.get("zonic_handoff") or None
        limited = bool(final.get("zonic_limited"))
        structured = final.get("structured_response")
        if schema is not None and handoff is None and not limited:
            structured = check(structured, schema, self.spec.node_id)
        defaults = ctx.services.resources.manifest.defaults
        warnings = []
        if limited:
            entry = ctx.services.resources.errors.errors.get(TURN_LIMIT)
            warnings.append(
                {"code": TURN_LIMIT, "nodeId": self.spec.node_id, "message": entry.message if entry else ""}
            )
        output = {
            "text": visit.text,
            "result": structured if structured is not None else _answer(visit.text),
            "structuredOutput": structured,
            "stopReason": _LIMIT_STOP if limited else visit.stop_reason,
            "toolCalls": visit.tool_calls,
            "toolResults": {**visit.tool_results, **(final.get("zonic_routed") or {})},
            "handoff": handoff["name"] if handoff else None,
            "fields": self.fields(visit.text, structured),
        }
        activate = (
            f"{defaults.handoff_handle_prefix}{handoff['target']}" if handoff else defaults.default_handle
        )
        return NodeOutcome(
            output={k: v for k, v in output.items() if v is not None},
            activate=[activate],
            usage=visit.usage,
            messages=visit_messages(new, visit.text, self.spec.node_id, ctx.visit),
            warnings=warnings,
            updates=self.flow_updates(final),
        )
