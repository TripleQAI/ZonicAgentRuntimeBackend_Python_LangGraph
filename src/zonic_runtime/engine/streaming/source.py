"""The interface both event sources implement (design Section 10, "Fallback").

A source runs the compiled graph once, passes every run event draft to ``emit`` as it
arrives, and returns the final state and the pending interrupts. Exceptions of the run
(node failures, ``GraphRecursionError``, ``GraphDrained``, cancellation) propagate to the
driver unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from langgraph.runtime import RunControl
from langgraph.types import Durability

from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.streaming.mapper import EventMapper
from zonic_runtime.models.events import EventDraft

Emit = Callable[[EventDraft], object]


@dataclass(frozen=True)
class StreamedRun:
    #: The root graph's last state, ``None`` when the graph emitted none.
    output: dict[str, Any] | None
    #: LangGraph ``Interrupt``s pending at the end; empty when the run finished.
    interrupts: list[Any] = field(default_factory=list)


class EventSource(Protocol):
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
    ) -> StreamedRun: ...
