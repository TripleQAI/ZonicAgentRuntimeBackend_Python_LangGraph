"""The load tests' model: ``ScriptedChatModel`` timing, every text chunk stamped with the wall-clock
nanoseconds it was emitted at. A client on the same host reads the stamp of each SSE token frame's
last chunk, so ``arrival - stamp`` is the latency the runtime adds (design 11.1, "model chunk to
SSE write")."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk

from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn


class StampedChatModel(ScriptedChatModel):
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            if isinstance(chunk.message.content, str) and chunk.message.content:
                message = chunk.message.model_copy(update={"content": f"{time.time_ns()} "})
                chunk = ChatGenerationChunk(message=message)
            yield chunk


def stamped(tokens: int, first_token_seconds: float, tokens_per_second: float) -> StampedChatModel:
    """Every call answers ``tokens`` chunks: the first after ``first_token_seconds``, then one per
    ``1 / tokens_per_second``."""

    def answer(_: Any) -> Turn:
        return Turn.say("w " * tokens, delay=first_token_seconds, chunk_delay=1 / tokens_per_second)

    return StampedChatModel(script=[answer], repeat_last=True)


def stamps(text: str) -> list[int]:
    """The emission stamps in a token frame's text."""
    return [int(part) for part in text.split() if part.isdigit()]
