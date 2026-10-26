"""Self-tests of the core fakes (P0-11): ``ScriptedChatModel``, ``FakeClock``, ``SequentialIds``."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from tests.fakes.clock import FakeClock
from tests.fakes.ids import SequentialIds
from tests.fakes.scripted_chat_model import Request, ScriptedChatModel, ScriptExhaustedError, Turn


@tool
def lookup(city: str) -> str:
    """Look up the weather of a city."""
    return f"sunny in {city}"


class Verdict(BaseModel):
    approved: bool
    reason: str


async def test_text_turn_streams_words_and_aggregates_to_invoke() -> None:
    streamed = ScriptedChatModel(script=[Turn.say("Hello there, world.")])
    chunks = [c async for c in streamed.astream("hi")]
    texts = [c.text for c in chunks if c.text]
    assert texts == ["Hello ", "there, ", "world."]

    total = chunks[0]
    for c in chunks[1:]:
        total += c
    invoked = await ScriptedChatModel(script=[Turn.say("Hello there, world.")]).ainvoke("hi")
    assert isinstance(total, AIMessageChunk)
    assert total.text == invoked.text == "Hello there, world."
    assert total.usage_metadata == invoked.usage_metadata


async def test_explicit_chunks_and_response_metadata() -> None:
    model = ScriptedChatModel(
        script=[Turn(text="ab", chunks=("a", "b"), response_metadata={"stop_reason": "end"})]
    )
    chunks = [c async for c in model.astream("x")]
    assert [c.text for c in chunks if c.text] == ["a", "b"]
    assert chunks[-1].chunk_position == "last"
    assert chunks[-1].response_metadata["stop_reason"] == "end"
    assert chunks[-1].response_metadata["model_name"] == "scripted"


async def test_tool_calls_with_bound_tools_are_recorded() -> None:
    model = ScriptedChatModel(script=[Turn.calls(("lookup", {"city": "Oslo"}), ("lookup", {"city": "Rome"}))])
    message = await model.bind_tools([lookup], tool_choice="auto").ainvoke([HumanMessage("weather?")])
    assert [(c["name"], c["args"]) for c in message.tool_calls] == [
        ("lookup", {"city": "Oslo"}),
        ("lookup", {"city": "Rome"}),
    ]
    assert [c["id"] for c in message.tool_calls] == ["call_0", "call_1"]
    (request,) = model.requests
    assert request.tool_names == ["lookup"]
    assert request.tool_choice == "auto"
    assert request.last_text == "weather?"
    assert request.streamed is False


async def test_streamed_tool_call_chunks_aggregate_to_tool_calls() -> None:
    model = ScriptedChatModel(script=[Turn.call("lookup", {"city": "Oslo"}, id="t1")])
    total: Any = None
    async for chunk in model.bind_tools([lookup]).astream("weather?"):
        total = chunk if total is None else total + chunk
    assert total.tool_calls == [{"name": "lookup", "args": {"city": "Oslo"}, "id": "t1", "type": "tool_call"}]
    assert model.requests[0].streamed is True


async def test_structured_output_through_with_structured_output() -> None:
    model = ScriptedChatModel(script=[Turn.structured(Verdict, {"approved": True, "reason": "fine"})])
    verdict = await model.with_structured_output(Verdict).ainvoke("judge")
    assert verdict == Verdict(approved=True, reason="fine")
    assert model.requests[0].tool_names == ["Verdict"]


async def test_error_before_the_first_chunk() -> None:
    model = ScriptedChatModel(script=[Turn.fail(TimeoutError("throttled"))])
    with pytest.raises(TimeoutError, match="throttled"):
        await model.ainvoke("x")
    with pytest.raises(ScriptExhaustedError):
        await model.ainvoke("x")


async def test_error_in_the_middle_of_a_stream() -> None:
    model = ScriptedChatModel(
        script=[Turn.fail(ConnectionError("dropped"), after_chunks=2, text="one two three")]
    )
    seen: list[str] = []
    with pytest.raises(ConnectionError, match="dropped"):
        async for chunk in model.astream("x"):
            seen.append(chunk.text)
    assert seen == ["one ", "two "]


async def test_delays_hold_the_first_chunk_and_space_the_rest() -> None:
    model = ScriptedChatModel(script=[Turn(text="a b c", delay=0.05, chunk_delay=0.02)])
    started = time.perf_counter()
    stamps = [time.perf_counter() - started async for _ in model.astream("x")]
    assert stamps[0] >= 0.05
    assert stamps[-1] >= 0.05 + 3 * 0.02


async def test_usage_explicit_and_counted() -> None:
    model = ScriptedChatModel(
        script=[
            Turn.say("four words right here", usage={"input_tokens": 7, "output_tokens": 3}),
            Turn.say("two words"),
        ]
    )
    first = await model.ainvoke("ignored")
    second = await model.ainvoke([SystemMessage("be brief"), HumanMessage("three more words")])
    assert first.usage_metadata == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    assert second.usage_metadata == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}


async def test_callable_turn_sees_the_request() -> None:
    def echo(request: Request) -> Turn:
        return Turn.say(f"you said {request.last_text}")

    model = ScriptedChatModel(script=[echo, echo])
    assert (await model.ainvoke("ping")).text == "you said ping"
    assert model.invoke("pong").text == "you said pong"


async def test_script_exhausted_and_repeat_last() -> None:
    model = ScriptedChatModel(script=[Turn.say("only")])
    await model.ainvoke("x")
    with pytest.raises(ScriptExhaustedError, match="2 times; the script has 1 turns"):
        await model.ainvoke("x")
    repeating = ScriptedChatModel(script=[Turn.say("again")], repeat_last=True)
    assert [(await repeating.ainvoke("x")).text for _ in range(3)] == ["again"] * 3
    assert repeating.remaining == 0


def test_sync_invoke_and_stream() -> None:
    model = ScriptedChatModel(script=[Turn.say("sync one"), Turn.say("sync two")])
    assert model.invoke("x").text == "sync one"
    assert [c.text for c in model.stream("x") if c.text] == ["sync ", "two"]


async def test_token_callbacks_fire_once_per_chunk() -> None:
    model = ScriptedChatModel(script=[Turn.say("a b c")])
    tokens = [
        event["data"]["chunk"].text
        async for event in model.astream_events("x", version="v2")
        if event["event"] == "on_chat_model_stream" and event["data"]["chunk"].text
    ]
    assert tokens == ["a ", "b ", "c"]


async def test_concurrent_calls_consume_the_script_once_each() -> None:
    model = ScriptedChatModel(script=[Turn.say(str(i)) for i in range(50)])
    answers = await asyncio.gather(*(model.ainvoke("x") for _ in range(50)))
    assert sorted(int(a.text) for a in answers) == list(range(50))
    assert len(model.requests) == 50


async def test_drives_a_create_agent_tool_loop() -> None:
    model = ScriptedChatModel(
        script=[Turn.call("lookup", {"city": "Oslo"}), Turn.say("It is sunny in Oslo.")]
    )
    agent = create_agent(model, tools=[lookup])
    result = await agent.ainvoke({"messages": [HumanMessage("weather in Oslo?")]})
    assert result["messages"][-1].text == "It is sunny in Oslo."
    tool_results = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.text for m in tool_results] == ["sunny in Oslo"]
    second = model.requests[1]
    assert isinstance(second.messages[-1], ToolMessage)
    assert second.tool_names == ["lookup"]


async def test_fake_clock_moves_only_when_told() -> None:
    clock = FakeClock()
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    await clock.sleep(30)
    clock.advance(0.5)
    assert clock.monotonic() == 30.5
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 30, 500000, tzinfo=UTC)
    assert clock.sleeps == [30]
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(start=datetime(2026, 1, 1))


def test_sequential_ids_are_deterministic_and_kind_separated() -> None:
    a, b = SequentialIds(), SequentialIds()
    runs = [a.uuid("run") for _ in range(2)]
    assert runs == [b.uuid("run"), b.uuid("run")]
    assert str(runs[1]).endswith("-0000-4000-8000-000000000002")
    assert a.uuid("session") not in runs
    assert len(a.trace_id()) == 32 and len(a.span_id()) == 16
    assert int(a.trace_id(), 16) and a.count("trace") == 2
