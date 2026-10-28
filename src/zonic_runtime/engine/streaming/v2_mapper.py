"""The fallback source on ``astream(..., subgraphs=True, version="v2")`` (design Section 10).

It feeds the same ``EventMapper`` as the v3 transformer, so both publish the same events
(case S-12). ``astream`` takes the ``tools`` mode too (it attaches the same
``StreamToolCallHandler`` as v3, though ``StreamMode`` does not list it), so tool events are
identical. Version 2 streams model chunks instead of content blocks: text deltas come from
the ``AIMessageChunk``s of the ``messages`` mode, and whole messages that nodes write to
state are skipped, since they are not model output.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import AIMessageChunk
from langgraph.runtime import RunControl
from langgraph.types import Durability, StreamMode

from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.streaming.mapper import CUSTOM, MESSAGES, TOOLS, EventMapper
from zonic_runtime.engine.streaming.source import Emit, StreamedRun
from zonic_runtime.models.events import EventDraft

_VALUES = "values"
_MODES = cast(list[StreamMode], [MESSAGES, CUSTOM, TOOLS, _VALUES])


def map_part(mapper: EventMapper, kind: str, namespace: Sequence[str], data: Any) -> list[EventDraft]:
    if kind == MESSAGES:
        chunk = data[0] if isinstance(data, (tuple, list)) and data else data
        if not isinstance(chunk, AIMessageChunk):
            return []
        metadata = data[1] if isinstance(data, (tuple, list)) and len(data) > 1 else None
        return mapper.text(mapper.model_namespace(namespace, metadata), chunk.text)
    return mapper.map(kind, namespace, data)


class V2EventSource:
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
        output: dict[str, Any] | None = None
        interrupts: list[Any] = []
        async for part in graph.astream(
            payload,
            config,
            context=context,
            durability=durability,
            control=control,
            stream_mode=_MODES,
            subgraphs=True,
            version="v2",
        ):
            namespace = list(part["ns"])
            if part["type"] == _VALUES:
                if not namespace:
                    output = part["data"]
                    interrupts.extend(part.get("interrupts", ()))
                continue
            for draft in map_part(mapper, part["type"], namespace, part["data"]):
                emit(draft)
        return StreamedRun(output=output, interrupts=interrupts)
