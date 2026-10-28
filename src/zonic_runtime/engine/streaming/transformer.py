"""``RunEventTransformer`` on ``astream_events(version="v3")`` (design Section 10; plan P2-01).

The transformer is a ``StreamTransformer`` registered per run. It maps each protocol event
with the run's ``EventMapper`` and pushes the drafts to its ``zonic`` projection, which the
source drains. The channel is local (unnamed): the drafts reach only the driver, never
LangGraph's main event log. ``CustomTransformer`` and ``ToolCallTransformer`` are passed to
every call, because without them there are no ``custom`` or ``tools`` events (SP-3).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.prebuilt import ToolCallTransformer
from langgraph.runtime import RunControl
from langgraph.stream import CustomTransformer, ProtocolEvent, StreamChannel, StreamTransformer
from langgraph.types import Durability

from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.streaming.mapper import CUSTOM, MESSAGES, TOOLS, EventMapper
from zonic_runtime.engine.streaming.source import Emit, StreamedRun
from zonic_runtime.models.events import EventDraft

#: The projection key of the run's event drafts.
ZONIC = "zonic"


class RunEventTransformer(StreamTransformer):
    required_stream_modes = (MESSAGES, TOOLS, CUSTOM)

    def __init__(self, mapper: EventMapper, scope: tuple[str, ...] = ()) -> None:
        super().__init__(scope)
        self._mapper = mapper
        self.channel: StreamChannel[EventDraft] = StreamChannel()

    @classmethod
    def for_run(cls, mapper: EventMapper) -> Callable[[tuple[str, ...]], RunEventTransformer]:
        """The factory LangGraph calls with the mux's scope."""

        def create(scope: tuple[str, ...] = ()) -> RunEventTransformer:
            return cls(mapper, scope)

        return create

    def init(self) -> dict[str, Any]:
        return {ZONIC: self.channel}

    def process(self, event: ProtocolEvent) -> bool:
        # Subgraph muxes build their own instance (``factory(scope)``) and see the events the
        # root sees too; only the root's projection is drained, and the mapper is stateful (tool
        # starts, stream maskers), so only the root maps.
        if self.scope:
            return True
        params = event["params"]
        for draft in self._mapper.map(event["method"], params["namespace"], params["data"]):
            self.channel.push(draft)
        return True


class V3EventSource:
    async def run(
        self,
        graph: Any,
        payload: Any,
        *,
        config: dict[str, Any],
        context: RunContext,
        durability: Durability,
        control: RunControl,
        mapper: EventMapper,
        emit: Emit,
    ) -> StreamedRun:
        stream = await graph.astream_events(
            payload,
            config,
            context=context,
            durability=durability,
            control=control,
            version="v3",
            transformers=[CustomTransformer, ToolCallTransformer, RunEventTransformer.for_run(mapper)],
        )
        async with stream:  # aborts the graph on any exit before the end
            async for draft in stream.extensions[ZONIC]:
                emit(draft)
            output = await stream.output()
            interrupts = await stream.interrupts()
        return StreamedRun(output=output, interrupts=list(interrupts))
