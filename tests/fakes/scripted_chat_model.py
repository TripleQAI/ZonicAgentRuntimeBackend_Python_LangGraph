"""``ScriptedChatModel``: a LangChain chat model that plays back scripted turns (P0-11).

Each model call consumes the next ``Turn``: text streamed as chunks, tool calls,
a structured output (a tool call named after the schema, which is how
``with_structured_output`` works on a model with ``bind_tools``), an error before
or in the middle of the stream, delays and usage. A turn may also be a function of
the ``Request``, for responses that depend on what the model was sent.

``invoke``/``ainvoke`` and ``stream``/``astream`` are built from the same chunks, so
a streamed turn aggregates to exactly the message a non-streamed call returns.
Token callbacks are left to ``BaseChatModel`` (it fires them per chunk). Every call
is recorded as a ``Request`` with its messages, bound tools and options.

The script is shared by every binding of the model (``bind_tools``, ``bind``,
``with_structured_output``), so one script drives an agent's whole loop.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.language_models.chat_models import agenerate_from_stream, generate_from_stream
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, PrivateAttr

_WORDS = re.compile(r"\s*\S+\s*|\s+")


class ScriptExhaustedError(AssertionError):
    """The model was called more times than the script has turns."""


@dataclass(frozen=True)
class ScriptedToolCall:
    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class Turn:
    """One model response."""

    text: str = ""
    #: Explicit stream chunks; by default the text is split into words with their whitespace.
    chunks: tuple[str, ...] | None = None
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    #: Raised instead of (``error_after_chunks == 0``) or in the middle of the response.
    error: BaseException | None = None
    error_after_chunks: int = 0
    #: Seconds before the first chunk, and between chunks.
    delay: float = 0.0
    chunk_delay: float = 0.0
    #: ``input_tokens`` and ``output_tokens``; counted from words when omitted.
    usage: dict[str, int] | None = None
    response_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def say(cls, text: str, **kwargs: Any) -> Turn:
        return cls(text=text, **kwargs)

    @classmethod
    def call(
        cls, name: str, args: dict[str, Any] | None = None, *, id: str | None = None, **kwargs: Any
    ) -> Turn:
        return cls(tool_calls=(ScriptedToolCall(name, args or {}, id),), **kwargs)

    @classmethod
    def calls(cls, *calls: tuple[str, dict[str, Any]], **kwargs: Any) -> Turn:
        return cls(tool_calls=tuple(ScriptedToolCall(name, args) for name, args in calls), **kwargs)

    @classmethod
    def structured(cls, schema: type | str, values: dict[str, Any], **kwargs: Any) -> Turn:
        name = schema if isinstance(schema, str) else schema.__name__
        return cls.call(name, values, **kwargs)

    @classmethod
    def fail(cls, error: BaseException, *, after_chunks: int = 0, text: str = "", **kwargs: Any) -> Turn:
        return cls(text=text, error=error, error_after_chunks=after_chunks, **kwargs)

    def text_chunks(self) -> list[str]:
        if self.chunks is not None:
            return list(self.chunks)
        return _WORDS.findall(self.text) if self.text else []


@dataclass(frozen=True)
class Request:
    """What one model call was sent."""

    messages: list[BaseMessage]
    tools: list[dict[str, Any]]
    tool_choice: Any
    stop: list[str] | None
    options: dict[str, Any]
    streamed: bool

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]

    @property
    def last_text(self) -> str:
        return self.messages[-1].text if self.messages else ""


Step = Turn | Callable[[Request], Turn]


class ScriptedChatModel(BaseChatModel):
    """Plays back ``script`` one turn per call; see the module docstring."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[Step]
    model_name: str = "scripted"
    #: Repeat the last turn once the script is used up, instead of failing.
    repeat_last: bool = False

    #: The next turn, in a list so that copies (a model tagged ``nostream``, see ScriptedModels)
    #: share one position with the original, as the requests and the lock are shared.
    _cursor: list[int] = PrivateAttr(default_factory=lambda: [0])
    _requests: list[Request] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    @property
    def requests(self) -> list[Request]:
        return list(self._requests)

    @property
    def remaining(self) -> int:
        return len(self.script) - self._cursor[0]

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    # -- playback ----------------------------------------------------------------------

    def _next(
        self, messages: list[BaseMessage], stop: list[str] | None, streamed: bool, kwargs: dict[str, Any]
    ) -> Turn:
        options = dict(kwargs)
        request = Request(
            messages=list(messages),
            tools=list(options.pop("tools", None) or []),
            tool_choice=options.pop("tool_choice", None),
            stop=stop,
            options=options,
            streamed=streamed,
        )
        with self._lock:
            self._requests.append(request)
            if self._cursor[0] < len(self.script):
                step = self.script[self._cursor[0]]
                self._cursor[0] += 1
            elif self.repeat_last and self.script:
                step = self.script[-1]
            else:
                raise ScriptExhaustedError(
                    f"ScriptedChatModel called {len(self._requests)} times; the script has {len(self.script)} turns"
                )
        return step(request) if callable(step) else step

    def _usage(self, turn: Turn, messages: list[BaseMessage]) -> UsageMetadata:
        if turn.usage is not None:
            inp, out = turn.usage.get("input_tokens", 0), turn.usage.get("output_tokens", 0)
        else:
            inp = sum(len(m.text.split()) for m in messages)
            out = len(turn.text.split()) + sum(len(json.dumps(c.args).split()) for c in turn.tool_calls)
        return UsageMetadata(input_tokens=inp, output_tokens=out, total_tokens=inp + out)

    def _chunks(self, turn: Turn, messages: list[BaseMessage]) -> list[ChatGenerationChunk]:
        metadata = {"model_name": self.model_name, **turn.response_metadata}
        chunks = [ChatGenerationChunk(message=AIMessageChunk(content=text)) for text in turn.text_chunks()]
        for index, call in enumerate(turn.tool_calls):
            chunk = tool_call_chunk(
                name=call.name, args=json.dumps(call.args), id=call.id or f"call_{index}", index=index
            )
            chunks.append(ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[chunk])))
        chunks.append(
            ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    usage_metadata=self._usage(turn, messages),
                    response_metadata=metadata,
                    chunk_position="last",
                )
            )
        )
        return chunks

    def _failure_point(self, turn: Turn) -> int | None:
        return None if turn.error is None else turn.error_after_chunks

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        turn = self._next(messages, stop, kwargs.pop("_scripted_streamed", True), kwargs)
        fail_at = self._failure_point(turn)
        if turn.delay:
            time.sleep(turn.delay)
        for position, chunk in enumerate(self._chunks(turn, messages)):
            if fail_at is not None and position == fail_at:
                raise turn.error  # type: ignore[misc]
            if position and turn.chunk_delay:
                time.sleep(turn.chunk_delay)
            yield chunk
        if fail_at is not None:
            raise turn.error  # type: ignore[misc]

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        turn = self._next(messages, stop, kwargs.pop("_scripted_streamed", True), kwargs)
        fail_at = self._failure_point(turn)
        if turn.delay:
            await asyncio.sleep(turn.delay)
        for position, chunk in enumerate(self._chunks(turn, messages)):
            if fail_at is not None and position == fail_at:
                raise turn.error  # type: ignore[misc]
            if position and turn.chunk_delay:
                await asyncio.sleep(turn.chunk_delay)
            yield chunk
        if fail_at is not None:
            raise turn.error  # type: ignore[misc]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return generate_from_stream(self._stream(messages, stop, _scripted_streamed=False, **kwargs))

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await agenerate_from_stream(self._astream(messages, stop, _scripted_streamed=False, **kwargs))
