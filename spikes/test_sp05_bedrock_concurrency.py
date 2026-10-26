"""SP-5: ``ChatBedrockConverse`` async behavior under 150 concurrent streams with fake latency.

``ChatBedrockConverse`` (langchain-aws 1.9) implements only the synchronous ``_stream``;
``astream`` falls back to ``BaseChatModel._astream``, which runs every ``next()`` of the
boto event stream on the event loop's default executor. The spike measures what that
means for 150 concurrent streams, with a fake boto client whose stream blocks like the
network (first-token latency, then one event every ``CHUNK_S``):

* default executor (``min(32, cpu + 4)`` threads) versus an executor sized to the load;
* event-loop lag while the streams run;
* the ideal: the same latency on a natively async model (``ScriptedChatModel``).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from langchain_aws import ChatBedrockConverse
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn

STREAMS = 150
FIRST_TOKEN_S = 0.3
CHUNK_S = 0.02
CHUNKS = 20
IDEAL_S = FIRST_TOKEN_S + CHUNKS * CHUNK_S


class FakeBedrockRuntime:
    """The two boto calls ``ChatBedrockConverse`` makes, blocking like a real stream."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _enter(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def _leave(self) -> None:
        with self._lock:
            self.active -= 1

    def converse_stream(self, **_: Any) -> dict[str, Any]:
        def events() -> Any:
            self._enter()
            try:
                time.sleep(FIRST_TOKEN_S)
                yield {"messageStart": {"role": "assistant"}}
                for i in range(CHUNKS):
                    time.sleep(CHUNK_S)
                    yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": f"t{i} "}}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                yield {
                    "metadata": {
                        "usage": {"inputTokens": 5, "outputTokens": CHUNKS, "totalTokens": 25},
                        "metrics": {"latencyMs": 700},
                    }
                }
            finally:
                self._leave()

        return {"stream": events()}


async def _lag_probe(stop: asyncio.Event) -> float:
    worst = 0.0
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(0.01)
        worst = max(worst, time.perf_counter() - started - 0.01)
    return worst


async def _run(model: Any) -> tuple[float, float, list[str]]:
    stop = asyncio.Event()
    probe = asyncio.create_task(_lag_probe(stop))

    firsts: list[float] = []

    async def one() -> str:
        text = ""
        async for chunk in model.astream("hi"):
            if not text and chunk.text:
                firsts.append(time.perf_counter() - started)
            text += chunk.text
        return text

    started = time.perf_counter()
    texts = await asyncio.gather(*(one() for _ in range(STREAMS)))
    elapsed = time.perf_counter() - started
    stop.set()
    firsts.sort()
    p50, p95 = firsts[len(firsts) // 2], firsts[int(len(firsts) * 0.95)]
    print(f"\nSP-5 time to first token: p50 {p50:.2f}s p95 {p95:.2f}s", end="")
    return elapsed, await probe, texts


def _bedrock(client: FakeBedrockRuntime) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        model="anthropic.claude-sonnet-4-5-20250929-v1:0", client=client, region_name="us-east-1"
    )


@pytest.mark.parametrize("threads", [None, 64, 160])
async def test_bedrock_streams_on_the_default_executor(threads: int | None) -> None:
    loop = asyncio.get_running_loop()
    if threads:
        loop.set_default_executor(ThreadPoolExecutor(max_workers=threads, thread_name_prefix="models"))
    client = FakeBedrockRuntime()
    elapsed, lag, texts = await _run(_bedrock(client))
    label = f"{threads} threads" if threads else f"default ({min(32, (os.cpu_count() or 1) + 4)} threads)"
    print(
        f"\nSP-5 ChatBedrockConverse, executor {label}: {STREAMS} streams in {elapsed:.2f}s "
        f"(ideal {IDEAL_S:.2f}s), peak concurrent streams {client.peak}, worst loop lag {lag * 1000:.0f} ms"
    )
    assert all(t.startswith("t0 ") for t in texts)


async def test_native_async_baseline() -> None:
    model = ScriptedChatModel(
        script=[
            Turn(text=" ".join(f"t{i}" for i in range(CHUNKS)), delay=FIRST_TOKEN_S, chunk_delay=CHUNK_S)
        ],
        repeat_last=True,
    )
    elapsed, lag, _ = await _run(model)
    print(
        f"\nSP-5 native async baseline: {STREAMS} streams in {elapsed:.2f}s (ideal {IDEAL_S:.2f}s), "
        f"worst loop lag {lag * 1000:.0f} ms"
    )
