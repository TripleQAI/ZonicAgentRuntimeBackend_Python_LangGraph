"""Messages in and out of LLM and agent nodes (design 8.5; plan P2-02, case B-03).

In: the resolved ``messages`` and ``history`` ports. A message whose content resolved to a list
of messages (``{{thread.messages}}``, ``{{messages}}`` as the whole content) expands into them;
any other content becomes text. ``system`` entries become the system prompt. History is cut to
``history.maxMessages``, then trimmed to ``history.maxTokens`` with ``trim_messages``.

Out: what an agent visit produced, read from the new messages of its final state: the answer
(the last model text), tool calls with their status, tool results by tool name (the last
successful call of each name), usage, and the visit's ``turn_messages``: the answer for the chat,
tool calls, tool results and intermediate turns as ``internal``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately

from zonic_runtime.engine.patches import thread_message
from zonic_runtime.models.engine import MessageRole, ThreadMessage, Usage
from zonic_runtime.models.resources.catalogs import ModelCatalog
from zonic_runtime.models.resources.settings import HistorySettings

_SYSTEM, _USER, _ASSISTANT = "system", "user", "assistant"
_ERROR_STATUS = "error"
_TOOL_ROLE: MessageRole = "tool"  # principle: allow rule-1-node-type message-role


@dataclass(frozen=True)
class ChatText:
    role: str
    content: str


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "" if content is None else json.dumps(content, ensure_ascii=False, default=str)


def _is_message(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get("role"), str)


def text_messages(value: Any) -> list[ChatText]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    found: list[ChatText] = []
    for message in value:
        if not _is_message(message):
            continue
        content = message.get("content")
        if isinstance(content, list) and content and all(_is_message(m) for m in content):
            found += text_messages(content)
        else:
            found.append(ChatText(role=message["role"], content=_text(content)))
    return found


def split_system(messages: Sequence[ChatText]) -> tuple[list[str], list[ChatText]]:
    system = [m.content for m in messages if m.role == _SYSTEM and m.content.strip()]
    return system, [m for m in messages if m.role != _SYSTEM]


def to_langchain(messages: Sequence[ChatText]) -> list[BaseMessage]:
    converted: list[BaseMessage] = []
    for message in messages:
        if message.role == _ASSISTANT:
            converted.append(AIMessage(message.content))
        elif message.role == _SYSTEM:
            converted.append(SystemMessage(message.content))
        else:
            converted.append(HumanMessage(message.content))
    return converted


def trimmed_history(history: Sequence[ChatText], settings: HistorySettings) -> list[BaseMessage]:
    recent = to_langchain(history[-settings.max_messages :])
    if not recent:
        return []
    kept = trim_messages(
        recent,
        max_tokens=settings.max_tokens,
        token_counter=count_tokens_approximately,
        strategy="last",
        start_on="human",
        allow_partial=False,
    )
    return list(kept)


def conversation(
    system: Sequence[str], history: Sequence[BaseMessage], messages: Sequence[ChatText], prompt: str | None
) -> list[BaseMessage]:
    """System prompt, history, the node's own messages, then the input: the last user message
    of the node's messages when it ends with one, else the ``prompt`` port."""
    own = list(messages)
    if own and own[-1].role == _USER:
        prompt, own = own[-1].content, own[:-1]
    found: list[BaseMessage] = [SystemMessage("\n\n".join(system))] if system else []
    found += [*history, *to_langchain(own)]
    if prompt:
        found.append(HumanMessage(prompt))
    return found


def response_model(message: AIMessage) -> str | None:
    """The model a provider says answered (``model_name`` for OpenAI-compatible APIs)."""
    meta = message.response_metadata or {}
    found = meta.get("model_name") or meta.get("model") or meta.get("model_id")
    return found if isinstance(found, str) else None


def usage_of(messages: Sequence[BaseMessage], prices: ModelCatalog | None = None) -> Usage | None:
    """Token usage of the model responses among ``messages``, costed from the catalog's prices
    when ``prices`` is given and the answering model has one (cached prompt tokens at the cached
    price). A response without a price adds no cost."""
    total = Usage()
    seen = False
    for message in messages:
        meta = getattr(message, "usage_metadata", None)
        if isinstance(message, AIMessage) and meta:
            seen = True
            inputs, outputs = int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))
            price = prices.price_for(response_model(message)) if prices is not None else None
            cost = None
            if price is not None:
                cached = int((meta.get("input_token_details") or {}).get("cache_read") or 0)
                cost = price.cost(max(inputs - cached, 0), outputs, cached)["total"]
            total = total + Usage(
                input_tokens=inputs,
                output_tokens=outputs,
                total_tokens=int(meta.get("total_tokens", 0)),
                model_calls=1,
                cost=cost,
            )
    return total if seen else None


def _result_value(content: Any) -> Any:
    text = (
        _text(content)
        if not isinstance(content, list)
        else "".join(b.get("text", "") if isinstance(b, Mapping) else str(b) for b in content)
    )
    try:
        return json.loads(text)
    except ValueError:
        return text


@dataclass
class AgentVisit:
    text: str = ""
    stop_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: dict[str, Any] = field(default_factory=dict)
    usage: Usage | None = None


def read_visit(new: Sequence[BaseMessage], prices: ModelCatalog | None = None) -> AgentVisit:
    visit = AgentVisit(usage=usage_of(new, prices))
    calls: dict[str, dict[str, Any]] = {}
    for message in new:
        if isinstance(message, AIMessage):
            if message.text:
                visit.text = message.text
            meta = message.response_metadata or {}
            visit.stop_reason = meta.get("stop_reason") or meta.get("finish_reason") or visit.stop_reason
            for call in message.tool_calls:
                record: dict[str, Any] = {
                    "toolUseId": call.get("id") or "",
                    "name": call["name"],
                    "input": call["args"],
                    "status": "success",
                }
                calls[record["toolUseId"]] = record
                visit.tool_calls.append(record)
        elif isinstance(message, ToolMessage):
            found = calls.get(message.tool_call_id)
            failed = message.status == _ERROR_STATUS
            if found is not None and failed:
                found["status"] = _ERROR_STATUS
            name = message.name or (found or {}).get("name")
            if name and not failed:
                visit.tool_results[name] = _result_value(message.content)
    return visit


def visit_messages(new: Sequence[BaseMessage], answer: str, node_id: str, visit: int) -> list[ThreadMessage]:
    """The visit's ``turn_messages``: intermediate model turns, tool calls and results are
    ``internal``; the final answer is the chat's."""
    found: list[ThreadMessage] = []
    final = next((m for m in reversed(new) if isinstance(m, AIMessage) and m.text and not m.tool_calls), None)
    for message in new:
        if isinstance(message, AIMessage):
            if message is final:
                continue
            if message.tool_calls:
                found.append(
                    thread_message(
                        "assistant",
                        "tool_call",
                        message.tool_calls,
                        node_id=node_id,
                        visit=visit,
                        visibility="internal",
                    )
                )
            elif message.text:
                found.append(
                    thread_message(
                        "assistant",
                        "agent_turn",
                        message.text,
                        node_id=node_id,
                        visit=visit,
                        visibility="internal",
                    )
                )
        elif isinstance(message, ToolMessage):
            found.append(
                thread_message(
                    _TOOL_ROLE,
                    "tool_result",
                    _result_value(message.content),
                    node_id=node_id,
                    visit=visit,
                    visibility="internal",
                )
            )
    if answer:
        found.append(thread_message("assistant", "answer", answer, node_id=node_id, visit=visit))
    return found
